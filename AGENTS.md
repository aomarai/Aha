# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

Aha is a small Discord bot (discord.py, src layout, managed with `uv`). Python is pinned to 3.14
(`requires-python = ">=3.14,<3.15"` — the upper bound is deliberate, see the comment in
`pyproject.toml`). Always run tools via `uv run` so versions match `uv.lock`.

## Commands

```bash
uv sync                                   # install runtime + dev deps
uv run aha                                # run the bot (needs DISCORD_BOT_TOKEN in .env)
uv run aha sync [--dry-run] [--guild ID]  # sync slash commands over REST, only if changed
uv run ruff check .                       # lint
uv run ruff format .                      # format (CI runs `--check`)
uv run mypy                               # strict type check of src/, tests/, scripts/
uv run pytest                             # full suite, enforces 95% coverage
uv run pytest tests/test_bot.py::test_name --no-cov   # single test (--no-cov skips the 95% gate)
uv run pre-commit run --all-files         # ruff + hygiene + gitleaks secret scan
uv run mutmut run && uv run mutmut export-cicd-stats && uv run python scripts/mutation_score.py
```

Running a subset of tests without `--no-cov` will report a coverage failure even when every test
passes — that is the global `--cov-fail-under=95` in `addopts`, not a real failure.

## CI gates (all must pass)

`.github/workflows/ci.yml`: ruff lint, ruff format check, pre-commit (incl. gitleaks), mypy
strict, pytest with 95% branch coverage, then a **mutation-testing job** that fails if the
mutmut score drops below `THRESHOLD` in `scripts/mutation_score.py` (currently 0.98 against a
measured 100%, a ratchet meant to be raised as surviving mutants are killed) and when the campaign
enumerates fewer than `MIN_MUTANTS` (544), so a silently shrinking campaign cannot pass. Separate
workflows run security checks and SonarQube. `.github/workflows/image.yml` builds the production
image on every PR, smoke-tests it (`scripts/smoke_image.sh`: unprivileged user, imports, `--version`,
`aha sync --help`, a missing token fails with exit code 1) and scans it with Trivy, failing on
**fixable** HIGH/CRITICAL findings; an exception goes in `.trivyignore` with a justification and an
expiry. The SARIF report goes to code scanning and a CycloneDX SBOM is kept as a build artifact.
Anything that changes what the image needs (a new stdlib module the image strips, a new data file)
must keep the smoke test passing; extend `scripts/smoke_image.sh` when you add such a dependency.

Consequence: tests must be *assertive*, not just covering. A test that executes a line but would
still pass if that line were deleted or altered lets the mutant survive. When writing or fixing
tests, assert on observable effects (exact log messages, call arguments, call order) so that
removing or mutating the code under test fails the test.

## Architecture

The target runtime architecture is recorded in
[`docs/adr/0001-runtime-architecture.md`](docs/adr/0001-runtime-architecture.md): scale out by
shard groups (one StatefulSet pod per disjoint block of shards), Postgres as the shared system of
record, command sync as a deploy step. Follow it rather than re-deciding it in a PR. The invariant
every change must keep:

> **A handler must stay correct when the next interaction from the same user or guild lands on a
> different pod, or after a restart.** Nothing that must survive lives only in process memory;
> component `custom_id`s carry their own state.

- `__main__.main()` loads `.env` from the **current directory only** (not parent dirs; real env
  vars win), builds `Settings`, configures logging, then runs `AhaBot` via `_serve` (not
  `bot.run()`): it owns the event loop so SIGTERM cancels `bot.start()` and closes the bot (POSIX only; SIGINT
  is left to `asyncio.run` so a second Ctrl+C can break a hung shutdown;
  `bot.start()` installs no log handler, so `logging_setup` owns all handlers).
  Misconfiguration returns exit code 1.
- `config.Settings` is a frozen dataclass built by `Settings.from_env(env=None)`. Pass an explicit
  mapping in tests instead of mutating `os.environ`. Invalid values raise `ConfigError` at
  startup rather than silently falling back. The token comes from `DISCORD_BOT_TOKEN` or the file named by
  `DISCORD_BOT_TOKEN_FILE` (exactly one; never put it in an error message or log).
- `bot.AhaBot.setup_hook` loads every module in `COG_MODULES` and, only when `/sync` is meant to
  exist (`DEV_GUILD_ID` or `SYNC_ON_STARTUP`), `OPERATOR_COG` (`aha.cogs.ops`, the owner-only
  `/sync`). It syncs the command tree at startup only in those same modes
  (`bot.sync_on_startup`); production syncs through `aha sync` (a deploy Job run *after* the
  rollout, ADR 0001 §4). `sync_commands()` diffs each target against Discord's raw payloads
  (`aha.cmdsync`, raw because discord.py 2.7 parses `contexts`/`integration_types` lossily) and
  writes only on a difference. With `DEV_GUILD_ID` set it copies globals to that guild, syncs it,
  then clears and re-syncs the global set so no stale global commands remain. Sync overwrites
  remote commands; two overlapping revisions still race (fencing arrives with the database).
- A command that must exist in production belongs in `COG_MODULES`; the tree `aha sync` pushes
  must equal the tree the pods serve, so never register commands conditionally on anything but
  the settings both read (`sync_command_enabled`).
