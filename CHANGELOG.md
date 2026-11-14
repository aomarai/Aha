# Changelog

All notable changes to Aha. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/). See [docs/releasing.md](docs/releasing.md)
for how a release is cut.

## [Unreleased]

### Added
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
- Translations (`aha.i18n`, `docs/i18n.md`): a JSON message catalogue with safe fallbacks, localized
  command names and descriptions sent by `aha sync`, and German as the first language; `/ping` and
  `/coin` and every error reply use the user's Discord language.
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
