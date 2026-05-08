FROM python:3.11-bookworm

COPY --from=ghcr.io/tencentcloud/cubesandbox-base:2026.16 /usr/bin/envd /usr/bin/envd
COPY --from=ghcr.io/tencentcloud/cubesandbox-base:2026.16 /usr/local/bin/cube-entrypoint.sh /usr/local/bin/cube-entrypoint.sh

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=49999 \
    HOST=0.0.0.0 \
    ENVD_PORT=49983 \
    BROWSER_HEADLESS=true \
    BROWSER_WINDOW_WIDTH=1440 \
    BROWSER_WINDOW_HEIGHT=900 \
    BROWSER_ARTIFACTS_DIR=/tmp/browser-agent-artifacts

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY app /app/app
COPY schemas /app/schemas
COPY .env.example /app/.env.example
COPY agent.build.yaml /app/agent.build.yaml

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /root/.cache/pip

EXPOSE 49983 49998 49999

ENTRYPOINT ["/usr/local/bin/cube-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "49999"]