- Cogs live in `src/aha/cogs/`, each exposing `async def setup(bot)`. **Adding a cog requires
  appending its dotted path to `COG_MODULES` in `bot.py`** (a test asserts all entries are under
  `aha.cogs.`).
- Use `aha.commands.hybrid_command`, not `discord.ext.commands.hybrid_command`, for hybrid
  (slash + mention) commands. The wrapper exists purely so mypy strict accepts the decorator on cog
  methods without `# type: ignore`.
- Hybrid commands run `prepare()` on the slash path too, so `commands.cooldown`,
  `commands.guild_only`, and `commands.has_permissions` apply to both surfaces.
  `app_commands.default_permissions` (Discord-side visibility) and `app_commands.checks.*`
  (interaction-time checks) must be applied *below* `@hybrid_command` so `HybridCommand` reads
  them off the callback. `commands.guild_only()` also sets the app command's `guild_only` flag.

## Conventions

- Every module starts with a docstring and `from __future__ import annotations` (a bare package
  marker — an `__init__.py` holding only a docstring — is exempt). Modules under `src/aha/` also
  declare `__all__`; test modules and `scripts/` are not a public API, so they need not.
  Module-level logger: `log = logging.getLogger(__name__)`; use %-style log args.
- Line length 100; ruff rule set is in `pyproject.toml`. No `# type: ignore` — fix types instead.
- Every command declares its `/help` category: `@hybrid_command(..., extras={"category": "Info"})`
  (`Info`, `Fun`, `Admin`; a test fails for a command without one). `/help` groups by it, with
  headers only when more than one category exists.
- Cog replies go through `aha.replies.reply`, which defaults to
  `allowed_mentions=AllowedMentions.none()`; never call `ctx.reply` directly, or user text can
  ping roles or `@everyone`. A bot-wide per-user budget is enforced by
  `AhaTree.interaction_check` (slash) and `AhaBot._enforce_rate_limit` (mentions), so a new
  command inherits rate limiting without its own decorator. The budget is per process; a
  multi-process deployment needs shared state (see #2). Interaction responses use
  `aha.replies.respond` / `edit_message`, which apply the same mention policy.
- Logging: `aha.logctx` carries the per-invocation fields (`shard_id`, `guild_id`, `channel_id`,
  `user_id`, `interaction_id`/`message_id`, `command`) in a `ContextVar`, bound by
  `AhaTree.interaction_check` (slash), `AhaBot._enforce_rate_limit` (mentions) and a view's own
  `interaction_check` (button clicks never pass the tree, so call `logctx.bind_interaction`
  there); `TaskRegistry` runs spawned tasks in a clean context. The JSON
  formatter (`LOG_FORMAT=json`) adds them to every record, so do not repeat those ids in log
  messages. Structured extras go in `extra={...}`. Both formatters mask the bot token. Never log
  `settings.token` deliberately, and keep new log fields free of user-supplied free text.
- Health and metrics: `aha.health.HealthServer` (started by `setup_hook` as the critical,
  restartable `health-server` task unless `HEALTH_PORT=0`) serves `/livez`, `/readyz`, `/metrics`;
  readiness is `bot.shard_states()` plus `bot.tasks.healthy`. Metrics live in the per-bot
  registry `bot.metrics`; commands are counted only in `cogs/observability.py` (from
  `command_completion`, `command_error`, `app_command_error`; not `app_command_completion`,
  which discord.py also fires for hybrid commands and would double-count), so never increment
  `aha_commands_total` from a cog. Tests get an idle fake server from `tests/conftest.py`.
- Audit trail: `cogs/observability.py` writes one `aha.audit` INFO record per completed command
  (`extra={event, outcome, arguments, duration_ms}`; who/where come from the log context). Log
  argument **names** only, never values, and never use `args`, `msg`, `message` or the other
  reserved `LogRecord` names as `extra` keys (logging raises `KeyError`).
- Background tasks: never call `asyncio.create_task` in a cog or hook for long-running work. Use
  `bot.tasks.spawn(factory, name=..., restart=..., critical=...)` (`aha.tasks.TaskRegistry`): it
  keeps a strong reference, logs failures with the task name, restarts with capped back-off when
  asked, feeds the readiness probe through `bot.tasks.healthy`, and is cancelled by `bot.close()`.
  `spawn` takes a zero-argument *factory* (not a coroutine) so a restart can build a fresh one.
- Concurrency: every process is one asyncio event loop. `RateLimiter` and `sync_commands`
  state is process-local and safe because no critical section awaits; `sync_commands`
  serializes with an `asyncio.Lock` and restores the local tree in a `finally`. In-process
  `AutoShardedClient` shards share that state; separate processes need #2.
- Tests are plain pytest functions; async code is driven with `asyncio.run(...)` (no
  pytest-asyncio). Discord collaborators are replaced with `monkeypatch.setattr` fakes that
  record calls; logs are checked with `caplog` against the `aha.*` logger.
- Mutmut copies the repo to `mutants/` and runs `tests/` there with `--no-cov`; `scripts/` is in
  `also_copy` because `tests/test_mutation_score.py` imports the gate module. Keep that in mind
  if tests start importing other top-level directories.
