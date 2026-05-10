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

COPY pyproject.toml /app/

# CubeSandbox template settings for the final pushed app image:
# - Build/push this image as linux/amd64, for example:
#   docker buildx build --platform linux/amd64 -t <registry>/browser-use-cubesandbox-agent:latest --push .
# - Template image: use the final app image above, preferably as image@sha256:...
# - Writable layer size: 2G
# - Expose ports:
#   49983 = envd control plane, required by CubeSandbox
#   49999 = FastAPI app API (/healthz, /v1/init, /v1/agent/run, /v1/agent/stream)
#   60000 = MCP streamable HTTP server, enabled by default
# - Readiness probe: HTTP on port 49983, path /health, startup timeout about 120s
# - Optional template env: set ENABLE_MCP=false only when MCP should be disabled
# - Avoid baking LLM_API_KEY into shared templates; inject it after sandbox startup via POST /v1/init.
#
# Equivalent cubemastercli shape:
#   cubemastercli tpl create-from-image \
#     --image <registry>/browser-use-cubesandbox-agent:latest \
#     --writable-layer-size 2G \
#     --expose-port 49983 \
#     --expose-port 49999 \
#     --expose-port 60000 \
#     --probe 49983 \
#     --probe-path /health
#
# Cache layout:
# - The expensive dependency/build-backend + Chromium layer below depends only on
#   pyproject.toml. Normal app or README changes should not rerun it.
# - After copying app code, install this project with --no-deps so only the
#   lightweight local package layer is rebuilt.
RUN python -c 'import subprocess, sys, tomllib; data = tomllib.load(open("pyproject.toml", "rb")); deps = [*data.get("build-system", {}).get("requires", []), *data["project"]["dependencies"]]; subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", *deps])' \
    && python -m playwright install --with-deps chromium \
    && rm -rf /root/.cache/pip

COPY README.md /app/README.md
COPY app /app/app
COPY mcp_server /app/mcp_server
COPY schemas /app/schemas
COPY .env.example /app/.env.example
COPY agent.build.yaml /app/agent.build.yaml

RUN pip install --no-cache-dir --no-build-isolation --no-deps .

EXPOSE 49983 49999 60000

ENTRYPOINT ["/usr/local/bin/cube-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "49999"]
