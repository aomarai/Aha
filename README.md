# Aha

[![Coverage](https://sq.homelab.local/api/project_badges/measure?project=aomarai_Aha_b5802f94-d481-4d0f-99fc-841310f2f7c2&metric=coverage&token=sqb_0372f73fdf3a98a173c869d0ad20370857ca068f)](https://sq.homelab.local/dashboard?id=aomarai_Aha_b5802f94-d481-4d0f-99fc-841310f2f7c2)

A Discord bot written in Python with [discord.py](https://discordpy.readthedocs.io/)
and managed with [uv](https://docs.astral.sh/uv/).

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs Python for you)
- A Discord bot token from the
  [Developer Portal](https://discord.com/developers/applications)

Python **3.15** is pinned in `.python-version`; `uv` downloads it on demand, so
nothing needs to be installed system-wide.

## Quickstart

```bash
# 1. Install uv (if you do not have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Create a bot, then configure it
cp .env.example .env          # add your DISCORD_BOT_TOKEN
#   In the Developer Portal enable: Bot -> Privileged Gateway Intents ->
#   "Message Content Intent".

# 3. Install dependencies (also fetches Python 3.15)
uv sync

# 4. Run the bot
uv run aha                    # or: uv run python -m aha
```

Invite the bot to a server (replace `APP_ID` with your application ID). The
`permissions` value grants View Channel, Send Messages, Embed Links and Read
Message History — enough for slash and prefix commands; adjust as needed:

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=84992
```

Verify it is alive with the `/ping` slash command (or `!ping` with the default
prefix). Slash commands work with any permissions; prefix commands additionally
need the bot to see and read messages in the channel.

### Slash-command syncing

`AhaBot.setup_hook` syncs the command tree on every startup:

- With `DEV_GUILD_ID` set, commands are synced to that guild (instant) **and**
  any previously-synced global commands are cleared, so switching the setting
  never leaves stale duplicates.
- Without it, commands are synced globally, which can take up to an hour to
  propagate.
- Sync overwrites the application's remote command set, so avoid running two
  bot revisions at once — whichever syncs last wins.

## Project layout

```
Aha/
├── src/aha/
│   ├── __main__.py      # entry point: load .env, build settings, run the bot
│   ├── bot.py           # AhaBot (commands.Bot) + setup_hook/on_ready
│   ├── config.py        # Settings loaded & validated from the environment
│   ├── logging_setup.py # logging configuration
│   └── cogs/            # command groups, each exposing async setup(bot)
│       └── meta.py      # /ping
├── tests/               # pytest suite
├── .devcontainer/       # VS Code dev container (Python 3.15 + uv)
├── .github/workflows/   # CI (lint, format, tests)
├── .env.example         # template for local secrets
├── pyproject.toml       # project metadata, dependencies, ruff/pytest config
└── uv.lock              # reproducible dependency lockfile (committed)
```

## Configuration

All configuration is read from environment variables (see `.env.example`).
Real environment variables take precedence over `.env`.

| Variable            | Required | Default | Purpose                                         |
|---------------------|----------|---------|-------------------------------------------------|
| `DISCORD_BOT_TOKEN` | yes      | –       | Bot token from the Developer Portal             |
| `COMMAND_PREFIX`    | no       | `!`     | Prefix for text commands (mentions always work) |
| `LOG_LEVEL`         | no       | `INFO`  | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DEV_GUILD_ID`      | no       | –       | Sync slash commands instantly to this guild     |

## Development

```bash
uv sync                       # install runtime + dev dependencies
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy                   # type check (strict)
uv run pytest                 # tests (enforces a 95% coverage floor)
```

Git hooks (ruff, whitespace/YAML/TOML hygiene, secret scanning). They are
enforced by CI, so installing them locally is for earlier feedback:

```bash
uv run pre-commit install            # enable the hooks (once per clone)
uv run pre-commit run --all-files    # run them on everything
```

CI (`.github/workflows/ci.yml`) runs lint, format check, the pre-commit hooks,
type checking, and tests on every push to `main` and every pull request.
Pull requests are reviewed by [Sourcery](https://sourcery.ai/) (configured in
`.sourcery.yaml`; install the GitHub App on the repository to enable it).

## Dev container

A [VS Code Dev Container](https://containers.dev/) is provided so the whole
toolchain (uv + Python 3.15 + dependencies) runs in an isolated, reproducible
environment with nothing installed on your host machine.

**Use it:** open the repository in VS Code and run *Dev Containers: Reopen in Container* (or use the **Dev Containers
CLI**:
`devcontainer up --workspace-folder .`). On first start the container runs
`uv sync --locked`, which downloads Python 3.15 and installs all dependencies.

- **Image** — `.devcontainer/Dockerfile` builds on
  `mcr.microsoft.com/devcontainers/base:ubuntu-24.04` and copies a pinned `uv`
  binary from the official `ghcr.io/astral-sh/uv` image; uv then provisions the
  interpreter, so no system Python is needed.
- **Extensions** — ruff, Pylance and the Python extension are pre-installed and
  configured (ruff as the formatter, pytest discovery on `tests/`).
- **gh CLI** — included via a dev container feature for the PR workflow.

Run the bot inside the container exactly as you would locally:

```bash
uv run aha        # needs DISCORD_BOT_TOKEN in .env (see .env.example)
```

## License

MIT -- see [LICENSE](LICENSE).
