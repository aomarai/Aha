# Changelog

All notable changes to Aha. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/). See [docs/releasing.md](docs/releasing.md)
for how a release is cut.

## [Unreleased]

## [0.2.1] - 2026-11-28

A voice fix. No migration and no new setting.

### Fixed
- A paused voice session now counts as idle: the bot leaves after `VOICE_IDLE_MINUTES` paused, even
  with people in the channel, instead of holding the channel and its Lavalink player indefinitely.
  A pause and resume restarts the idle time, and a pause, resume or `/play` sent while the bot is
  leaving is refused instead of appearing to work.

## [0.2.0] - 2026-11-23

Show reminders, YouTube voice playback, API documentation and the Postgres-backed scheduler.
Migrations 0005 and 0006 apply on upgrade (`aha migrate` runs first). `LAVALINK_URL` is optional: without
it the voice commands say playback is off.

### Added
- The voice queue and controls (#171): `/play` queues when something is playing; `/queue`,
  `/nowplaying`, `/skip`, `/pause`, `/resume`, `/stop` and `/remove`, plus Pause/Skip/Stop buttons on
  the now-playing message. The queue is stored in PostgreSQL (`voice_queue`, migration 0006), so it
  survives a restart; `VOICE_MAX_QUEUE` caps it. Only the requester of a track (or someone with Manage
  Channels) may skip, remove or stop it. `/forgetme` and the departed-server purge delete queued
  tracks.
- `/play <YouTube link>` (#170): plays one video through the Lavalink node. Only YouTube video links
  are accepted (an allowlist; the node is asked for a canonical link built from the video id), and
  playlists, live streams and tracks over `VOICE_MAX_TRACK_MINUTES` are refused. A track the node
  cannot play is reported once in the channel. `aha_voice_tracks_total{outcome}` counts requests.
- Voice foundation (#169, ADR 0002): `/join` and `/leave`, an `AudioBackend` interface with a Lavalink
  (Wavelink) adapter, the `voice_states` intent, `LAVALINK_URL` / `LAVALINK_PASSWORD` /
  `VOICE_MAX_PLAYERS` / `VOICE_IDLE_MINUTES`, an idle janitor, and the `aha_voice_players` and
  `aha_lavalink_up` gauges. Off unless `LAVALINK_URL` is set.
- API documentation: an OpenAPI 3.1 spec for the health, readiness and metrics endpoints
  (`docs/openapi.yaml`) and a generated command reference (`docs/commands.md`), each kept in sync by
  a test.
- `/watch`, `/watching` and `/unwatch`: follow a show and get a reminder (by DM, or a ping in the
  channel) when each new episode comes out, optionally with a lead time. Schedules come from TVmaze;
  a postponed episode is rescheduled, and the subscription ends when the show does. `/forgetme` and
  the departed-server purge delete subscriptions (new table `show_subscriptions`, migration 0005).
- Token rotation runbook now documents the Sealed Secrets procedure, rehearsed on staging (#106).
- `aha sync [--guild ID] [--dry-run]`: syncs slash commands over REST, only when they differ.
  Production no longer syncs at startup; `SYNC_ON_STARTUP` opts a single process in.
- Health, readiness and Prometheus metrics server (`/livez`, `/readyz`, `/metrics`) with
  `HEALTH_BIND` / `HEALTH_PORT`.
- `LOG_FORMAT=json` structured logs with shard, guild, user, interaction and command context;
  the bot token is masked in both formats.
- Supervised background tasks (`TaskRegistry`) with failure logging, restart back-off and health.
- Image build, smoke test and Trivy scan on every pull request; versioned images published to GHCR.
- `/privacy`, draft privacy policy and terms, and a data inventory rule: a test fails when a
  migration creates a table that is not declared with its owner columns and retention.
- `/remind`, `/reminders`, `/unremind`: reminders stored in PostgreSQL and delivered by whichever
  pod runs the scheduler (needs `DATABASE_URL`). Delivery pings only the invoker, falls back to a
  DM, and drops the reminder if neither works. Limits: 25 per user, 1 year ahead, 1,000 characters.
- The bot and `aha migrate` wait up to 60 s for the database when it is not reachable yet (a new pod's
  first packets can be dropped while the cluster programs its network policy; PostgreSQL may still be
  starting), instead of failing the pod or the deploy.
- The `@Aha command` mention surface is removed: the bot subscribes to **no message events**
  (`guilds` is the only gateway intent). `aha sync --check` is a dry
  run that exits 3 on drift (a post-deploy smoke test); gitleaks rules for Discord bot tokens and
  Anthropic keys; `docs/environments.md` describes staging versus production.
- Staging environment in the cluster (docs/environments.md): generated from production, own database
  and Discord application, smoke-checked before promotion.
- Commands and menus declare **server-only** installs, so they stay server-only even if user installs
  are enabled in the Developer Portal.
- `aha_rest_rate_limited_total{kind=global|route}` (counted from discord.py's 429 log lines) with
  alerts `AhaGlobalRateLimit` and `AhaRouteRateLimited`; the trigger and candidate for a shared REST
  proxy are in ADR 0001 section 6.
- `/summarize` and a "Summarize" message menu (Claude API): off in every server until an admin runs
  `/configure summaries on`, unavailable without `ANTHROPIC_API_KEY`, a per-user daily quota that is
  refunded when the model call fails, bounded input and output, the text sent as data, answers shown
  privately as plain text, token metrics, and only counters stored (deleted by `/forgetme`).
- Translations (`aha.i18n`, `docs/i18n.md`): a JSON message catalogue with safe fallbacks, localized
  command names and descriptions sent by `aha sync`, and German as the first language; `/ping` and
  `/coin`, every error reply, reminders, server settings and `/forgetme` use the user's Discord
  language.
- `/forgetme`: deletes the invoker's reminders and scheduled jobs and anonymises their id in server
  settings, behind an expiring, invoker-only confirm button. A test fails when a table with personal
  data has no purge path.
- Gateway session-start budget: logged and exported at startup, a guard that waits for Discord's
  reset instead of identifying when it is nearly spent (`SESSION_START_FLOOR`), and a PostgreSQL
  advisory lock that serialises IDENTIFY across pods. Runbook: `docs/runbooks/session-budget.md`.
- Explicit shard assignment: `SHARD_COUNT`, `SHARD_IDS` or `SHARDS_PER_POD` + `POD_INDEX`, validated
  at startup; the bot is now an `AutoShardedBot` with per-shard readiness, reconnect metrics and
  disconnect watchdog. See `docs/sharding.md` (including the stop-the-world reshard procedure).
- Per-server settings: `/config` shows them and `/configure` changes them (Manage Server only): a log
  channel and per-command disabling (`config`, `configure` and `help` can never be disabled). Stored
  in `guild_settings` with a 60 s per-pod cache; a server that removes the bot has its data deleted
  after 30 days unless it is re-invited. Needs `DATABASE_URL`.
- Durable job scheduler on PostgreSQL (`scheduled_jobs`): any pod claims due jobs with
  `FOR UPDATE SKIP LOCKED`, at-least-once delivery, retry with back-off, dead-lettering,
  `aha_scheduler_lag_seconds` and an alert.
- PostgreSQL support: an optional pooled connection (`DATABASE_URL`), `aha migrate` plain-SQL
  migrations (advisory lock, one transaction each, bounded `lock_timeout`), and a startup check
  that refuses to run on a schema that is behind. No tables yet.
- `/url`: a link preview that cannot be pointed at internal addresses (SSRF-safe fetcher in
  `aha.net`: public addresses only, redirects re-validated, bounded time and size).
- `/poll` (native Discord polls: Discord stores the votes) and a "Count reactions" message
  context menu.
- `/time` (with timezone autocomplete), `/8ball` and `/countdown`; the `tzdata` package so zones
  resolve in the distroless image.
- `CONTRIBUTING.md`, `SECURITY.md`, a Code of Conduct, issue and pull request templates and
  `CODEOWNERS`.
- `aha --version`; build identity (`AHA_VERSION`, `AHA_COMMIT`) baked into the image.
- Architecture decision record for horizontal scaling (`docs/adr/0001-runtime-architecture.md`).

### Changed
- `/sync` moved to an operator cog that is loaded only with `DEV_GUILD_ID` or `SYNC_ON_STARTUP`
  and requires the bot owner as well as Manage Server.

## [0.1.0]

Initial development version (it predates tagged releases): slash commands (`/ping`, `/help`,
`/roll`, `/coin`, `/sync`), global error handling, embeds, rate limiting, mention-safe replies, a
gateway watchdog, a distroless production image, and strict quality gates (mypy, 95% coverage,
mutation testing).
