# Releasing

A release is a version bump merged to `main`, then a `vX.Y.Z` tag on that commit. The
[Publish workflow](../.github/workflows/publish.yml) builds the image and pushes it to
`ghcr.io/aomarai/aha`. Nothing else publishes: pull requests never push an image.

## What gets published

| Event | Tags |
|-------|------|
| Push to `main` | `sha-<short>` |
| Push of tag `v1.2.3` | `1.2.3`, `1.2`, `sha-<short>`, `latest` |
| Push of tag `v1.2.3-rc1` | `1.2.3-rc1`, `sha-<short>` (never `latest` or `1.2`) |

`sha-*` and `X.Y.Z` tags are immutable by convention. `X.Y` and `latest` move to the newest
release. **Deployments must reference the image by digest** (`ghcr.io/aomarai/aha@sha256:...`),
which the workflow prints in its run summary; never by `latest`.

Each image has the build identity baked in: `AHA_VERSION` and `AHA_COMMIT` environment variables
(read for the `version` and `commit` log fields and for `/about`), plus the standard
`org.opencontainers.image.*` labels. The image is `linux/amd64` only, with a provenance
attestation and an SBOM.

## Cutting a release

1. In a PR, change `version` in `pyproject.toml` **and** `__version__` in `src/aha/__init__.py`
   (a test fails if they differ), and move the `Unreleased` entries in `CHANGELOG.md` under the
   new version and date.
2. Merge it. CI must be green, including the Image workflow (build, smoke test, vulnerability scan).
3. Tag the merge commit and push the tag:

   ```bash
   git switch main && git pull
   git tag -a v1.2.3 -m "v1.2.3"
   git push origin v1.2.3
   ```

   The workflow refuses to publish if the tag does not equal `v` + `__version__`.
4. Open the workflow run and copy the digest from its summary. Bumping the deployed digest is a
   reviewed change in the deploy manifests (tracked in #105).

Tags are not meant to be moved. If a release is wrong, ship a new patch version.

## First-time setup

The first push creates the package. In the repository's package settings, link it to the
repository and set its visibility (public or private) deliberately; a private package needs an
image pull secret in the cluster.
