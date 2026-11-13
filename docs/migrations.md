# Database migrations

The bot's only stateful dependency is PostgreSQL (ADR 0001, section 3). The schema changes through
plain SQL files in `src/aha/migrations/`, named `NNNN_description.sql` (four digits, versions
`1, 2, 3, ...` with no gaps). `aha.dbmigrate` applies them; there is no ORM.

## Running them

```bash
uv run aha migrate --status      # what is applied and what is pending; changes nothing
uv run aha migrate               # apply what is pending, then exit
```

**Migrations run as a deploy Job, never at pod startup**, before the new revision rolls out
(#105). Pods only *check*: at startup the bot refuses to run if the schema is behind what that
build knows (`Run 'aha migrate'`). A schema that is *ahead* is normal (a rollback runs old code on
the newer schema) and is accepted.

The runner takes a session-level **advisory lock**, so even two Jobs cannot overlap; applies each
migration and its bookkeeping row in **one transaction** (a failure rolls that migration back and
stops the run); and sets `lock_timeout = 5s` so an `ALTER TABLE` waiting behind a long transaction
fails loudly instead of queueing and blocking every query behind it. `statement_timeout` is
disabled for the migration itself, since a legitimate migration may run long.

## Writing a migration: expand / contract

Because the previous release must keep working against the new schema (so a rollback never needs a
down-migration), a release may only **add**:

- new tables, new **nullable** columns (or columns with a constant default), new indexes;
- create indexes on big tables with `CREATE INDEX CONCURRENTLY`, in a migration of its own (it
  cannot run inside a transaction block, so see "Non-transactional migrations" below);
- backfill in batches, not one giant `UPDATE`.

Anything destructive (drop, rename, `NOT NULL` on an existing column, a narrower type) ships in a
**later release**, after every pod running code that uses the old shape has drained.

Every table must also have a purge path for user and guild data (#99): a test will enforce that
once the first table exists.

### Non-transactional migrations

`CREATE INDEX CONCURRENTLY` and a few other statements cannot run in a transaction. The runner does
not support them yet; the first migration that needs one should extend `aha.dbmigrate` (for example
a `-- no-transaction` header) with a test, rather than working around it in SQL.

## Tests

`tests/test_dbmigrate.py` covers the runner with a fake connection. `tests/test_postgres_integration.py`
runs against a real PostgreSQL when `AHA_TEST_DATABASE_URL` is set (CI sets it through a service
container): rollback, the lock under concurrency, `lock_timeout`, and the statement timeout. Locally:

```bash
docker run -d --rm --name aha-pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 postgres:17-alpine
AHA_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres uv run pytest
```
