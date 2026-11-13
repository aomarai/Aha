#!/usr/bin/env bash
# Smoke-test the built runtime image. The image is distroless (no shell), so every check
# overrides the command with the image's own interpreter.
#
# Usage: scripts/smoke_image.sh [IMAGE]   (default: aha:ci)
set -euo pipefail

image="${1:-aha:ci}"
python=/app/.venv/bin/python

fail() {
  echo "SMOKE TEST FAILED: $1" >&2
  exit 1
}

echo "== runs as the unprivileged user"
user="$(docker image inspect "$image" --format '{{.Config.User}}')"
[ "$user" = "65532:65532" ] || fail "expected user 65532:65532, got '$user'"

echo "== the application and its runtime dependencies import"
docker run --rm "$image" "$python" -c \
  "import aha.bot, aha.health, aha.metrics, aha.cmdsync, aiohttp, prometheus_client, discord"

echo "== the entry point reports the packaged version"
version="$(docker run --rm "$image" "$python" -m aha --version)"
[ "$version" = "aha $(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/aha/__init__.py)" ] \
  || fail "unexpected version output: '$version'"

echo "== the sync subcommand is available"
docker run --rm "$image" "$python" -m aha sync --help | grep -q -- "--dry-run" \
  || fail "aha sync --help does not mention --dry-run"

echo "== a missing token fails fast with a clear message (exit code 1)"
set +e
output="$(docker run --rm "$image" 2>&1)"
code=$?
set -e
[ "$code" -eq 1 ] || fail "expected exit code 1 without a token, got $code"
grep -q "DISCORD_BOT_TOKEN is not set" <<<"$output" \
  || fail "missing-token message not found in: $output"

echo "image smoke tests passed"
