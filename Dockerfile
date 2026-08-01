# Dockerfile for hindsight-memorial webhook receiver.
#
# The HTTP layer is stdlib-only; the persistence layer needs PyMySQL, which
# is the single runtime dependency installed below. We start from
# python:3.13-slim and copy the source tree into the image.
#
# After `docker compose up -d --build`, the running container is fully self-
# contained: it does NOT need the host to have the source tree mounted. Logs
# are the only host bind-mount, see docker-compose.yml.
#
# Environment variables (set via compose `environment:` / `env_file:`):
#     HINDSIGHT_API_URL             e.g. http://hindsight:8888 (in compose network)
#     HINDSIGHT_API_KEY             optional, for cloud Hindsight
#     HINDSIGHT_WEBHOOK_SECRET      required, must match Hindsight's webhook config
#     HINDSIGHT_MEMORIAL_LOG_FILE   optional, e.g. /data/logs/hindsight-memorial.log
#     HINDSIGHT_MEMORIAL_LOG_LEVEL  optional, default INFO
#     HINDSIGHT_MYSQL_*             persistence backend; unset → in-memory SQLite
FROM python:3.13-slim

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Installed before the source COPY so the layer is cached across code edits.
# Pinned to the same floor as pyproject.toml's dependency list.
RUN pip install --no-cache-dir "PyMySQL>=1.1.0"

# Copy the full source tree into the image. .dockerignore (if present) keeps
# tests/, .git/, .venv/, etc. out; otherwise the image stays small anyway.
COPY . /app

# Healthcheck against the running server's /healthz. The endpoint returns a
# JSON body of the form {"status": "ok", "pending": N, "processed": N, ...}
# — we parse the status field rather than comparing the raw bytes (the older
# version expected b'ok' which broke when the body became structured).
# MEMORIAL_PORT defaults to 9602; override via build arg if you also change
# the EXPOSE / CMD below.
ARG MEMORIAL_PORT=9602
ENV MEMORIAL_PORT=${MEMORIAL_PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,json,sys,os; \
sys.exit(0 if json.loads(urllib.request.urlopen('http://127.0.0.1:'+os.environ['MEMORIAL_PORT']+'/healthz',timeout=3).read()).get('status')=='ok' else 1)"

EXPOSE 9602

CMD ["python", "-m", "hindsight_memorial.webhook_server", \
     "--host", "0.0.0.0", \
     "--port", "9602"]