# Runbook: rotate the Discord bot token

Use this when the token may have leaked, when someone who knew it leaves, or on a schedule.
Rotation takes the bot offline for as long as it takes to roll the pods, because Discord invalidates
the old token the moment a new one is issued.

## How the bot receives its token

| Where | How | Notes |
|-------|-----|-------|
| Local development | `DISCORD_BOT_TOKEN` in `.env` (git-ignored) | |
| Kubernetes | `DISCORD_BOT_TOKEN_FILE=/run/secrets/discord/token`, a mounted Secret | Keeps the token out of `kubectl describe`, crash dumps and `/proc/<pid>/environ`. |

Setting both variables is a startup error. The file's trailing newline is trimmed. The token is
masked as `***` in every log line and traceback, and is never part of a configuration error message.

## Rotate

1. **Reset it in the Developer Portal**: application → Bot → *Reset Token*. Copy the new token once;
   it is shown only once. The old token stops working immediately.
2. **Update the secret** wherever it is stored:
   - local: edit `.env`;
   - cluster: update the encrypted Secret in the deploy repository (the mechanism is chosen in
     #106; prefer a file-mounted Secret managed by SOPS, Sealed Secrets or External Secrets, never
     a hand-run `kubectl create secret`), then let the GitOps controller apply it.
3. **Roll the pods** so they re-read the file (a mounted Secret updates on disk, but the bot reads
   its token only at start): `kubectl rollout restart statefulset/aha`.
4. **Verify**:
   - `kubectl get pods` shows the pods `Ready` (`/readyz` is green once every shard is connected);
   - the logs show `Logged in as ...` and no `LoginFailure` / `Improper token` errors;
   - run `/ping` in a test guild.
5. **Confirm the old token is dead** (optional): a request with it returns `401`.

If the pods crash-loop with `Improper token has been passed`, the Secret holds a stale or mangled
value (a trailing space is trimmed, but a pasted line break inside the token is not). Fix the Secret
and roll again. Do not keep restarting: every start spends part of the daily session-start budget.

## If the token leaked publicly

Discord scans public GitHub and **resets tokens it finds**, which takes the bot offline without
warning. If the bot suddenly cannot log in, treat the token as leaked:

1. Rotate as above immediately.
2. Remove it from git history if it was committed (rewriting history is not enough on its own: assume
   it was read), and look for where else it was pasted (CI logs, tickets, chat).
3. Check the Developer Portal audit information and the guilds for activity you did not expect.

`gitleaks` runs in pre-commit and CI and flags realistic bot tokens, but it is a safety net, not a
reason to paste a token anywhere.

## Staging

The staging application has its own token and its own Secret (#104). Rotate it independently; the
production token is never used outside production.
