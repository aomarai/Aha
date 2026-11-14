# Runbook: the gateway session-start budget

Discord lets a bot token open a limited number of gateway sessions per 24 hours
(`session_start_limit.total`, normally 1,000) and, separately, limits how fast shards may identify
(`max_concurrency`: one identify per 5 seconds per bucket, where the bucket is
`shard_id % max_concurrency`). Every pod start, every watchdog restart and every failed resume that
falls back to a fresh session spends one. A crash loop can empty the budget in an hour, and an empty
budget locks the bot out of the gateway until it resets.

## What Aha does about it

- **Logs and metrics at startup**: `Gateway session budget: 997 of 1000 remaining, resets in 81234 s,
  max concurrency 1`, exported as `aha_session_start_remaining`, `aha_session_start_total` and
  `aha_session_start_reset_after_seconds`.
- **Floor guard**: when `remaining` is below `SESSION_START_FLOOR` of `total` (default `0.1`, `0`
  disables it), the pod logs a **CRITICAL** line and sleeps until `reset_after` (plus a margin)
  instead of identifying, then looks again. Kubernetes' restart back-off alone is far too short for this.
  While it sleeps the pod is **not ready** and serves no commands.
- **Cross-pod IDENTIFY lock** (multi-pod only: needs `DATABASE_URL` and an explicit `SHARD_IDS` /
  `SHARDS_PER_POD`): before each shard identifies, the pod takes a PostgreSQL advisory lock for the
  shard's bucket and keeps it for 5 seconds, so pods that start together do not identify at once.
  If the database is unreachable the pod identifies anyway (Discord's own limit is the backstop).
- Alert `AhaSessionBudgetLow` fires when under 20 % remains.

## When the alert fires

1. **Find what is restarting.** `kubectl get pods -l app=aha` (restart counts) and the logs for the
   watchdog's `Gateway has been disconnected for ...; closing the bot` or `Shard N disconnected`.
   `AhaReconnectStorm` usually fires first.
2. **Stop the loop before anything else.** If a bad release is crash-looping, roll it back or scale
   the StatefulSet to 0. Do not keep restarting pods to "see if it helps": each one spends a session.
3. **Check the budget** from the first log line of any start, or the metrics above. Discord's
   `reset_after` says when it refills.
4. **If the budget is exhausted** (pods are waiting with the CRITICAL line): there is nothing to do
   but wait for the reset, or fix the cause so the restarts stop. Do not lower
   `SESSION_START_FLOOR` to force a start; Discord would reject the identify and can invalidate the
   token's sessions. Raising the daily total is a Discord approval (large bots only).
5. **After the reset**, scale back up one pod at a time and watch `/readyz`.

## Resharding and the budget

A reshard (`docs/sharding.md`) identifies every shard once. Check `remaining` before starting one
and do not reshard when it is below roughly twice the shard count.
