# Runbook: voice playback (Lavalink)

Voice playback plays YouTube links in a voice channel (`/play`, `/queue`, `/skip` and the rest).
The audio is not handled by the bot: a **Lavalink** node in the cluster fetches the audio from
YouTube, transcodes it, and streams it to Discord. The bot only tells the node what to do. Design:
[ADR 0002](../adr/0002-voice-playback.md). Deployment: `apps/aha*/lavalink.yaml` in the homelab repo.

## How the parts fit

| Part | Where | What it holds |
|------|-------|---------------|
| Bot (`aha-0`) | `aha` / `aha-staging` namespace | Commands, the checks, the queue logic. Joins deafened. |
| Lavalink node (`aha-lavalink`) | same namespace | The player state of every guild (current track, position, paused), the YouTube plugin. |
| PostgreSQL (`voice_queue`) | `aha-postgres` | Which tracks are queued in each guild, and who asked. |

Without `LAVALINK_URL` the voice commands exist but say playback is off. Without a database they say
it needs one (the queue lives there). The bot stays ready (`/readyz`) when Lavalink is down.

## What a restart does

- **A Lavalink restart** stops playback in every guild. The bot reconnects by itself (Wavelink
  retries forever). The queue is untouched, so `/resume` (or the next `/play`) starts it again.
- **A bot restart or deploy** drops the bot from its voice channels (Discord ends the session with
  the gateway connection). The queue is untouched; it does **not** rejoin by itself. The first person
  to run `/resume` in a voice channel brings it back and plays the saved queue from the first track.
  Resuming the *same* track mid-way, and rejoining without being asked, are not implemented.
  **Measured on staging (2026-11-23):** a player was playing; the bot's connection was then dropped
  without a leave (what a restart does). The node kept its session but, within a second, no player
  was left in it: Discord ends the voice connection together with the gateway session, so there is
  nothing on the node to resume. That is why the saved queue plus `/resume` is the recovery path.
- **A database restart** makes the queue commands (`/play`, `/queue`, `/skip`, `/remove`, `/resume`,
  `/stop`) fail until it is back; a track that is already playing is unaffected, and `/join`,
  `/leave` and `/pause` do not need the database.

## Symptoms and what they mean

Look in the bot's logs (`kubectl -n aha logs aha-0`) and the node's (`kubectl -n aha logs
deploy/aha-lavalink`).

| You see | Meaning | What to do |
|---------|---------|-----------|
| Users get "The audio server isn't reachable" | The node is down or the bot cannot connect. `aha_lavalink_up` is `0`. | Check the node pod (`get pods`, `describe`, its logs), its Secret `aha-lavalink`, and the bot's NetworkPolicy rule to port 2333. The bot reconnects on its own once the node answers. |
| Log: `Loading video X failed: Sign in to confirm you're not a bot (suspicious)` | YouTube is blocking the cluster's address for anonymous requests. | First bump the plugin (below): fixes are released quickly. If it persists the address is flagged: wait, or change egress address. Do **not** add a Google account's cookies or OAuth to the node: the account risks a ban (ADR 0002). |
| Log: `Loading video X failed: ... (common)` for one video | That video is private, removed, age-restricted or region-locked. | Nothing: the user is told it could not be loaded. |
| Log: `Playback failed in guild G for video X: ...` and users see "That track stopped because of a playback problem" | The node could not stream the track. Often the same cause as the sign-in block, or a changed YouTube player. | Same as above: bump the plugin. |
| Many `Playback failed` lines at once | YouTube changed something. `aha_voice_tracks_total{outcome="failed"}` climbs. | Bump the plugin. |
| Log: `Dropping queued video X: voice.load_failed` | A saved queue entry no longer loads; it was removed so the queue keeps moving. | Nothing. |
| The bot sits in a channel and never leaves | The janitor leaves after `VOICE_IDLE_MINUTES` of silence or an empty channel; if it does not, check the `voice-janitor` task in the bot's logs. | Restart the bot as a last resort; the queue survives. |
| Node pod `OOMKilled` | The JVM outgrew its limit (640 MiB; heap capped at 384 MiB). | Check how many guilds were playing; raise the limit only if memory allows (the nodes are tight), or lower `VOICE_MAX_PLAYERS`. |

## Bump the YouTube plugin

YouTube changes break extraction regularly; the fix is a newer `youtube-source` release
(<https://github.com/lavalink-devs/youtube-source/releases>).

1. **Staging first, in its own PR.** In the homelab repo change
   `dev.lavalink.youtube:youtube-plugin:<version>` in `apps/aha/lavalink.yaml`, then run
   `python3 apps/aha-staging/regenerate.py` and commit **only** the regenerated staging files (leave
   `apps/aha/lavalink.yaml` out of this PR, or production would take the new plugin at the same
   time). Merge it.
2. Argo restarts the staging node. The plugin is downloaded from `maven.lavalink.dev` at start, so
   the node needs egress to it (the NetworkPolicy allows public TCP 443). Confirm in the node's log:
   `Loaded 'youtube-plugin-<version>.jar'`.
3. In the test server run `/play` with any video and check that it plays.
4. **Then production, in a second PR** that changes `apps/aha/lavalink.yaml`. Check the log line and
   a `/play` again.

To move Lavalink itself to a newer 4.x, change the image digest (pinned; the 4.2 line has Discord's
required voice encryption). Check the release notes first: a client library may need updating too
(`wavelink` in `pyproject.toml`).

## Turn playback off

- **Lasting:** set `replicas: 0` in `apps/aha/lavalink.yaml` (homelab repo) and merge. The commands
  then answer "The audio server isn't reachable"; everything else is unaffected, and the queue is
  kept. Set it back to `1` to restore. **`kubectl scale` alone does not hold:** Argo CD self-heals the
  Deployment, and on staging it put the node back within 40 seconds (measured 2026-11-23). It is
  fine for a quick restart, not as an off switch.
- **Everywhere:** remove **both** `LAVALINK_URL` and `LAVALINK_PASSWORD_FILE` from the bot's
  environment (the settings go together: one without the other stops the bot from starting). The
  commands then say playback is off.

## Checks after a rollout

1. `aha_lavalink_up` is `1` on the bot (`/metrics`) and the node pod is `Ready`.
2. `aha sync --check` is clean (the voice commands are registered).
3. In a voice channel: `/join`, `/play <a short video>`, `/nowplaying`, `/queue`, `/skip`, `/stop`.
4. Restart the bot pod while a track is queued: after it is back, `/resume` in a voice channel plays
   the saved queue.

## Testing a voice session by hand

A person in the voice channel running `/join`, `/play <link>` and the controls is the real test.
Do **not** drive the bot from a second process that logs in with the same token while the real bot is
running: Discord can deliver the bot's own voice-state events to either session, and a join from the
wrong one waits for an event that never comes (it timed out on roughly half of the attempts when
this was tried on staging, and worked from the same code the rest of the time). Scale the bot to
zero first if a scripted test is really needed. Joins that succeeded in those tests played the video
with the position advancing in step with the clock, and `aha_lavalink_up` stayed `1`.

## Alerts (when monitoring exists)

Not deployed yet (the cluster has no Prometheus). When it does, alert on:

- `aha_lavalink_up == 0` for 5 minutes (only meaningful where `LAVALINK_URL` is set);
- `rate(aha_voice_tracks_total{outcome="failed"}[15m])` above a few per hour, which signals that
  extraction broke;
- `aha_voice_players` close to `VOICE_MAX_PLAYERS`.
