# Aha

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

Invite the bot to a server (replace `APP_ID` with your application ID):

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=0
```

Verify it is alive with the `/ping` slash command (or `!ping` with the default
prefix).

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
├── .env.example         # template for local secrets
├── pyproject.toml       # project metadata, dependencies, ruff/pytest config
└── uv.lock              # reproducible dependency lockfile (committed)
```

## Configuration

All configuration is read from environment variables (see `.env.example`).
Real environment variables take precedence over `.env`.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DISCORD_BOT_TOKEN` | yes | – | Bot token from the Developer Portal |
| `COMMAND_PREFIX` | no | `!` | Prefix for text commands (mentions always work) |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DEV_GUILD_ID` | no | – | Sync slash commands instantly to this guild |

## Development

```bash
uv sync                       # install runtime + dev dependencies
uv run ruff check .           # lint
uv run ruff format .          # format
uv run pytest                 # tests
```

CI (`.github/workflows/ci.yml`) runs lint, format check, and tests on every
push to `main` and every pull request. Pull requests are reviewed by
[Sourcery](https://sourcery.ai/) (configured in `.sourcery.yaml`; install the
GitHub App on the repository to enable it).

## License

MIT -- see [LICENSE](LICENSE).
