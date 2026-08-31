# syntax=docker/dockerfile:1
# All base images are digest-pinned for supply-chain safety and
# reproducibility; Dependabot (docker ecosystem) keeps the digests fresh.
# The two Chainguard digests must move together: the builder and runtime
# stages must track the same release so the venv built in the builder runs
# on the same interpreter version at runtime.

# Tool images, declared as stages so Dependabot can bump their digests
FROM ghcr.io/astral-sh/uv:latest@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv
FROM oven/bun:latest@sha256:e10577f0db68676a7024391c6e5cb4b879ebd17188ab750cf10024a6d700e5c4 AS bun

# Build stage: Chainguard's -dev variant includes a shell and apk for build
# tooling.
FROM cgr.dev/chainguard/python:latest-dev@sha256:30cd0d997b48b7bc5c1c0cb2d88a4cd00e35d68c2babd783a08f2c896628223d AS builder

USER root

# Set environment variables
ENV PYTHONUNBUFFERED=1
# Precompile dependencies to .pyc at build time so container boots skip
# bytecode compilation (~1.1s saved per cold start)
ENV UV_COMPILE_BYTECODE=1
# Build the venv against the image's interpreter so its symlinks resolve to
# the same path in the runtime stage
ENV UV_PYTHON=/usr/bin/python

# Copy uv binary from official image
COPY --from=uv /uv /uvx /bin/

# Copy bun binary from official image
COPY --from=bun /usr/local/bin/bun /usr/local/bin/

# Set the working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock /app/

# Install project dependencies (without dev dependencies)
RUN UV_HTTP_TIMEOUT=120 uv sync --frozen --no-dev

# Copy frontend dependency files and install
COPY package.json bun.lock /app/
RUN bun install --frozen-lockfile

# Copy frontend source files
COPY src/ /app/src/
COPY postcss.config.js /app/

# Copy application code
COPY ./app/ .

# Build frontend assets
RUN mkdir -p static/dist/fonts && \
    cp /app/node_modules/@tabler/icons-webfont/dist/fonts/tabler-icons.woff2 static/dist/fonts/ && \
    cp /app/node_modules/@tabler/icons-webfont/dist/fonts/tabler-icons.woff static/dist/fonts/ && \
    cp /app/node_modules/@tabler/icons-webfont/dist/fonts/tabler-icons.ttf static/dist/fonts/ && \
    bun x tailwindcss -i /app/src/css/main.css -o static/dist/main.css --minify

# Collect static files
RUN uv run --no-dev python manage.py collectstatic --noinput

# Precompile application code to bytecode (deps are handled by UV_COMPILE_BYTECODE)
RUN /app/.venv/bin/python -m compileall -q -x '(\.venv|node_modules)' /app

# Drop frontend build inputs so they don't ship in the runtime image
RUN rm -rf /app/node_modules /app/src /app/package.json /app/bun.lock /app/postcss.config.js

# Runtime stage: distroless (no shell, no package manager), runs as nonroot.
# Same Chainguard release as the builder stage above — keep in lockstep.
FROM cgr.dev/chainguard/python:latest@sha256:8e3a8e17c9ab20b463f76f62ace70fe55d9b10217b943b26f5b46422ab4936b6

# Spelled out rather than left to the base image's default: the builder stage
# above switches to root, and a reader (or a scanner) tracking USER through the
# file should not have to know which stage that belonged to. 65532 is the
# nonroot uid this image already runs as, so this changes nothing at runtime.
USER 65532

ENV PYTHONUNBUFFERED=1

# Git SHA for Sentry release tracking (passed as build arg)
ARG GIT_SHA=unknown
ENV SENTRY_RELEASE=${GIT_SHA}

WORKDIR /app
COPY --from=builder /app /app

# Port that the application will use
EXPOSE 8080

# The base image's entrypoint is `python`; reset it so CMD (and
# docker-compose `command:` overrides) execute as-is
ENTRYPOINT []

# Command to start the server
# Invoke uvicorn from the venv directly: `uv run` re-validates the lockfile and
# environment on every boot, which costs startup time and can mutate the env
# --proxy-headers: Use X-Forwarded-* headers for client IP (from Fly.io/Cloudflare)
# --forwarded-allow-ips='*': Trust proxy headers from any IP (we're behind Fly.io)
CMD ["/app/.venv/bin/uvicorn", "--host", "0.0.0.0", "--port", "8080", "--lifespan", "off", "--proxy-headers", "--forwarded-allow-ips", "*", "django_notipus.asgi:application"]
