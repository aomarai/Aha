# Runbook: PostgreSQL connections and settings

Provisioning, backups and the restore drill are tracked in #107; this page covers how the bot uses
the database so that sizing is not guesswork.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` / `DATABASE_URL_FILE` | unset | `postgres://` or `postgresql://` URL. **Contains the password**, so mount it from a Secret as a file in a cluster. Unset means no database: stateful features are unavailable and the bot runs as before. |
| `DB_POOL_MAX` | `5` | Connections per process (1 to 50). A small pool is enough: queries are short. |
| `DB_STATEMENT_TIMEOUT` | `2.0` | Seconds before the server cancels a query (up to 60). Kept below Discord's 3 s acknowledgement window so one slow query cannot hold a pool connection past it. |

## Sizing

`max_connections` on the server must exceed `pods x DB_POOL_MAX` plus the migration Job (1),
maintenance and monitoring connections. Example: 4 pods x 5 + 1 + headroom 10 = 31, comfortably
under the default 100. If pods multiply, lower `DB_POOL_MAX` or put PgBouncer in front (in
transaction mode it breaks session-level advisory locks, so run `aha migrate` against the server
directly, not through the pooler).

Connections identify themselves in `pg_stat_activity.application_name` as `aha` (pool) and
`aha-job` (migrations), so a DBA can see who holds what.

## When the bot will not start

- `The database schema is behind (pending: ...). Run 'aha migrate'.` The Job did not run, or ran
  against another database. Run `aha migrate --status`, then `aha migrate`.
- `DATABASE_URL must start with postgres:// ...` The value is malformed (the URL itself is never
  echoed, because it holds the password).
- Connection errors at startup are fatal on purpose: a bot that silently runs without its
  database would drop reminders and settings.
