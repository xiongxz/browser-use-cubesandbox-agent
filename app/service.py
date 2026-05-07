from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from browser_use import Agent, Browser, BrowserProfile, ChatOpenAI

from .auth_store import AuthStore
from .config import Settings
from .draft_store import DraftSessionError, DraftSessionStore
from .models import (
    BrowserAgentRunRequest,
    BrowserAgentRunResponse,
    FeishuBitableToFormOutput,
    GenericBrowserTaskOutput,
    StreamEvent,
)
from .prompts import build_task_prompt, effective_allowed_domains


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str | None, limit: int = 280) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


@dataclass
class EventCollector:
    run_id: str
    queue: asyncio.Queue[StreamEvent] | None = None
    events: list[StreamEvent] = field(default_factory=list)

    async def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        stream_event = StreamEvent(
            event=event,
            run_id=self.run_id,
            timestamp=_utc_now(),
            data=data or {},
        )
        self.events.append(stream_event)
        if self.queue is not None:
            await self.queue.put(stream_event)


def _resolve_llm_settings(request: BrowserAgentRunRequest, settings: Settings) -> dict[str, Any]:
    override = request.llm
    base_url = (override.base_url if override and override.base_url else settings.llm_base_url).rstrip("/")
    api_key = override.api_key if override and override.api_key else settings.llm_api_key
    model = override.model if override and override.model else settings.llm_model
    temperature = override.temperature if override and override.temperature is not None else settings.llm_temperature

    if not api_key:
        raise ValueError(
            "No LLM API key configured. Set LLM_API_KEY in the env, "
            "POST llm_api_key (and llm_base_url / llm_model if needed) to /v1/init, "
            "or pass llm.api_key inline in the request."
        )

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "temperature": temperature,
    }


def _build_browser(
    request: BrowserAgentRunRequest,
    settings: Settings,
    run_dir: Path,
    auth_store: AuthStore,
) -> Browser:
    auth = request.auth
    storage_state: str | dict[str, Any] | None = None

    if auth and auth.profile_id:
        profile = auth_store.get_profile(auth.profile_id)
        if profile is None:
            raise ValueError(f"Auth profile not found: {auth.profile_id}")
        storage_state = str(profile.storage_state_path)
    elif request.mode == "feishu_bitable_to_form":
        profile = auth_store.get_profile(settings.feishu_default_profile_id)
        if profile is not None:
            storage_state = str(profile.storage_state_path)
    elif auth and auth.storage_state_path:
        storage_state = auth.storage_state_path
    elif auth and auth.storage_state:
        state_path = run_dir / "storage_state.json"
        state_path.write_text(json.dumps(auth.storage_state, ensure_ascii=False), encoding="utf-8")
        storage_state = str(state_path)

    browser_profile = BrowserProfile(
        headless=settings.browser_headless if request.headless is None else request.headless,
        chromium_sandbox=False,
        window_size={
            "width": settings.browser_window_width,
            "height": settings.browser_window_height,
        },
        allowed_domains=effective_allowed_domains(request) or None,
        storage_state=storage_state,
        user_data_dir=None if storage_state else str(run_dir / "profile"),
        keep_alive=False,
    )
    return Browser(browser_profile=browser_profile)


