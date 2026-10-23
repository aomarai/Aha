# Dev container image for the Aha Discord bot.
#
# Based on the Microsoft devcontainer base image (ships the non-root `vscode`
# user plus common tooling). uv is copied in from the official distroless image
# and provisions the pinned Python interpreter itself, so no system Python is
# needed in the image.
#
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

# uv version is pinned for reproducible builds; keep it in step with the host
# development environment.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /usr/local/bin/

# UV_LINK_MODE=copy: hardlink/copy into the venv instead of symlinking from the
# cache, which is required when the workspace is a bind mount (cache and venv
# live on different filesystems). UV_COMPILE_BYTECODE speeds up start-up.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
