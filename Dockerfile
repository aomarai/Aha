# syntax=docker/dockerfile:1

# Production image
#
# Two stages: a builder that installs a standalone CPython and resolves runtime-only
# dependencies with uv, and a distroless runtime (no shell, no package manager) that
# holds just that Python and the resulting virtualenv. The Python minor version must
# match `requires-python` in pyproject.toml (and .python-version).
# Ref: https://docs.astral.sh/uv/guides/integration/docker/
#
# Base images are pinned by digest so builds are reproducible and a moved tag cannot
# change what we ship; Dependabot's `docker` ecosystem bumps tag and digest together.

# Same Debian release as the runtime base so the glibc the standalone Python was
# linked against is the one it runs on.
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS builder

# Pinned for reproducible builds; keep in step with .devcontainer/Dockerfile.
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /bin/

# Runtime deps only; bytecode is compiled now because the runtime user cannot
# write it; copy (not hardlink) because the cache mount is a different filesystem.
# Python is uv-managed and lives at a fixed path, because the venv's `python` is a
# symlink into it and the runtime stage must find it at the same location.
ENV UV_NO_DEV=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/opt/python

WORKDIR /app

# The interpreter and the dependencies come first so these layers are cached until
# .python-version, pyproject.toml or uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=.python-version,target=.python-version \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv python install \
    && uv sync --locked --no-install-project --no-editable

# Then the project itself, installed non-editable so only .venv is needed at runtime.
COPY .python-version pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

# Remove everything the bot cannot reach, so it is not there for an attacker either.
# `cpython-3.14-*` is uv's minor-version symlink to the one real install directory.
#  - the interpreter: the `python3.14` binary is statically linked, so the 22MB
#    libpython shared library, headers, pkg-config files, pip, IDLE, pydoc and the
#    Tcl/Tk GUI libraries are unused;
#  - the stdlib: test suite, unittest, REPL, docs, sqlite3/dbm/curses, venv, GUI
#    toolkits and demos; the venv has its own site-packages, so the base one goes too;
#  - the venv: C/Cython sources, headers, type stubs and test directories shipped
#    inside wheels.
RUN cd /opt/python/cpython-3.14-* \
    && rm -rf include share lib/libpython* lib/pkgconfig lib/tcl* lib/tk* lib/libtcl* lib/libtk* \
        lib/itcl* lib/tdbc* lib/thread* \
        bin/pip* bin/idle* bin/pydoc* bin/*-config \
    && cd lib/python3.14 \
    && rm -rf test idlelib tkinter turtledemo ensurepip pydoc_data unittest _pyrepl sqlite3 dbm \
        curses venv site-packages/* turtle.py pydoc.py doctest.py zipapp.py pickletools.py \
        lib-dynload/_tkinter* lib-dynload/_dbm* \
    && find /app/.venv \( -name '*.pyx' -o -name '*.pxd' -o -name '*.c' -o -name '*.h' \
        -o -name '*.pyi' -o -name tests -o -name test \) -prune -exec rm -rf {} +

# libgcc_s is the only shared library beyond glibc that the standalone Python links
# against. `cp --parents` keeps the architecture-specific path (/usr/lib/<triplet>/),
# which the dynamic loader searches. (frozenlist's optional C extension would also
# need libstdc++; it is deliberately not shipped, and frozenlist falls back to its
# pure-Python implementation.)
RUN mkdir /stage && cp --parents /usr/lib/*-linux-gnu/libgcc_s.so.1 /stage


# `base-nossl`: just glibc, CA certificates (for the Discord TLS connection) and
# tzdata. No libssl (this Python statically links its own OpenSSL) and no libstdc++.
# The one extra library the interpreter needs, libgcc_s, is copied from the builder
# below. The `nonroot` tag runs as UID/GID 65532.
FROM gcr.io/distroless/base-nossl-debian13:nonroot@sha256:6a2a3346335b94d3be0732472245f42ca3ce3cc1135a9521a2bda4b677f2ef0d AS runtime

# Root-owned and read-only to the bot: it never needs to modify its own code.
COPY --from=builder /stage/ /
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv

# PYTHONUNBUFFERED: flush stdout/stderr immediately, even for non-logging writes.
# PYTHONDONTWRITEBYTECODE: bytecode was precompiled; the user cannot write it anyway.
# PYTHONSAFEPATH: do not put the working directory on sys.path.
# PYTHONNOUSERSITE: ignore any per-user site-packages.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONNOUSERSITE=1

WORKDIR /app
# Numeric so Kubernetes `runAsNonRoot` can verify it.
USER 65532:65532

# Exec form (distroless has no shell) with the venv's python (not `uv run`), so
# Python is PID 1 and receives SIGTERM directly; the bot closes cleanly on it.
# Configure via environment variables (DISCORD_BOT_TOKEN, ...); no .env file is
# baked into the image.
CMD ["/app/.venv/bin/python", "-m", "aha"]
