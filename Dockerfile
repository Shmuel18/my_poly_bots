# Polybot — reproducible image for the calendar-arbitrage bot + dashboard.
#
# This is OPTIONAL infrastructure. The production deployment today is bare
# systemd on a VPS (see deploy/). This Dockerfile exists so the exact
# runtime (Python 3.10 + pinned deps) is reproducible and portable — useful
# for a future move off the single-VPS SPOF, or for local testing that
# matches prod.
#
# Build:  docker build -t polybot .
# Run:    docker compose up -d        (see docker-compose.yml)
#
# Note: sentence-transformers pulls in torch, so the image is large (~2-3GB).
# That's inherent to the embeddings-based discovery; acceptable for a
# long-running service.

FROM python:3.10-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps for psycopg2 / native wheels. curl is kept for the dashboard
# container's healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cached unless requirements change).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy the application source. data/, logs/, and config/.env are NOT baked
# in — they're mounted at runtime (see .dockerignore + compose volumes) so
# secrets and state never live in the image.
COPY . .

# Run as a non-root user. The mounted data/logs volumes must be writable by
# uid 10001 on the host.
RUN useradd -m -u 10001 polybot \
    && mkdir -p /app/data /app/logs \
    && chown -R polybot:polybot /app
USER polybot

# Default: run the bot in DRY-RUN (no --live). Override the command to add
# --live only when you intend to trade real money. The dashboard service
# overrides this command entirely (see docker-compose.yml).
CMD ["python", "run_calendar_bot.py", "--env", "config/.env", "--use-llm"]
