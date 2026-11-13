# Contributing to Aha

Thanks for helping. This is a small project with strict quality gates, so the quickest way to a
merged change is to know what they are before you start. [AGENTS.md](AGENTS.md) is the full,
authoritative description of conventions (it is written for AI coding agents but is just as useful
to people); this page is the short version.

## Set up

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and nothing else: it
downloads the pinned Python (3.14) itself.

```bash
git clone https://github.com/aomarai/Aha.git && cd Aha
uv sync                           # runtime and dev dependencies
uv run pre-commit install         # optional: run the hooks on every commit
cp .env.example .env              # then add DISCORD_BOT_TOKEN (see the README)
uv run aha                        # run the bot
```

A [dev container](.devcontainer/) is provided if you would rather not install anything locally.
Use a **separate Discord application and token for development**, never the production one, and set
`DEV_GUILD_ID` so commands sync to your test server instantly.

## The loop

```bash
uv run ruff format . && uv run ruff check .   # format and lint
uv run mypy                                   # strict type check
uv run pytest                                 # tests; enforces 95% coverage (we are at 100%)
uv run pre-commit run --all-files             # hooks, including the gitleaks secret scan
```

CI runs all of that, then a **mutation-testing gate** (`mutmut`, score at least 0.98), then builds
and scans the Docker image. Because of the mutation gate, tests must be *assertive*: a test that
runs a line but would still pass if the line were changed does not count. Assert exact messages,
call arguments and boundaries. To check locally (it takes a few minutes):

```bash
uv run mutmut run && uv run mutmut results
```

Check that the run completed (`failed to collect stats` in its output means it aborted and
listed nothing); then `uv run mutmut show <name>` prints a surviving mutant.

## Making a change

1. **Open or find an issue first** for anything larger than a typo. The [roadmap](https://github.com/aomarai/Aha/issues/20)
   shows what is planned and in what order, and [ADR 0001](docs/adr/0001-runtime-architecture.md)
   records the architecture; please follow it rather than re-deciding it in a PR.
2. **Branch from `main`.** Never push to `main`; every change is a pull request.
3. **Keep it small.** One concern per PR is easier to review, test and revert.
4. **Fill in the PR template**: what changed, why, and how you tested it. Say plainly what you did
   *not* verify.
5. **CI must be green** and review comments (including [Sourcery](https://sourcery.ai/)'s automated
   review) addressed or answered. Reply to a comment you disagree with instead of ignoring it.

## House rules that CI will not catch

- Every module starts with a docstring and `from __future__ import annotations`; no `# type: ignore`.
- Reply to users only through `aha.replies` (mentions are disabled there). Never call `ctx.reply`.
- Commands use `aha.commands.hybrid_command` and declare `extras={"category": ...}`.
- State that must survive a restart or another process never lives in memory: see
  [docs/components.md](docs/components.md) and the invariant in AGENTS.md.
- A user-supplied URL is fetched only through `aha.net` (it is the SSRF guard). Untrusted text goes
  through `aha.textutil` before it is shown.
- Log argument **names**, never user content, and never the bot token.
- New GitHub Actions are pinned by commit SHA with a version comment.

## Security

Do not open a public issue for a vulnerability; see [SECURITY.md](SECURITY.md). Never commit a
token; if you did, rotate it first (the [runbook](docs/runbooks/token-rotation.md)) and then deal with
the history.

## Conduct

Be kind and assume good faith. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
