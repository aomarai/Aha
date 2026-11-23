# ADR 0002: Voice playback of YouTube links through Lavalink

- **Status:** Accepted
- **Date:** 2026-11-23
- **Tracking:** [#168](https://github.com/aomarai/Aha/issues/168), tracker [#68](https://github.com/aomarai/Aha/issues/68)

Issue references below (`#N`) point at github.com/aomarai/Aha issues. Versions are as of the date
above; check for newer ones when the work starts.

## Context

The owner wants a music player (#68): people paste a YouTube link and the bot plays it in a voice
channel. Today the bot has no voice support at all. It subscribes to the `guilds` intent only, the
image has no audio libraries, and the cluster's NetworkPolicy allows UDP only for DNS.

Three facts shape the decision:

- **YouTube's terms.** The Terms of Service allow access only through YouTube's own player and
  services, and forbid downloading or re-streaming. An audio bot does exactly that. In 2021 Google's
  cease-and-desist letters ended YouTube playback in the two largest Discord music bots (Groovy and
  Rythm). The owner has accepted this for a small private bot (2026-11-23).
- **Extraction breaks.** Whatever fetches the audio has to keep up with YouTube's changes, and
  requests from server addresses increasingly meet "Sign in to confirm you're not a bot". That is
  ongoing upkeep, not a one-off cost.
- **Discord requires end-to-end encrypted voice (DAVE).** Since March 2026, a client without DAVE
  support can't join voice calls, so every component that speaks voice must support it.

## Decision

### 1. Play YouTube links (only)
Sources are YouTube links: `youtube.com/watch`, `youtu.be`, `music.youtube.com` and
`youtube.com/shorts`, checked against an allowlist. Playlists, search, live streams and
age-restricted videos are out of scope at first (#170). Other services (Spotify, SoundCloud) need
their own decision.

### 2. Audio runs in Lavalink, not in the bot
[Lavalink](https://github.com/lavalink-devs/Lavalink) is a standalone, self-hosted audio node (MIT,
free; Java). The bot only sends it instructions ("play this link in this guild") over HTTP and a
WebSocket. Lavalink fetches the audio, transcodes it, and streams it to Discord's voice servers,
including the DAVE encryption.

- **Lavalink 4.2.x:** 4.2.0 is the first release with DAVE support; use the latest 4.2.x.
- **[`youtube-source` plugin](https://github.com/lavalink-devs/youtube-source):** Lavalink's built-in
  YouTube source is deprecated in favour of this plugin. It was at 1.18.2 when this ADR was written.
  Pin its version; bumping it is the usual fix when YouTube breaks extraction.
- **No Google account cookies or OAuth.** An account used for playback risks a ban. If anonymous
  playback gets blocked, that is a new decision.

### 3. Python client: Wavelink, behind an Aha interface
[Wavelink](https://github.com/PythonistaGuild/Wavelink) 3.5.x: it's built for discord.py
(it requires discord.py 2.7 or later), supports DAVE (3.5.1), and ships type information, which
strict mypy needs. Its maintenance has gaps (no release between mid-2024 and April 2026), so cogs
talk to a small `aha.voice.AudioBackend` protocol, never to Wavelink directly.
[Lavalink.py](https://github.com/devoxin/Lavalink.py) (5.11, more frequent releases, also supports
DAVE, but untyped) is the fallback if Wavelink stalls. Swapping clients then touches only the
adapter.

### 4. Where things live
- **Lavalink is one Deployment in the `aha` namespace** (and one in `aha-staging`), with a sealed
  password and the JVM heap capped. It's a shared node for every bot pod.
- **Bot pods still own the voice *session*.** ADR 0001 §2 still holds: the pod that owns a guild's
  shard receives that guild's interactions and voice events, and it forwards the voice credentials
  to Lavalink. Scheduled jobs run on any pod (ADR 0001 §5), so they never touch voice.
- **Queues live in Postgres** (`voice_queue`, #171). Wavelink's in-memory queue is not used, because
  the invariant in AGENTS.md applies: nothing that must survive lives only in process memory.
- **Network:** only the Lavalink pod needs UDP egress to Discord's voice servers, plus HTTPS to
  YouTube. Bot pods need TCP to the Lavalink Service inside the namespace, and no UDP.
- **The bot image is unchanged** apart from the Wavelink dependency: no `ffmpeg`, `libopus`,
  `PyNaCl` or `yt-dlp`.

### 5. Deploys and restarts
- **A bot rollout ends the bot's gateway session, and Discord then drops the bot from the voice
  channel.** Lavalink keeps the player (track and position) for a resume window. The new pod resumes
  its Lavalink session, rejoins the channel and continues the track: a short gap, not a lost queue.
  The Lavalink session id and each guild's channel are kept in Postgres for that. **This has to be
  proven on staging (#172) before it is promised to users.** If it doesn't hold, the fallback is
  that the queue survives and the next `/play` or `/resume` carries on.
- **A Lavalink restart stops playback in every guild.** The bot notices, reconnects, and replays the
  current track from the queue.

### 6. Limits
Every limit is configurable, and the bot refuses over a limit rather than degrading everyone:

- concurrent players per bot pod (default 10);
- track length (default 30 minutes);
- queue length per guild (default 50).

## Alternatives considered

- **Playback inside the bot (discord.py `VoiceClient` + `yt-dlp` + `ffmpeg`).** Simpler, with nothing
  new to run, and lighter on memory, which this cluster is short of. It was rejected because it puts
  transcoding CPU and four native dependencies (`ffmpeg`, `libopus`, `PyNaCl`, `davey`) into every
  gateway pod and the scanned image. It also couples extractor fixes to bot releases, and it cuts the
  audio on every deploy with no way to resume.
- **No YouTube.** It avoids the terms risk entirely. Rejected because the owner wants the feature and
  accepts the risk for a small private bot.
- **A paid or hosted Lavalink.** Rejected: self-hosting is free, and the cluster already runs
  everything else.

## Consequences

- **One more always-on service:** a JVM of roughly 0.5 GiB, even when no one is listening. If memory
  gets tight, scale the Lavalink Deployment to zero; the commands then answer that playback is off.
- **Ongoing upkeep:** bumping the `youtube-source` plugin when YouTube changes, with a runbook (#172).
- **New personal data:** queued tracks and who requested them. They get the usual inventory entry,
  `/forgetme` purge and departed-server purge (#171).
- **Third parties:** `docs/privacy.md` gains YouTube, which receives requests from the cluster's
  address for the videos people ask for.
- **This ADR is superseded, not edited,** if the source, the audio node or the client changes.
