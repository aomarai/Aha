# Environments: staging and production

Two Discord applications, so a release can be proven before users see it (#104). They run the **same
image digest**; only configuration differs.

| | Production | Staging |
|---|---|---|
| Discord application | "Aha" | "Aha (staging)" |
| Bot token | its own, sealed in the cluster | its own, sealed in the cluster |
| Where it is installed | every guild | one private test guild |
| `DEV_GUILD_ID` | unset (commands sync globally) | the test guild id (syncs are instant and isolated) |
| Database | its own database and roles | its own database and roles |

Each token is held by whoever administers that application in the Developer Portal; only the
cluster's Sealed Secrets (never CI, never the repository in plaintext) carry them. Rotation:
[docs/runbooks/token-rotation.md](runbooks/token-rotation.md). Never reuse one token for both.

## The release path

1. Build and publish the image (`docs/releasing.md`), copy its digest.
2. Deploy the digest to **staging**: the PreSync Job runs `aha migrate`, the StatefulSet rolls out,
   the PostSync Job runs `aha sync`.
3. **Smoke check** (all three must pass):
   - `aha sync --check` exits 0. It is a dry run that exits **3** if Discord's commands differ from
     the bot's, so a zero means the sync Job really left no diff.
   - `/readyz` of every pod is 200 (all owned shards ready).
   - The bot is in the test guild: `GET /users/@me/guilds` with the staging token lists it (REST,
     no gateway session needed).
4. Only then bump the same digest in production's manifests.

The staging Application, its namespace and secrets belong with the cluster manifests (the homelab
repository), not here; the Aha-side pieces are the flags above and this checklist.
