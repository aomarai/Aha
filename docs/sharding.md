# Sharding

Aha scales by **shard groups** (ADR 0001, section 2): Discord allows one gateway session per shard,
so extra replicas of the same shard cannot help. Each pod owns a fixed, disjoint block of shards.

## Settings

| Variable | Meaning |
|----------|---------|
| `SHARD_COUNT` | Total shards. Unset lets Discord recommend a number and one process then owns all of them: valid for a **single pod only**. |
| `SHARD_IDS` | The shards this process owns, e.g. `0,1,2`. Every id must be in `0..SHARD_COUNT-1` and unique. |
| `SHARDS_PER_POD` + `POD_INDEX` | Derive `SHARD_IDS` from the pod ordinal: pod `i` owns `i*N .. i*N+N-1`, clipped to `SHARD_COUNT` (the last pod takes the remainder). |

`SHARD_IDS` and `SHARDS_PER_POD` are mutually exclusive, and either one requires `SHARD_COUNT`
(discord.py refuses `shard_ids` without `shard_count`). Anything invalid stops the process at
startup with a message naming the variable. The pod logs `Owning shards [0, 1] of 4` when it starts.

## StatefulSet mapping

Run the gateway as a StatefulSet. Kubernetes labels each pod with its ordinal
(`apps.kubernetes.io/pod-index`); expose it through the downward API:

```yaml
env:
  - name: SHARD_COUNT
    value: "4"
  - name: SHARDS_PER_POD
    value: "2"
  - name: POD_INDEX
    valueFrom:
      fieldRef:
        fieldPath: metadata.labels['apps.kubernetes.io/pod-index']
```

With `replicas: 2` that gives pod 0 shards `0,1` and pod 1 shards `2,3`.

## Health and metrics

Readiness (`/readyz`) and `aha_shard_ready` / `aha_gateway_latency_seconds` /
`aha_gateway_reconnects_total` are per owned shard, and each shard has its own disconnect clock: a
shard that is back never hides one that is still down. The watchdog closes the process (exit code
1, so the supervisor restarts it) when **any** owned shard has been down longer than its limit.

## Resharding is stop-the-world

Changing `SHARD_COUNT` changes which guilds each shard serves, and a rolling update can start a pod
with the new assignment while an old pod still owns the same shard, which makes Discord invalidate
one of the two sessions. So a reshard is a planned maintenance:

1. Announce it. Expect a short full outage.
2. Scale the StatefulSet to 0 and wait for every pod to terminate.
3. Change `SHARD_COUNT` (and `replicas` / `SHARDS_PER_POD`).
4. Scale back up, then confirm the `Owning shards` log lines cover `0..SHARD_COUNT-1` exactly once.
5. Run `aha sync` as usual afterwards; command definitions do not depend on shards.

Ordinary releases that keep the same layout roll one pod at a time.

Discord requires sharding once the bot is in 2,500 guilds; plan the reshard well before that.
