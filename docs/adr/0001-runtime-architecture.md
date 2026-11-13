# ADR 0001: Runtime architecture for horizontal scale

- **Status:** Accepted
- **Date:** 2026-11-13
- **Tracking:** [#91](https://github.com/aomarai/Aha/issues/91), epic [#2](https://github.com/aomarai/Aha/issues/2)

Issue references below (`#N`) point at github.com/aomarai/Aha issues.

## Context

Aha is a slash-command-only discord.py **gateway** bot with no privileged intents, running as one process. Per-process state today: the `RateLimiter`, the `sync_commands` lock, and the `/help` paginator views (in memory, 120 s). There is no database.

## Decision

### 1. Ingress: stay on the gateway (discord.py)
Interactions keep arriving over the gateway WebSocket. The HTTP Interactions Endpoint was considered and deferred (see Alternatives).

### 2. Scale out by shard groups, not by identical replicas
- Discord allows **one** gateway session per shard. Identical replicas of the same shard invalidate each other's sessions, so they are never a scaling step.
- Runtime: a **StatefulSet**. Pod ordinal `i` owns a fixed, disjoint block of shard ids. `SHARD_COUNT` and the ids per pod are explicit config, validated at startup (#93). Each pod runs `AutoShardedBot(shard_ids=..., shard_count=...)`.
- Today that means 1 pod and 1 shard, which needs no change to keep working.
- Changing `SHARD_COUNT` is a planned **reshard** (restart every pod), not autoscaling. There is no HPA on gateway pods.
- **Guild-scoped work partitions itself:** every event and interaction for a guild arrives on that guild's shard, so on one pod. **DMs arrive on shard 0 only.**
- More pods buy memory, CPU and gateway-session spread. They do **not** raise Discord's REST quota (see §6), and they do not make a shard highly available: a shard is offline while its pod restarts.

### 3. State tiers
| Tier | Lives in | Examples |
|---|---|---|
| Process-local (allowed) | memory | caches, the sync lock, short-lived views with a timeout |
| Durable, shared | **Postgres** (system of record) | guild settings, reminders, job queue, consent/deletion records |
| Coordination | **Postgres advisory locks** | IDENTIFY slots across pods, single-writer jobs |
| Hot shared counters | Redis, **only on trigger** | cross-pod per-user rate limit (#102) |

**Invariant:** every handler must stay correct when the next interaction from the same user or guild lands on a different pod, or after a restart. Component `custom_id`s carry their own state (#67), and nothing that must survive lives only in memory.

Postgres is introduced by the first stateful feature (#84) and is the only new stateful dependency. SQLite is dropped because it cannot be shared across pods.

### 4. Command sync is a deploy step
`aha sync` runs as a one-shot Job, once per release (#92). Production pods never sync at startup, which removes the last-writer-wins race (#81, #83). The `DEV_GUILD_ID` dev loop keeps its startup sync.

### 5. Timed work runs on any replica
Due jobs (reminders and similar) live in Postgres. Any pod claims them with `SELECT ... FOR UPDATE SKIP LOCKED` and delivers through REST, which needs no gateway shard (#96). Timed work therefore scales with the replicas and survives restarts.

### 6. Discord limits that pods do not raise
- **REST:** a global ~50 req/s per bot token, shared by every pod, plus per-route buckets. **Invalid requests** (401/403/429) above 10,000 per 10 minutes trigger a temporary Cloudflare ban of the IP. A shared REST proxy is introduced on trigger (#103).
- **Session start limit:** identifies per 24 h (typically 1,000), with `max_concurrency` buckets. A crash loop can exhaust it and lock the bot out for a day. Mitigated by #94.
- **3-second interaction ack:** anything that can be slow (outbound HTTP, an LLM call, a DB call under load) defers first. The interaction token is then valid for 15 minutes.

### 7. Slow and outbound work
Slow work runs in-process: defer, then a timeout and a per-pod concurrency semaphore. A separate worker tier (queue plus consumers) is added **only** if measured shard-loop latency demands it.

### 8. Operability
- Every pod serves `/livez`, `/readyz` (all owned shards READY) and `/metrics` on an HTTP port. aiohttp is already a discord.py dependency (#17).
- Logs are JSON to stdout, carrying shard, pod and interaction ids (#100).

### 9. Platform
- The k3s homelab, running GHCR images pinned by digest (#37).
- Manifests applied by GitOps (#105), with migrations and `aha sync` as pre-deploy Jobs.
- A separate **staging** Discord application with its own token (#104).

## Scaling stages
| Stage | Trigger | Shape |
|---|---|---|
| 0 (today) | — | 1 pod, 1 shard, no DB |
| 1 | first stateful feature | 1 pod + Postgres, sync Job |
| 1b | 75–100 guilds | Discord verification: privacy policy, ToS, data deletion (#99) |
| 2 | ~1,000+ guilds, memory pressure, or Discord requires sharding (2,500 guilds/shard) | StatefulSet N pods, explicit shards, IDENTIFY coordination |
| 3 | sustained REST 429s, or a measured cross-pod rate-limit need | REST proxy, Redis limiter |

## Alternatives considered
- **HTTP Interactions Endpoint (stateless Deployment + HPA).** This is the most "web-like" option, and Aha's slash-only design suits it. Deferred because:
  - discord.py has no HTTP-interactions server, so every cog and check would be rewritten;
  - it needs a public HTTPS ingress into the homelab with Ed25519 signature verification;
  - it gives up mention commands, presence (#16), guild counts (#86) and `on_guild_remove`, which #99 needs to delete data.

  Revisit if gateway shards become the bottleneck for command throughput, or if the bot ever drops gateway-only features.
- **Plain replicas of the same shard:** invalid (session invalidation loop).
- **Redis from day one:** an extra stateful service with no current need. Deferred behind a trigger.
- **Gateway → message bus → worker split** (gateway proxies publishing to NATS/RabbitMQ): justified only far beyond stage 3.

## Consequences

- New code must respect the invariant in §3. Reviewers should reject handlers that keep
  must-survive state only in memory.
- Scaling decisions are revisited at the triggers in the stage table, not speculatively.
  Changing a decision here means a new ADR that supersedes this one.
- Single-process self-hosting stays supported (`SYNC_ON_STARTUP`, no `DATABASE_URL`), but it
  is not the primary target.