def _build_response(
    request: BrowserAgentRunRequest,
    run_id: str,
    history: Any,
    duration_sec: float,
) -> BrowserAgentRunResponse:
    structured = None
    if request.mode == "feishu_bitable_to_form":
        structured = history.structured_output or history.get_structured_output(FeishuBitableToFormOutput)
    else:
        structured = history.structured_output or history.get_structured_output(GenericBrowserTaskOutput)

    visited_urls = [url for url in (history.urls() if hasattr(history, "urls") else []) if url]
    screenshots = [path for path in (history.screenshot_paths() if hasattr(history, "screenshot_paths") else []) if path]
    errors = [error for error in (history.errors() if hasattr(history, "errors") else []) if error]
    history_excerpt = [_truncate(item) or "" for item in (history.extracted_content() or []) if item][-8:]
    current_url = visited_urls[-1] if visited_urls else None

    if isinstance(structured, FeishuBitableToFormOutput):
        is_waiting = structured.awaiting_human_confirmation
        return BrowserAgentRunResponse(
            run_id=run_id,
            success=structured.success and bool(structured.form_url) and not is_waiting,
            mode=request.mode,
            final_text=(
                structured.form_url
                or ("Awaiting human confirmation for the draft questionnaire." if is_waiting else None)
                or "; ".join(structured.notes)
                or history.final_result()
            ),
            form_url=structured.form_url,
            form_name=structured.form_name,
            awaiting_human_confirmation=is_waiting,
            draft_questions=structured.draft_questions,
            current_url=current_url,
            visited_urls=visited_urls,
            steps=history.number_of_steps(),
            duration_sec=duration_sec,
            screenshots=screenshots,
            errors=errors,
            notes=structured.notes,
            structured_output=structured.model_dump(),
            history_excerpt=history_excerpt,
        )

    if isinstance(structured, GenericBrowserTaskOutput):
        return BrowserAgentRunResponse(
            run_id=run_id,
            success=structured.success,
            mode=request.mode,
            final_text=structured.final_answer,
            current_url=current_url,
            visited_urls=visited_urls,
            steps=history.number_of_steps(),
            duration_sec=duration_sec,
            screenshots=screenshots,
            errors=errors,
            notes=structured.notes,
            structured_output=structured.model_dump(),
            history_excerpt=history_excerpt,
        )

    return BrowserAgentRunResponse(
        run_id=run_id,
        success=bool(history.is_successful()),
        mode=request.mode,
        final_text=history.final_result(),
        current_url=current_url,
        visited_urls=visited_urls,
        steps=history.number_of_steps(),
        duration_sec=duration_sec,
        screenshots=screenshots,
        errors=errors,
        notes=[],
        structured_output=None,
        history_excerpt=history_excerpt,
    )


