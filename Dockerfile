# Dockerfile for hindsight-memorial webhook receiver.
#
# Stdlib-only Python package — no third-party deps to install. We start from
# python:3.13-slim and just add the source tree. The image is small (~120 MB)
# and rebuilds in seconds because pip-install is a no-op.
#
# Usage from the parent docker-compose.yml:
#     build: .
#     volumes:
#       - ./app:/app:ro      # mount the source tree read-only for hot reloads
#
# Environment variables (set via compose `environment:` / `env_file:`):
#     HINDSIGHT_API_URL             e.g. http://hindsight:8888 (in compose network)
#     HINDSIGHT_API_KEY             optional, for cloud Hindsight
#     HINDSIGHT_WEBHOOK_SECRET      required, must match Hindsight's webhook config
#     HINDSIGHT_MEMORIAL_LOG_FILE   optional, e.g. /data/logs/hindsight-memorial.log
#     HINDSIGHT_MEMORIAL_LOG_LEVEL  optional, default INFO
FROM python:3.13-slim

# No pip install needed — the package is stdlib-only and mounted read-only
# from the host at /app (see compose volumes). PYTHONPATH points there.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Healthcheck: GET /healthz on the same port the server binds to.
# Note: the actual port comes from HINDSIGHT_MEMORIAL_PORT (default 9602);
# HEALTHCHECK_PORT mirrors it via build arg so the dockerfile stays
# configuration-light. Override at build time:
#   docker build --build-arg MEMORIAL_PORT=9700 .
ARG MEMORIAL_PORT=9602
ENV MEMORIAL_PORT=${MEMORIAL_PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ['MEMORIAL_PORT']+'/healthz',timeout=3).read()==b'ok' else 1)"

EXPOSE 9602

CMD ["python", "-m", "hindsight_memorial.webhook_server", \
     "--host", "0.0.0.0", \
     "--port", "9602"]