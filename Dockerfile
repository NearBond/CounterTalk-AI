# CounterTalk: the API and the built web app in one image. Nothing in it depends on a host:
# DATABASE_URL, PORT, DATA_DIR, DEBUG and SECRET_KEY come from the environment at run time.

# --- Build stage: the web app ---
FROM node:22-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Runtime: Python with ffmpeg ---
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
# Dependencies first, so changing the code doesn't reinstall them.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/ ./
# A `railway up` from Windows uploads every file as 0644, so start.sh would lose its exec bit.
RUN chmod 0755 scripts/start.sh
RUN uv sync --locked --no-dev

COPY --from=web /app/web/dist /app/web/dist

ENV PATH="/app/backend/.venv/bin:$PATH" \
    WEB_DIST_DIR=/app/web/dist

# Runs as root for now: hosts such as Railway mount volumes owned by root, so a non-root user
# couldn't write to /data. Moving to a non-root user is a later item in the plan.
EXPOSE 8000
CMD ["./scripts/start.sh"]
