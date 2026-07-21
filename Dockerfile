# syntax=docker/dockerfile:1
# Use the official Python image
FROM python:3.14-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
# Precompile dependencies to .pyc at build time so container boots skip
# bytecode compilation (~1.1s saved per cold start)
ENV UV_COMPILE_BYTECODE=1

# Git SHA for Sentry release tracking (passed as build arg)
ARG GIT_SHA=unknown
ENV SENTRY_RELEASE=${GIT_SHA}

# Copy uv binary from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy bun binary from official image
COPY --from=oven/bun:latest /usr/local/bin/bun /usr/local/bin/

# Set the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y libpq-dev gcc redis-tools postgresql-client && rm -rf /var/lib/apt/lists/*

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
RUN mkdir -p static/dist/fonts static/webfonts && \
    cp -r /app/node_modules/@fortawesome/fontawesome-free/webfonts/* static/webfonts/ && \
    cp /app/node_modules/@tabler/icons-webfont/dist/fonts/tabler-icons.woff2 static/dist/fonts/ && \
    cp /app/node_modules/@tabler/icons-webfont/dist/fonts/tabler-icons.woff static/dist/fonts/ && \
    cp /app/node_modules/@tabler/icons-webfont/dist/fonts/tabler-icons.ttf static/dist/fonts/ && \
    bun x tailwindcss -i /app/src/css/main.css -o static/dist/main.css --minify

# Collect static files
RUN uv run --no-dev python manage.py collectstatic --noinput

# Precompile application code to bytecode (deps are handled by UV_COMPILE_BYTECODE)
RUN /app/.venv/bin/python -m compileall -q -x '(\.venv|node_modules)' /app

# Port that the application will use
EXPOSE 8080

# Command to start the server
# Invoke uvicorn from the venv directly: `uv run` re-validates the lockfile and
# environment on every boot, which costs startup time and can mutate the env
# --proxy-headers: Use X-Forwarded-* headers for client IP (from Fly.io/Cloudflare)
# --forwarded-allow-ips='*': Trust proxy headers from any IP (we're behind Fly.io)
CMD ["/app/.venv/bin/uvicorn", "--host", "0.0.0.0", "--port", "8080", "--lifespan", "off", "--proxy-headers", "--forwarded-allow-ips", "*", "django_notipus.asgi:application"]
