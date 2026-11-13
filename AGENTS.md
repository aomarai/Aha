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
measured 100%, a ratchet meant to be raised as surviving mutants are killed). Separate workflows
run security checks and SonarQube.

Consequence: tests must be *assertive*, not just covering. A test that executes a line but would
still pass if that line were deleted or altered lets the mutant survive. When writing or fixing
tests, assert on observable effects (exact log messages, call arguments, call order) so that
removing or mutating the code under test fails the test.

## Architecture

- `__main__.main()` loads `.env` from the **current directory only** (not parent dirs; real env
  vars win), builds `Settings`, configures logging, then runs `AhaBot` via `_serve` (not
  `bot.run()`): it owns the event loop so SIGTERM cancels `bot.start()` and closes the bot (POSIX only; SIGINT
  is left to `asyncio.run` so a second Ctrl+C can break a hung shutdown;
  `bot.start()` installs no log handler, so `logging_setup` owns all handlers).
  Misconfiguration returns exit code 1.
- `config.Settings` is a frozen dataclass built by `Settings.from_env(env=None)`. Pass an explicit
  mapping in tests instead of mutating `os.environ`. Invalid values raise `ConfigError` at
  startup rather than silently falling back.
- `bot.AhaBot.setup_hook` loads every module in `COG_MODULES` and syncs the app-command tree. With
  `DEV_GUILD_ID` set it copies globals to that guild, syncs it, then clears and re-syncs the
  global set so no stale global commands remain. Sync overwrites remote commands — the last bot
  revision to start wins.
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
- Cog replies go through `aha.replies.reply`, which defaults to
  `allowed_mentions=AllowedMentions.none()`; never call `ctx.reply` directly, or user text can
  ping roles or `@everyone`. A bot-wide per-user budget is enforced by
  `AhaTree.interaction_check` (slash) and `AhaBot._enforce_rate_limit` (mentions), so a new
  command inherits rate limiting without its own decorator. Interaction responses use
  `aha.replies.respond` / `edit_message`, which apply the same mention policy.
- Tests are plain pytest functions; async code is driven with `asyncio.run(...)` (no
  pytest-asyncio). Discord collaborators are replaced with `monkeypatch.setattr` fakes that
  record calls; logs are checked with `caplog` against the `aha.*` logger.
- Mutmut copies the repo to `mutants/` and runs `tests/` there with `--no-cov`; `scripts/` is in
  `also_copy` because `tests/test_mutation_score.py` imports the gate module. Keep that in mind
  if tests start importing other top-level directories.
