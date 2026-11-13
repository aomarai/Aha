# ADR 0001: Runtime architecture for horizontal scale

- **Status:** Accepted
- **Date:** 2026-11-13
- **Tracking:** [#91](https://github.com/aomarai/Aha/issues/91), epic [#2](https://github.com/aomarai/Aha/issues/2)

Issue references below (`#N`) point at github.com/aomarai/Aha issues.

## Context

Aha is a slash-first discord.py **gateway** bot with no privileged intents, running as one process. Hybrid commands (such as `/ping` and `/roll`) are also invocable by `@Aha` mention, which Discord delivers without the Message Content intent (whether that surface stays is #101). Per-process state today: the `RateLimiter`, the `sync_commands` lock, and the `/help` paginator views (in memory, 120 s). There is no database.

**Implementation status:** this ADR describes the *target*. Settings such as `SYNC_ON_STARTUP`, `SHARD_COUNT`/`SHARD_IDS` and `DATABASE_URL`, the `aha sync` and `aha migrate` commands, and the `/help` paginator rework do not exist yet. Each is delivered by the issue linked where it is mentioned, and until then the code behaves as the README describes.

## Decision

### 1. Ingress: stay on the gateway (discord.py)
Interactions keep arriving over the gateway WebSocket. The HTTP Interactions Endpoint was considered and deferred (see Alternatives).

### 2. Scale out by shard groups, not by identical replicas
- Discord allows **one** gateway session per shard. Identical replicas of the same shard invalidate each other's sessions, so they are never a scaling step.
- Runtime: a **StatefulSet**. Pod ordinal `i` owns a fixed, disjoint block of shard ids. `SHARD_COUNT` and the ids per pod are explicit config, validated at startup (#93). Each pod runs `AutoShardedBot(shard_ids=[...], shard_count=N)`. discord.py 2.7.1 accepts `shard_ids` (it must be a list or tuple, and `shard_count` is then required), so no wrapper process is needed.
- Today that means 1 pod and 1 shard, which needs no change to keep working.
- Changing `SHARD_COUNT` is a planned **reshard**, not autoscaling, and there is no HPA on gateway pods. A rolling update is **unsafe** for a reshard: a pod that already has the new assignment can identify a shard an old pod still owns, and Discord invalidates one of the two sessions. A reshard is therefore **stop-the-world**: scale the StatefulSet to 0, apply the new `SHARD_COUNT`, scale back up. Expect a short full outage and schedule it. Ordinary releases (same shard layout) roll one pod at a time.
- **Guild-scoped work partitions itself:** every event and interaction for a guild arrives on that guild's shard, so on one pod. **DMs arrive on shard 0 only.**
- More pods buy memory, CPU and gateway-session spread. They do **not** raise Discord's REST quota (see §6), and they do not make a shard highly available: a shard is offline while its pod restarts.

### 3. State tiers
| Tier | Lives in | Examples |
|---|---|---|
| Process-local (allowed) | memory | caches, the sync lock, short-lived views with a timeout |
| Durable, shared | **Postgres** (system of record) | guild settings, reminders, job queue, consent/deletion records |
| Coordination | **Postgres advisory locks** | IDENTIFY slots across pods, single-writer jobs |
| Hot shared counters | Redis, **only on trigger** | cross-pod per-user rate limit (#102) |

**Invariant:** every handler must stay correct when the next interaction from the same user or guild lands on a different pod, or after a restart. Two exceptions are accepted and documented rather than hidden:
- **Per-user rate limit.** Each pod keeps its own budget (`AhaBot.rate_limiter`), so a user whose interactions reach several pods can use up to `rate × pods` in one window. The documented guarantee is *per pod*. Cross-pod enforcement is deferred behind a trigger (#102).
- **Short-lived views.** The `/help` paginator keeps its pages in memory with a 120 s timeout, so a restart breaks buttons on messages already sent. It is ephemeral by design and is reworked by #67; no new feature may copy the pattern for state that must survive. Component `custom_id`s carry their own state (#67), and nothing that must survive lives only in memory.

Postgres is introduced by the first stateful feature (#84) and is the only new stateful dependency. SQLite is dropped because it cannot be shared across pods.

### 4. Command sync is a deploy step
`aha sync` runs as a one-shot Job, once per release (#92). Production pods never sync at startup, which removes the last-writer-wins race (#81, #83). The `DEV_GUILD_ID` dev loop keeps its startup sync. Today `AhaBot.setup_hook` always syncs; `SYNC_ON_STARTUP` and its default are introduced by #92.

- **Order: after the rollout, not before.** The Job runs once every pod serves the new revision (post-rollout). If it ran first, Discord would route a newly added or changed command to an old pod whose local tree cannot handle it. Running last, the new pods briefly serve handlers for commands Discord has not advertised yet, which is harmless, and commands removed in the release stay invocable until the sync removes them. Handlers for a removed command must therefore keep answering for one release.
- **Unknown or stale commands:** a pod that receives an interaction it has no handler for replies with a friendly ephemeral error rather than failing silently.
- **Fencing:** the Job holds a Postgres advisory lock so two syncs cannot overlap, and records the applied release (a monotonically increasing build number) in a `sync_state` row. It refuses to apply a release older than the recorded one, so a delayed Job from an old release cannot overwrite a newer command set. Before Postgres exists (stage 0), there is one process and one revision, so the process-local lock is enough.
- **`/sync`:** global `/sync` is removed in production (#81), so no pod can race the Job.

### 5. Timed work runs on any replica
Due jobs (reminders and similar) live in Postgres. Any pod claims them with `SELECT ... FOR UPDATE SKIP LOCKED` and delivers through REST, which needs no gateway shard (#96). Timed work therefore scales with the replicas and survives restarts.

**Delivery is at-least-once.** `SKIP LOCKED` prevents concurrent claims, but a pod can crash after Discord accepts a message and before the row is deleted, and the lease then expires and another pod re-sends it. Handlers must be idempotent where Discord allows it: pass a deterministic `nonce` derived from the job id with `enforce_nonce=True` on message sends, which makes Discord drop a duplicate within a short window, and record `delivered_at` before deleting the row. Where a duplicate cannot be suppressed (for example a DM after the nonce window), a rare duplicate reminder is accepted and documented per job kind (#96).

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
- Manifests applied by GitOps (#105). Release order: **migrate** (pre-deploy Job) → **rolling update** of the pods → **`aha sync`** (post-rollout Job).
- Migrations are forward-only and follow **expand/contract**: a release only adds (nullable columns, new tables, backfills) while the previous release is still serving. Destructive changes (drops, renames, new NOT NULL) ship one release later, after every old pod has drained. Old pods therefore never see a schema they cannot use, and a rollback needs no down-migration (#84).
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
