from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import uuid4

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
    FeishuFormAnswerOverride,
    FeishuFormFillRunRequest,
    FormFillRunInputRequest,
    FormFillUserIntent,
    GatewayReplyPayload,
    GatewayReplySummaryItem,
    PRESET_FEISHU_FORM_URL,
    RuntimeConfigUpdateRequest,
    StreamEvent,
)
from .runtime_config import RuntimeConfigStore
from .run_store import FormFillRunState, FormFillRunStore
from .service import EventCollector, classify_form_fill_user_intent, execute_run, make_run_id
from .sse import encode_sse


load_dotenv()
_base_settings = load_settings()
runtime_config = RuntimeConfigStore(_base_settings)
auth_store = AuthStore(_base_settings)
draft_store = DraftSessionStore(ttl_sec=_base_settings.draft_session_ttl_sec)
form_fill_runs = FormFillRunStore()

logging.basicConfig(
    level=getattr(logging, _base_settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

def _mcp_enabled() -> bool:
    raw = os.getenv("ENABLE_MCP")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse(run_id: str, event: str, data: dict[str, Any]) -> bytes:
    return encode_sse(StreamEvent(event=event, run_id=run_id, timestamp=_now_iso(), data=data))


def _public_payload(payload: GatewayReplyPayload | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    return payload.model_dump(exclude_none=True)


def _response_public_data(response: Any) -> dict[str, Any]:
    return response.model_dump(
        exclude={
            "draft_session_id",
            "draft_session_expires_at",
            "draft_questions",
            "draft_answers",
            "structured_output",
        },
        exclude_none=True,
    )


def _payload_with_field_updates(
    payload: GatewayReplyPayload | None,
    updates: dict[str, Any],
) -> GatewayReplyPayload | None:
    if payload is None or not updates:
        return payload

    summary: list[GatewayReplySummaryItem] = []
    for item in payload.summary:
        if item.id in updates:
            summary.append(item.model_copy(update={"value": updates[item.id]}))
        else:
            summary.append(item)

    return payload.model_copy(
        update={
            "text": "已应用你的修改，请再次确认是否提交。",
            "summary": summary,
        }
    )


def _overrides_from_fields(fields: dict[str, Any]) -> list[FeishuFormAnswerOverride]:
    overrides: list[FeishuFormAnswerOverride] = []
    for field_key, value in fields.items():
        if value is None:
            overrides.append(FeishuFormAnswerOverride(field_key=field_key, clear_value=True))
            continue
        overrides.append(FeishuFormAnswerOverride(field_key=field_key, confirmed_value=str(value)))
    return overrides


def _content_decision(request: FormFillRunInputRequest) -> str:
    content = request.content or {}
    raw = content.get("decision")
    if raw is None:
        if request.action == "accept":
            raw = "confirm"
        elif request.action in {"cancel", "decline"}:
            raw = "cancel"
        else:
            raw = "message"
    decision = str(raw).strip().lower()
    aliases = {
        "submit": "confirm",
        "approve": "confirm",
        "ok": "confirm",
        "reject": "cancel",
        "decline": "cancel",
    }
    return aliases.get(decision, decision)


def _content_fields(request: FormFillRunInputRequest) -> dict[str, Any]:
    content = request.content or {}
    raw = content.get("fields")
    return raw if isinstance(raw, dict) else {}


def _content_notes(request: FormFillRunInputRequest) -> str | None:
    content = request.content or {}
    for key in ("notes", "free_text", "message", "supplement"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if request.reason and request.reason.strip():
        return request.reason.strip()
    return None


def _content_text(request: FormFillRunInputRequest) -> str | None:
    content = request.content or {}
    for key in ("text", "message", "reply", "utterance"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if request.reason and request.reason.strip() and request.action == "message":
        return request.reason.strip()
    return None


def _heuristic_intent(text: str) -> FormFillUserIntent:
    normalized = text.strip().lower().replace(" ", "")
    confirm_terms = ("没问题", "没错", "对", "可以", "确认", "提交", "ok", "okay", "yes", "yep", "go")
    cancel_terms = ("取消", "算了", "不用", "别提交", "不要", "停止", "no", "stop", "cancel")
    if any(term in normalized for term in cancel_terms):
        return FormFillUserIntent(intent="cancel", confidence="medium", reason="matched cancel phrase")
    if any(term in normalized for term in confirm_terms):
        return FormFillUserIntent(intent="confirm", confidence="medium", reason="matched confirm phrase")
    return FormFillUserIntent(intent="unknown", confidence="low", reason="no clear local match")


async def _interpret_user_input(
    state: FormFillRunState,
    request: FormFillRunInputRequest,
) -> FormFillUserIntent:
    fields = _content_fields(request)
    notes = _content_notes(request)
    decision = _content_decision(request)

    if request.action in {"cancel", "decline"} or decision == "cancel":
        return FormFillUserIntent(intent="cancel", confidence="high", reason=request.reason)
    if fields:
        return FormFillUserIntent(intent="edit", confidence="high", fields=fields, reason="explicit fields")
    if decision == "edit" and notes:
        return FormFillUserIntent(intent="supplement", confidence="high", supplement=notes, reason="explicit supplement")
    if decision == "confirm" and not _content_text(request):
        return FormFillUserIntent(intent="confirm", confidence="high", reason="explicit confirm")

    text = _content_text(request) or notes
    if not text:
        return FormFillUserIntent(intent="unknown", confidence="low", reason="empty user response")

    try:
        intent = await classify_form_fill_user_intent(
            text,
            settings=runtime_config.settings,
            llm=state.request.llm,
            original_query=state.request.query,
            current_query=state.current_query,
            payload=state.payload,
        )
    except Exception:
        logger.warning("LLM intent classification failed; using heuristic fallback", exc_info=True)
        intent = _heuristic_intent(text)

    if intent.intent in {"edit", "supplement"} and not intent.fields and not intent.supplement:
        intent.supplement = text
    return intent


def _clarification_payload(payload: GatewayReplyPayload | None, user_text: str | None) -> GatewayReplyPayload:
    base = payload or GatewayReplyPayload(
        kind="ask_user",
        title="请确认下一步",
        text="我还不能确定你的意思，请确认是否继续提交。",
    )
    suffix = f"（你的回复：{user_text}）" if user_text else ""
    return base.model_copy(update={"text": f"我还不能确定你的意思{suffix}，请确认是提交、修改还是取消。"})


def _stream_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


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
        "POST /v1/feishu/form-fill/run (segmented SSE)",
        "POST /v1/feishu/form-fill/runs/{run_id}/input (segmented SSE)",
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


def _build_prepare_request(request: FeishuFormFillRunRequest, query: str) -> BrowserAgentRunRequest:
    return BrowserAgentRunRequest(
        mode="feishu_form_fill",
        query=query,
        form_url=request.form_url or PRESET_FEISHU_FORM_URL,
        allowed_domains=request.allowed_domains,
        headless=request.headless,
        max_steps=request.max_steps,
        timeout_sec=request.timeout_sec,
        use_vision=request.use_vision,
        llm=request.llm,
        auth=request.auth,
        require_human_confirmation=True,
        human_confirmation_granted=False,
        feishu_field_ids=request.field_ids,
    )


def _build_submit_request(
    request: FeishuFormFillRunRequest,
    draft_session_id: str,
    confirmed_answers: list[FeishuFormAnswerOverride],
    notes: str | None,
) -> BrowserAgentRunRequest:
    return BrowserAgentRunRequest(
        mode="feishu_form_fill",
        query="",
        form_url=request.form_url or PRESET_FEISHU_FORM_URL,
        allowed_domains=request.allowed_domains,
        headless=request.headless,
        max_steps=request.max_steps,
        timeout_sec=request.timeout_sec,
        use_vision=request.use_vision,
        llm=request.llm,
        auth=request.auth,
        require_human_confirmation=True,
        human_confirmation_granted=True,
        human_confirmation_notes=notes,
        draft_session_id=draft_session_id,
        confirmed_answers=confirmed_answers,
        feishu_field_ids=request.field_ids,
    )


async def _emit_ask_user_question(run_id: str, state: FormFillRunState) -> bytes:
    question_id = str(uuid4())
    await form_fill_runs.update(
        run_id,
        status="awaiting_user",
        current_question_id=question_id,
    )
    return _sse(
        run_id,
        "ask_user_question",
        {
            "status": "awaiting_user",
            "question_id": question_id,
            "input_url": f"/v1/feishu/form-fill/runs/{run_id}/input",
            "expires_at": state.draft_session_expires_at,
            "payload": _public_payload(state.payload),
            "stream_closed": True,
        },
    )


async def _stream_draft_until_question(
    run_id: str,
    state: FormFillRunState,
) -> AsyncGenerator[bytes, None]:
    await form_fill_runs.update(run_id, status="running", current_question_id=None)
    yield _sse(run_id, "phase_started", {"phase": "draft", "message": "正在生成待确认的表单答案。"})
    prepare_response = await execute_run(
        _build_prepare_request(state.request, state.current_query),
        runtime_config.settings,
        EventCollector(run_id=run_id),
        draft_store,
    )

    if (
        not prepare_response.awaiting_human_confirmation
        or not prepare_response.draft_session_id
        or prepare_response.payload is None
    ):
        await form_fill_runs.update(run_id, status="failed", current_question_id=None)
        yield _sse(
            run_id,
            "run_failed",
            {
                "status": "failed",
                "message": "未能生成可供用户确认的表单草稿。",
                "result": _response_public_data(prepare_response),
            },
        )
        await form_fill_runs.remove(run_id)
        return

    await form_fill_runs.update(
        run_id,
        draft_session_id=prepare_response.draft_session_id,
        draft_session_expires_at=prepare_response.draft_session_expires_at,
        payload=prepare_response.payload,
    )
    refreshed = await form_fill_runs.get(run_id)
    assert refreshed is not None
    yield await _emit_ask_user_question(run_id, refreshed)


async def _stream_submit(
    run_id: str,
    state: FormFillRunState,
    notes: str | None,
) -> AsyncGenerator[bytes, None]:
    if not state.draft_session_id:
        yield _sse(run_id, "run_failed", {"status": "failed", "message": "缺少 draft_session_id，无法提交。"})
        await form_fill_runs.remove(run_id)
        return

    queue: asyncio.Queue = asyncio.Queue()
    collector = EventCollector(run_id=run_id, queue=queue)
    submit_request = _build_submit_request(
        state.request,
        state.draft_session_id,
        list(state.confirmed_by_key.values()),
        notes,
    )

    async def runner() -> None:
        await execute_run(submit_request, runtime_config.settings, collector, draft_store)

    task = asyncio.create_task(runner())
    try:
        await form_fill_runs.update(run_id, status="running", current_question_id=None)
        yield _sse(run_id, "phase_completed", {"phase": "draft", "message": "表单答案已确认。"})
        yield _sse(run_id, "phase_started", {"phase": "submit", "message": "开始打开浏览器并提交飞书表单。"})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                yield encode_sse(event)
                if event.event in {"run_completed", "run_failed"}:
                    break
            except asyncio.TimeoutError:
                yield _sse(run_id, "heartbeat", {})
        await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await form_fill_runs.remove(run_id)


@app.post("/v1/feishu/form-fill/run")
async def stream_feishu_form_fill(request: FeishuFormFillRunRequest) -> StreamingResponse:
    run_id = make_run_id()
    state = await form_fill_runs.create(run_id, request)

    async def stream() -> AsyncGenerator[bytes, None]:
        yield _sse(
            run_id,
            "run_started",
            {
                "mode": "feishu_form_fill",
                "form_url": request.form_url or PRESET_FEISHU_FORM_URL,
                "message": "开始解析待填表单信息。",
            },
        )
        async for chunk in _stream_draft_until_question(run_id, state):
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_stream_headers())


@app.post("/v1/feishu/form-fill/runs/{run_id}/input")
async def stream_form_fill_run_input(run_id: str, request: FormFillRunInputRequest) -> StreamingResponse:
    try:
        state = await form_fill_runs.validate_input(run_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}") from exc
    except TimeoutError as exc:
        await form_fill_runs.remove(run_id)
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def stream() -> AsyncGenerator[bytes, None]:
        intent = await _interpret_user_input(state, request)
        user_text = _content_text(request) or _content_notes(request)
        yield _sse(
            run_id,
            "user_response_received",
            {
                "question_id": request.question_id,
                "intent": intent.model_dump(exclude_none=True),
                "message": "已理解用户回复，继续处理。",
            },
        )

        if intent.intent == "cancel" and intent.confidence != "low":
            await form_fill_runs.update(run_id, status="cancelled", current_question_id=None)
            yield _sse(
                run_id,
                "run_cancelled",
                {"status": "cancelled", "message": intent.reason or request.reason or "用户取消了本次表单提交。"},
            )
            await form_fill_runs.remove(run_id)
            return

        if intent.intent == "confirm" and intent.confidence != "low":
            refreshed = await form_fill_runs.get(run_id)
            assert refreshed is not None
            async for chunk in _stream_submit(run_id, refreshed, _content_notes(request)):
                yield chunk
            return

        if intent.intent == "edit" and intent.fields:
            confirmed_by_key = dict(state.confirmed_by_key)
            for override in _overrides_from_fields(intent.fields):
                key = override.field_key or override.field_label or str(override.index)
                confirmed_by_key[key] = override
            payload = _payload_with_field_updates(state.payload, intent.fields)
            await form_fill_runs.update(
                run_id,
                confirmed_by_key=confirmed_by_key,
                payload=payload,
            )
            refreshed = await form_fill_runs.get(run_id)
            assert refreshed is not None
            yield await _emit_ask_user_question(run_id, refreshed)
            return

        if intent.intent in {"edit", "supplement"} and (intent.supplement or user_text):
            supplement = intent.supplement or user_text or ""
            next_query = f"{state.current_query}\n用户补充：{supplement}"
            await form_fill_runs.update(
                run_id,
                current_query=next_query,
                confirmed_by_key={},
                current_question_id=None,
            )
            refreshed = await form_fill_runs.get(run_id)
            assert refreshed is not None
            async for chunk in _stream_draft_until_question(run_id, refreshed):
                yield chunk
            return

        payload = _clarification_payload(state.payload, user_text)
        await form_fill_runs.update(run_id, payload=payload)
        refreshed = await form_fill_runs.get(run_id)
        assert refreshed is not None
        yield await _emit_ask_user_question(run_id, refreshed)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_stream_headers())


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=_base_settings.host, port=_base_settings.port, reload=False)


if __name__ == "__main__":
    main()
