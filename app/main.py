from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from .auth_store import AuthStore
from .config import load_settings
from .draft_store import DraftSessionStore
from .models import (
    AuthProfileListResponse,
    AuthProfileSummary,
    AuthProfileUpsertRequest,
    BrowserAgentRunRequest,
    BrowserAgentRunResponse,
    FeishuFormFillPrepareRequest,
    FeishuFormFillSubmitRequest,
    PRESET_FEISHU_FORM_URL,
    RuntimeConfigUpdateRequest,
)
from .runtime_config import RuntimeConfigStore
from .service import EventCollector, execute_run, make_run_id
from .sse import encode_sse


load_dotenv()
_base_settings = load_settings()
runtime_config = RuntimeConfigStore(_base_settings)
auth_store = AuthStore(_base_settings)
draft_store = DraftSessionStore(ttl_sec=_base_settings.draft_session_ttl_sec)

logging.basicConfig(
    level=getattr(logging, _base_settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

def _mcp_enabled() -> bool:
    return os.getenv("ENABLE_MCP", "").strip().lower() in {"1", "true", "yes", "on"}


def _mcp_port() -> int:
    return int(os.getenv("MCP_PORT", "60000"))


def _start_mcp_thread() -> None:
    import threading

    def runner() -> None:
        import asyncio

        from mcp_server.server import build_server

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = build_server()
        logger.info(
            "MCP server thread starting on http://%s:%s%s",
            server.settings.host,
            server.settings.port,
            server.settings.streamable_http_path,
        )
        try:
            loop.run_until_complete(server.run_streamable_http_async())
        except Exception:
            logger.exception("MCP server thread crashed")

    threading.Thread(target=runner, daemon=True, name="mcp-server").start()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if _mcp_enabled():
        _start_mcp_thread()
    yield


app = FastAPI(
    title="Browser Use CubeSandbox Agent",
    version="0.1.0",
    description=(
        "Browser Use based agent service with JSON and SSE APIs. "
        "Supports runtime config injection via /v1/init when env vars cannot be "
        "passed at sandbox creation time."
    ),
    lifespan=_lifespan,
)


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".healthz_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _llm_auth_probe_sync(base_url: str, api_key: str) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"ok": True, "status": response.status, "url": url, "interpretation": "auth_ok"}
    except urllib.error.HTTPError as exc:
        body_excerpt = exc.read(200).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        interpretation = {
            401: "auth_failed",
            403: "auth_forbidden",
            404: "endpoint_missing_but_url_reachable",
        }.get(exc.code, "http_error")
        return {
            "ok": exc.code == 200,
            "status": exc.code,
            "url": url,
            "reason": (exc.reason or "")[:200],
            "body_excerpt": body_excerpt,
            "interpretation": interpretation,
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "url": url,
            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            "interpretation": "unreachable",
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            "interpretation": "error",
        }


async def _llm_auth_probe(base_url: str | None, api_key: str | None) -> dict[str, Any]:
    if not base_url or not api_key:
        return {"ok": False, "skipped": True, "reason": "llm_base_url or llm_api_key not set"}
    return await asyncio.get_running_loop().run_in_executor(
        None, _llm_auth_probe_sync, base_url, api_key
    )


@app.get("/")
async def root() -> JSONResponse:
    s = runtime_config.settings
    endpoints = [
        "GET /healthz",
        "GET /healthz?probe=auth",
        "POST /v1/init",
        "GET /v1/auth/storage-state",
        "POST /v1/auth/storage-state",
        "GET /v1/auth/storage-state/{profile_id}",
        "POST /v1/agent/run",
        "POST /v1/agent/stream",
        "POST /v1/feishu/form-fill/prepare",
        "POST /v1/feishu/form-fill/submit",
    ]
    ports: dict[str, Any] = {"envd": 49983, "app": s.port}
    if _mcp_enabled():
        ports["mcp"] = _mcp_port()
        endpoints.append(f"POST http://<host>:{_mcp_port()}/mcp (Model Context Protocol streamable HTTP, separate port)")
    return JSONResponse(
        {
            "name": "browser-use-cubesandbox-agent",
            "version": "0.1.0",
            "ports": ports,
            "mcp_enabled": _mcp_enabled(),
            "endpoints": endpoints,
        }
    )


@app.get("/healthz")
async def healthz(
    probe: str | None = Query(
        default=None,
        description="Set to 'auth' to call <llm_base_url>/models with the configured key (validates URL+key, costs one upstream request).",
    ),
) -> JSONResponse:
    s = runtime_config.settings
    snapshot = runtime_config.snapshot()

    checks: dict[str, Any] = {
        "llm_api_key_set": bool(s.llm_api_key),
        "llm_base_url_set": bool(s.llm_base_url),
        "llm_model_set": bool(s.llm_model),
        "browser_artifacts_dir_writable": _is_writable(s.browser_artifacts_dir),
        "auth_state_dir_writable": _is_writable(s.auth_state_dir),
    }

    if probe == "auth":
        checks["llm_auth_probe"] = await _llm_auth_probe(s.llm_base_url, s.llm_api_key)

    llm_ready = checks["llm_api_key_set"] and checks["llm_base_url_set"] and checks["llm_model_set"]
    fs_ready = checks["browser_artifacts_dir_writable"] and checks["auth_state_dir_writable"]
    if not llm_ready:
        status = "needs_init"
    elif not fs_ready:
        status = "degraded"
    else:
        status = "ok"

    auth_profiles_count = 0
    try:
        auth_profiles_count = len(auth_store.list_profiles())
    except Exception:
        auth_profiles_count = -1

    return JSONResponse(
        {
            "status": status,
            "initialized_at": runtime_config.initialized_at,
            "initialized_keys": runtime_config.initialized_keys,
            "runtime_config": snapshot,
            "checks": checks,
            "auth_profiles_count": auth_profiles_count,
        }
    )


@app.post("/v1/init")
async def init_runtime_config(request: RuntimeConfigUpdateRequest) -> JSONResponse:
    payload = request.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="empty payload; provide at least one config field")
    try:
        snapshot = await runtime_config.apply(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Runtime config updated; keys=%s initialized_at=%s",
        runtime_config.initialized_keys,
        runtime_config.initialized_at,
    )
    return JSONResponse(
        {
            "ok": True,
            "initialized_at": runtime_config.initialized_at,
            "initialized_keys": runtime_config.initialized_keys,
            "runtime_config": snapshot,
        }
    )


@app.get("/v1/auth/storage-state", response_model=AuthProfileListResponse)
async def list_auth_profiles() -> AuthProfileListResponse:
    return AuthProfileListResponse(items=[record.to_summary() for record in auth_store.list_profiles()])


@app.post("/v1/auth/storage-state", response_model=AuthProfileSummary)
async def upsert_auth_profile(request: AuthProfileUpsertRequest) -> AuthProfileSummary:
    record = auth_store.upsert_profile(request)
    return record.to_summary()


@app.get("/v1/auth/storage-state/{profile_id}", response_model=AuthProfileSummary)
async def get_auth_profile(profile_id: str) -> AuthProfileSummary:
    record = auth_store.get_profile(profile_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {profile_id}")
    return record.to_summary()


@app.post("/v1/agent/run", response_model=BrowserAgentRunResponse)
async def run_agent(request: BrowserAgentRunRequest) -> BrowserAgentRunResponse:
    collector = EventCollector(run_id=make_run_id())
    return await execute_run(request, runtime_config.settings, collector, draft_store)


@app.post("/v1/feishu/form-fill/prepare", response_model=BrowserAgentRunResponse)
async def prepare_feishu_form_fill(request: FeishuFormFillPrepareRequest) -> BrowserAgentRunResponse:
    collector = EventCollector(run_id=make_run_id())
    run_request = BrowserAgentRunRequest(
        mode="feishu_form_fill",
        query=request.query,
        form_url=PRESET_FEISHU_FORM_URL,
        llm=request.llm,
        require_human_confirmation=True,
        human_confirmation_granted=False,
    )
    return await execute_run(run_request, runtime_config.settings, collector, draft_store)


@app.post("/v1/feishu/form-fill/submit", response_model=BrowserAgentRunResponse)
async def submit_feishu_form_fill(request: FeishuFormFillSubmitRequest) -> BrowserAgentRunResponse:
    collector = EventCollector(run_id=make_run_id())
    run_request = BrowserAgentRunRequest(
        mode="feishu_form_fill",
        query="",
        form_url=PRESET_FEISHU_FORM_URL,
        allowed_domains=request.allowed_domains,
        headless=request.headless,
        max_steps=request.max_steps,
        timeout_sec=request.timeout_sec,
        use_vision=request.use_vision,
        llm=request.llm,
        auth=request.auth,
        require_human_confirmation=True,
        human_confirmation_granted=True,
        human_confirmation_notes=request.human_confirmation_notes,
        draft_session_id=request.draft_session_id,
        confirmed_answers=request.confirmed_answers,
        feishu_field_ids=request.field_ids,
    )
    return await execute_run(run_request, runtime_config.settings, collector, draft_store)


@app.post("/v1/agent/stream")
async def stream_agent(request: BrowserAgentRunRequest) -> StreamingResponse:
    run_id = make_run_id()
    queue: asyncio.Queue = asyncio.Queue()
    collector = EventCollector(run_id=run_id, queue=queue)

    async def runner() -> None:
        await execute_run(request, runtime_config.settings, collector, draft_store)

    async def stream() -> AsyncGenerator[bytes, None]:
        task = asyncio.create_task(runner())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield encode_sse(event)
                    if event.event in {"run_completed", "run_failed"}:
                        break
                except asyncio.TimeoutError:
                    if collector.events:
                        heartbeat = collector.events[-1].model_copy(update={"event": "heartbeat", "data": {}})
                    else:
                        heartbeat = None
                    if heartbeat is not None:
                        yield encode_sse(heartbeat)
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=_base_settings.host, port=_base_settings.port, reload=False)


if __name__ == "__main__":
    main()
