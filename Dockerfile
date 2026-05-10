# Pin mutable tags by digest so BuildKit cache parents do not drift when tags move.
ARG IMAGE_PLATFORM=linux/amd64
FROM --platform=$IMAGE_PLATFORM ghcr.io/tencentcloud/cubesandbox-base@sha256:b83eee5be295b042560229b571e933ec785f055d4b6ecaca795b5c89ba0acd0a AS cubesandbox_base
FROM --platform=$IMAGE_PLATFORM python:3.11-bookworm@sha256:2209d186b561bf8a8298f86e82f2d79cb45fb3b42e89b1e3b2e25329f87d8401

COPY --from=cubesandbox_base /usr/bin/envd /usr/bin/envd
COPY --from=cubesandbox_base /usr/local/bin/cube-entrypoint.sh /usr/local/bin/cube-entrypoint.sh

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
COPY mcp_server /app/mcp_server
COPY schemas /app/schemas
COPY .env.example /app/.env.example
COPY agent.build.yaml /app/agent.build.yaml

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /root/.cache/pip

EXPOSE 49983 49999 60000

ENTRYPOINT ["/usr/local/bin/cube-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "49999"]
