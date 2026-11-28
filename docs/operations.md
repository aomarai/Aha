# Operating Aha

How the bot connects, syncs its commands, reports health and logs. The environment variables are in
[configuration.md](configuration.md); the architecture is in [ADR 0001](adr/0001-runtime-architecture.md).

## Intents

Aha is **slash-command only** and requests **no privileged gateway intents**:
leave Message Content, Server Members and Presence switched off in the Developer
Portal. Discord's guidance is to migrate text commands to slash commands, and
privileged-intent access above 10,000 unique users requires an application that
must be renewed annually (rule change of 2026-06-10), so the bot avoids needing
it. There is no text trigger at all: the `@Aha command` mention surface was removed, so the bot
subscribes to no message events.

The bot also requests only the **non-privileged intents it uses**, not `Intents.default()`:
`guilds` (the guild and channel cache, `on_guild_remove`) and `voice_states` (who is in which voice
channel, and the voice updates forwarded to the Lavalink audio node; not privileged, kept in memory
only, ADR 0002). Messages, typing, invites, emoji, reactions, scheduled events and the rest are not
subscribed, and no message cache is kept (`max_messages=None`),
no member cache (`MemberCacheFlags.none()`) and no guild chunking at startup.
Each unused event stream costs bandwidth, CPU and memory on every shard. Add an intent in
`aha.bot.gateway_intents` only together with the feature that needs it. If a future feature genuinely needs message
text, prefer a message context-menu command (`app_commands.ContextMenu`), which
receives the message without the intent.

## Slash-command syncing

Syncing overwrites the application's remote command set, so it is a **deploy step**, not
something every process does at startup (see
[ADR 0001](adr/0001-runtime-architecture.md), section 4):

```bash
uv run aha sync --dry-run      # show what would change, write nothing
uv run aha sync                # sync only if the commands differ, then exit
uv run aha sync --guild ID     # sync to one guild (as a dev server) instead of globally
```

`aha sync` logs in over REST only (no gateway connection), compares the bot's commands with
Discord's, and writes only when they differ, so re-running it is free. Run it **once per release,
after the new revision is serving**, never before: Discord would otherwise route a new or changed
command to an old process that cannot handle it. Exit code 0 means success (including "nothing to
do"); 1 means it failed.

At startup the bot does **not** sync unless one of these is set:

- `DEV_GUILD_ID`: commands sync to that guild (instant) **and** any previously-synced global
  commands are cleared, so switching the setting never leaves stale duplicates. Unchanged
  targets are skipped.
- `SYNC_ON_STARTUP=true`: global sync at every start. Only for a single self-hosted process;
  with more than one process or revision the last writer wins.

Without either, startup logs `Command sync skipped at startup (run 'aha sync')`.

`/sync` exists only in those two modes (it is not even registered otherwise), and only the bot's
owner with Manage Server may run it, so no guild admin can rewrite the commands every guild sees.
In a production deployment, use `aha sync`.

Limits: the sync is serialized within one process by a lock and restores the local command tree if
a purge sync fails. Two *Jobs* that overlap, or a delayed Job from an older release, are not yet
fenced; that arrives with the database (#84), and until then the deploy must run one sync at a time.

## Connection health

`on_connect` and `on_disconnect` are logged (the disconnect warning includes the
WebSocket close code when discord.py exposes it), and `on_resumed` logs the
outage duration. If the gateway stays disconnected for five minutes, the
watchdog logs CRITICAL and closes the bot with a non-zero exit code, so an
on-failure supervisor restarts it; the limit is `GATEWAY_DISCONNECT_TIMEOUT` in
`bot.py`.

## Health, readiness and metrics

Each process serves a small read-only HTTP server (default `127.0.0.1:8080`, set `HEALTH_BIND` and
`HEALTH_PORT`; `HEALTH_PORT=0` turns it off). There is no authentication: access control is the bind
address and, in a cluster, a NetworkPolicy, so bind `0.0.0.0` only inside a pod network.

| Endpoint | Meaning | Use for |
|----------|---------|---------|
| `GET /livez` | The event loop answers and no critical background task has died for good. | liveness probe |
| `GET /readyz` | Every shard this process owns is connected and ready (a disconnect shorter than 30 s does not count), and no critical task has died or is still recovering from a crash. `503` until the first READY. | readiness / startup probe; gates StatefulSet rollouts |
| `GET /metrics` | Prometheus text exposition. | scrape |

Metrics: `aha_commands_total{command,outcome}` (`ok`, `error`, `denied`, `cooldown`),
`aha_command_duration_seconds{command}`, `aha_gateway_latency_seconds{shard}`,
`aha_shard_ready{shard}`, `aha_gateway_reconnects_total{shard}` and `aha_guilds{shard}`. Commands are
counted in one place (`cogs/observability.py`), so a new command is covered automatically. Alert
rules for the metrics that exist today are in [`deploy/alerts/aha.rules.yml`](../deploy/alerts/aha.rules.yml).

## Logging

`LOG_FORMAT=json` writes one JSON object per line to stdout for a log store such as Loki. Every
record carries `ts` (UTC, ISO 8601), `level`, `logger` and `msg`, plus `exc` for a traceback. When
set in the environment, `HOSTNAME` (as `pod`), `AHA_VERSION` (`version`) and `AHA_COMMIT`
(`commit`) are added to every record. While a command is being handled, its records also carry
`shard_id`, `guild_id`, `channel_id`, `user_id`, `interaction_id` and `command`. Every completed command is also written to the `aha.audit` logger at
INFO as an audit trail (`event=command`, `outcome=ok`, the argument **names**, `duration_ms`); argument
values are never logged, since they are user content. Filter on that logger name to route or silence
the trail. The bot token is replaced with `***` in both formats, including inside
tracebacks.

## Command access control
Commands that can be spammed or that expose operator power declare their own
guardrails. Hybrid commands run `prepare()` on the slash path too, so the
`commands.*` decorators apply to both surfaces; `app_commands.*` controls what
Discord shows and enforces at interaction time. Apply the decorators **below**
`@hybrid_command` so the hybrid command can read them off the callback:

```python
@hybrid_command(name="example", description="…")
@app_commands.default_permissions(manage_guild=True)  # hidden unless Manage Server
@app_commands.checks.has_permissions(manage_guild=True)  # interaction-time check
@commands.guild_only()  # prefix + slash (sets the app flag)
@commands.has_permissions(manage_guild=True)  # enforced on both paths
@commands.cooldown(1, 5, commands.BucketType.user)  # per-user rate limit
async def example(ctx: commands.Context[commands.Bot]) -> None: ...
```

Replies go through `aha.replies.reply` (or `respond`/`edit_message` for interaction
responses), which disable mentions, and a bot-wide
per-user budget (10 invocations per 10 s) applies to every command; `/help` and
`/sync` also carry their own cooldowns. The budget is kept per process behind a swappable
`RateLimitBackend`, so with several processes a user can use up to that many invocations per process. Permission and cooldown failures reply
through the global error handler: ephemeral for slash invocations and a normal
(public) message for mention invocations, since Discord cannot make a message
reply private. Guild-only is enforced by Discord for slash invocations and by the
local check for mentions. `/sync` (in `cogs/ops.py`) demonstrates the full pattern; `/ping`,
`/roll`, and `/coin` carry cooldowns.
