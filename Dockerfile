# Dockerfile for hindsight-memorial webhook receiver.
#
# Stdlib-only Python package — no third-party deps to install. We start from
# python:3.13-slim and copy the source tree into the image. The image ends up
# at ~125 MB and rebuilds in seconds because there's no pip-install step.
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
FROM python:3.13-slim

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy the full source tree into the image. .dockerignore (if present) keeps
# tests/, .git/, .venv/, etc. out; otherwise the image stays small anyway.
COPY . /app

# Healthcheck against the running server's /healthz. MEMORIAL_PORT defaults
# to 9602; override via build arg if you also change the EXPOSE / CMD below.
ARG MEMORIAL_PORT=9602
ENV MEMORIAL_PORT=${MEMORIAL_PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,os; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['MEMORIAL_PORT']+'/healthz',timeout=3).read()==b'ok' else 1)"

EXPOSE 9602

CMD ["python", "-m", "hindsight_memorial.webhook_server", \
     "--host", "0.0.0.0", \
     "--port", "9602"]