async def execute_run(
    request: BrowserAgentRunRequest,
    settings: Settings,
    collector: EventCollector,
    draft_store: DraftSessionStore,
) -> BrowserAgentRunResponse:
    run_dir = settings.browser_artifacts_dir / collector.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    auth_store = AuthStore(settings)

    browser: Browser | None = None
    started = time.perf_counter()
    is_phase_two = (
        request.mode == "feishu_bitable_to_form"
        and bool(request.require_human_confirmation)
        and request.human_confirmation_granted
    )

    try:
        # Phase 2 entry guard: consume the draft session (or refuse fast).
        if is_phase_two:
            assert request.draft_session_id is not None  # validator guarantees this
            assert request.bitable_url is not None
            try:
                await draft_store.consume(
                    session_id=request.draft_session_id,
                    bitable_url=request.bitable_url,
                    profile_id=request.auth.profile_id if request.auth and request.auth.profile_id else None,
                )
            except DraftSessionError as exc:
                duration_sec = round(time.perf_counter() - started, 3)
                logger.warning(
                    "Draft session validation failed for run_id=%s: %s",
                    collector.run_id,
                    exc,
                )
                await collector.emit(
                    "run_failed",
                    {"error": str(exc), "duration_sec": duration_sec, "stage": "draft_session_validation"},
                )
                return BrowserAgentRunResponse(
                    run_id=collector.run_id,
                    success=False,
                    mode=request.mode,
                    final_text=str(exc),
                    current_url=None,
                    visited_urls=[],
                    steps=0,
                    duration_sec=duration_sec,
                    screenshots=[],
                    errors=[str(exc)],
                    notes=["Phase 2 rejected before launching the browser; re-run the draft phase."],
                    structured_output=None,
                    history_excerpt=[],
                )

        llm_settings = _resolve_llm_settings(request, settings)
        llm = ChatOpenAI(
            base_url=llm_settings["base_url"],
            api_key=llm_settings["api_key"],
            model=llm_settings["model"],
            temperature=llm_settings["temperature"],
        )

        await collector.emit(
            "run_started",
            {
                "mode": request.mode,
                "start_url": request.start_url,
                "bitable_url": request.bitable_url,
                "max_steps": request.max_steps,
                "allowed_domains": effective_allowed_domains(request),
                "llm_model": llm_settings["model"],
                "auth_profile_id": request.auth.profile_id if request.auth and request.auth.profile_id else None,
                "feishu_default_profile_id": settings.feishu_default_profile_id if request.mode == "feishu_bitable_to_form" else None,
            },
        )

        browser = _build_browser(request, settings, run_dir, auth_store)
        task = build_task_prompt(request)
        output_model = FeishuBitableToFormOutput if request.mode == "feishu_bitable_to_form" else GenericBrowserTaskOutput

        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
            output_model_schema=output_model,
            sensitive_data=request.auth.sensitive_data if request.auth and request.auth.sensitive_data else None,
            use_vision={
                "auto": "auto",
                "always": True,
                "never": False,
            }[request.use_vision],
            initial_actions=[
                {"navigate": {"url": request.bitable_url or request.start_url}}
            ]
            if (request.bitable_url or request.start_url)
            else None,
        )

        async def on_step_start(hooked_agent: Agent) -> None:
            step_no = hooked_agent.history.number_of_steps() + 1
            await collector.emit(
                "step_start",
                {
                    "step": step_no,
                    "url": hooked_agent.history.urls()[-1] if hooked_agent.history.urls() else None,
                },
            )

        async def on_step_end(hooked_agent: Agent) -> None:
            urls = hooked_agent.history.urls()
            action_names = hooked_agent.history.action_names()
            extracted_content = hooked_agent.history.extracted_content()
            await collector.emit(
                "step_end",
                {
                    "step": hooked_agent.history.number_of_steps(),
                    "url": urls[-1] if urls else None,
                    "last_action": action_names[-1] if action_names else None,
                    "last_excerpt": _truncate(next((item for item in reversed(extracted_content) if item), None)),
                },
            )

        history = await asyncio.wait_for(
            agent.run(
                max_steps=request.max_steps,
                on_step_start=on_step_start,
                on_step_end=on_step_end,
            ),
            timeout=request.timeout_sec,
        )

        duration_sec = round(time.perf_counter() - started, 3)
        response = _build_response(request, collector.run_id, history, duration_sec)

        # Phase 1 exit: register a draft session if the agent paused for human review.
        if (
            request.mode == "feishu_bitable_to_form"
            and not is_phase_two
            and response.awaiting_human_confirmation
            and response.draft_questions
            and request.bitable_url
        ):
            session = await draft_store.create(
                bitable_url=request.bitable_url,
                profile_id=request.auth.profile_id if request.auth and request.auth.profile_id else None,
                draft_questions=[q.model_dump() for q in response.draft_questions],
                form_name=response.form_name,
            )
            response.draft_session_id = session.session_id
            response.draft_session_expires_at = session.expires_at

        await collector.emit(
            "run_completed",
            {
                "success": response.success,
                "final_text": response.final_text,
                "form_url": response.form_url,
                "awaiting_human_confirmation": response.awaiting_human_confirmation,
                "draft_session_id": response.draft_session_id,
                "draft_session_expires_at": response.draft_session_expires_at,
                "current_url": response.current_url,
                "steps": response.steps,
                "duration_sec": response.duration_sec,
            },
        )
        return response

    except Exception as exc:
        duration_sec = round(time.perf_counter() - started, 3)
        logger.exception("Browser run failed for run_id=%s", collector.run_id)
        response = BrowserAgentRunResponse(
            run_id=collector.run_id,
            success=False,
            mode=request.mode,
            final_text=str(exc),
            current_url=None,
            visited_urls=[],
            steps=0,
            duration_sec=duration_sec,
            screenshots=[],
            errors=[str(exc)],
            notes=["Execution failed before a successful final result was produced."],
            structured_output=None,
            history_excerpt=[],
        )
        await collector.emit(
            "run_failed",
            {
                "error": str(exc),
                "duration_sec": duration_sec,
            },
        )
        return response

    finally:
        if browser is not None:
            try:
                await browser.stop()
            except Exception:
                logger.warning("Browser close raised an error for run_id=%s", collector.run_id, exc_info=True)


def make_run_id() -> str:
    return str(uuid.uuid4())
