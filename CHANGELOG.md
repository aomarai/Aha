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
