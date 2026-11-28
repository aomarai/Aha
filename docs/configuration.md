# Configuration

All configuration is read from environment variables (see `.env.example`).
Real environment variables take precedence over `.env`.

| Variable            | Required | Default | Purpose                                         |
|---------------------|----------|---------|-------------------------------------------------|
| `DISCORD_BOT_TOKEN` | yes*     | –       | Bot token from the Developer Portal             |
| `DISCORD_BOT_TOKEN_FILE` | yes* | –      | Path of a file holding the token (use instead of the variable above; set exactly one) |
| `LOG_LEVEL`         | no       | `INFO`  | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LOG_FORMAT`        | no       | `text`  | `text` (human-readable) or `json` (one object per line) |
| `DEV_GUILD_ID`      | no       | –       | Sync slash commands instantly to this guild     |
| `SYNC_ON_STARTUP`   | no       | `false` | Sync commands at every start (single process only) |
| `BOT_ACTIVITY`      | no       | `/help` | Activity text under the bot's name; `off` shows none (max 128 chars) |
| `BOT_ACTIVITY_TYPE` | no       | `listening` | `playing`, `listening`, `watching` or `competing` |
| `BOT_STATUS`        | no       | `online` | `online`, `idle`, `dnd` or `invisible`      |
| `DATABASE_URL`      | no       | –       | PostgreSQL URL (or `DATABASE_URL_FILE`); see [docs/runbooks/postgres.md](runbooks/postgres.md) |
| `DB_POOL_MAX`       | no       | `5`     | Database connections per process               |
| `DB_STATEMENT_TIMEOUT` | no    | `2.0`   | Seconds before a slow query is cancelled        |
| `HEALTH_BIND`       | no       | `127.0.0.1` | Address of the health/metrics server        |
| `HEALTH_PORT`       | no       | `8080`  | Port of the health/metrics server; `0` disables it |
| `SHARD_COUNT`       | no       | –       | Total gateway shards; unset lets Discord choose (single process only). See [docs/sharding.md](sharding.md) |
| `SHARD_IDS`         | no       | all     | Shards this process owns, e.g. `0,1` (needs `SHARD_COUNT`) |
| `ANTHROPIC_API_KEY` | no | – | Enables `/summarize` (or `ANTHROPIC_API_KEY_FILE`; set at most one). Off per server until an admin runs `/configure summaries on` |
| `ANTHROPIC_MODEL` | no | `claude-haiku-5-5` | Model used for summaries |
| `LAVALINK_URL`, `LAVALINK_PASSWORD` | no | – | Voice playback: the [Lavalink](https://github.com/lavalink-devs/Lavalink) audio node (`http://host:2333`) and its password (or `LAVALINK_PASSWORD_FILE`; set at most one). Set both or neither; without them `/join` says playback is off. See [ADR 0002](adr/0002-voice-playback.md) |
| `VOICE_MAX_PLAYERS`, `VOICE_IDLE_MINUTES`, `VOICE_MAX_TRACK_MINUTES`, `VOICE_MAX_QUEUE` | no | `10`, `5`, `30`, `50` | Voice sessions one process holds at once (1-200); minutes alone, silent or paused before the bot leaves (1-120); longest track `/play` accepts (1-600); tracks one server may queue (1-500). Voice also needs `DATABASE_URL` for the queue |
| `SUMMARIZE_DAILY_QUOTA`, `SUMMARIZE_MAX_CHARS` | no | `10`, `4000` | Summaries per user per UTC day (1-1000); characters sent to the model (100-20000) |
| `SESSION_START_FLOOR` | no     | `0.1`   | Wait for Discord's reset instead of identifying when fewer than this fraction of the daily gateway sessions remain (`0` disables); see [the runbook](runbooks/session-budget.md) |
| `SHARDS_PER_POD`, `POD_INDEX` | no | – | Derive `SHARD_IDS` from a StatefulSet ordinal instead (needs `SHARD_COUNT`) |

\* Exactly one of `DISCORD_BOT_TOKEN` and `DISCORD_BOT_TOKEN_FILE` is required. See the
[token rotation runbook](runbooks/token-rotation.md).
