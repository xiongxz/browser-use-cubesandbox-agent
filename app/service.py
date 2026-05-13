from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from browser_use import Agent, Browser, BrowserProfile, ChatOpenAI, Controller
from browser_use.agent.views import ActionResult
from browser_use.llm.messages import SystemMessage, UserMessage

from .auth_store import AuthStore
from .config import Settings
from .draft_store import DraftSessionError, DraftSessionStore
from .feishu_form_fill import PRESET_FEISHU_FORM_FIELD_IDS, display_time_to_timestamp_ms, parse_form_fill_query
from .models import (
    BrowserAgentRunRequest,
    BrowserAgentRunResponse,
    FeishuFormAnswerDraft,
    FeishuFormFillExtraction,
    FeishuFormAnswerOverride,
    FeishuFormFillOutput,
    FeishuBitableToFormOutput,
    GenericBrowserTaskOutput,
    GatewayReplyField,
    GatewayReplyOption,
    GatewayReplyPayload,
    GatewayReplyQuestion,
    GatewayReplySummaryItem,
    PRESET_FEISHU_FORM_URL,
    StreamEvent,
)
from .prompts import build_task_prompt, effective_allowed_domains


logger = logging.getLogger(__name__)


def _configure_browser_use_start_timeout(settings: Settings) -> None:
    timeout = max(float(settings.browser_start_timeout_sec), 1.0)
    timeout_text = str(timeout)

    # browser-use reads these when BrowserStartEvent / BrowserLaunchEvent are
    # instantiated. Keep both in sync so the outer lifecycle event and the
    # nested local launch event share the same budget.
    os.environ["TIMEOUT_BrowserStartEvent"] = timeout_text
    os.environ["TIMEOUT_BrowserLaunchEvent"] = timeout_text

    # LocalBrowserWatchdog._wait_for_cdp_url has its own default timeout=30,
    # and BrowserLaunchEvent calls it without passing a timeout. Patch the
    # default through our app layer instead of modifying site-packages.
    from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog

    original_attr = "_goclaw_original_wait_for_cdp_url"
    if hasattr(LocalBrowserWatchdog, original_attr):
        return

    original_wait_for_cdp_url = LocalBrowserWatchdog._wait_for_cdp_url
    setattr(LocalBrowserWatchdog, original_attr, original_wait_for_cdp_url)

    async def wait_for_cdp_url_with_configured_timeout(port: int, timeout: float | None = None) -> str:
        effective_timeout = timeout if timeout is not None else float(os.environ["TIMEOUT_BrowserLaunchEvent"])
        return await original_wait_for_cdp_url(port, timeout=effective_timeout)

    LocalBrowserWatchdog._wait_for_cdp_url = staticmethod(wait_for_cdp_url_with_configured_timeout)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str | None, limit: int = 280) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _merge_confirmed_answers(
    drafted: list[dict[str, Any]],
    overrides: list[FeishuFormAnswerOverride],
) -> list[FeishuFormAnswerOverride]:
    merged_by_index: dict[int, FeishuFormAnswerOverride] = {}
    merged_by_key: dict[str, FeishuFormAnswerOverride] = {}
    merged_by_label: dict[str, FeishuFormAnswerOverride] = {}

    for item in drafted:
        confirmed_value = item.get("proposed_value")
        normalized_values = [str(v) for v in (item.get("normalized_values") or []) if str(v).strip()]
        if confirmed_value is None and not normalized_values:
            continue
        override = FeishuFormAnswerOverride(
            index=item.get("index"),
            field_key=item.get("field_key"),
            field_label=item.get("field_label"),
            confirmed_value=confirmed_value,
            normalized_values=normalized_values,
            clear_value=False,
        )
        if override.index is not None:
            merged_by_index[override.index] = override
        if override.field_key:
            merged_by_key[override.field_key] = override
        if override.field_label:
            merged_by_label[override.field_label] = override

    for override in overrides:
        target: FeishuFormAnswerOverride | None = None
        if override.index is not None:
            target = merged_by_index.get(override.index)
        if target is None and override.field_key:
            target = merged_by_key.get(override.field_key)
        if target is None and override.field_label:
            target = merged_by_label.get(override.field_label)

        updated = override if target is None else target.model_copy(
            update={
                "confirmed_value": override.confirmed_value if override.confirmed_value is not None else target.confirmed_value,
                "normalized_values": override.normalized_values or target.normalized_values,
                "clear_value": override.clear_value,
                "field_key": target.field_key or override.field_key,
                "field_label": target.field_label or override.field_label,
                "index": target.index if target.index is not None else override.index,
            }
        )
        if updated.index is not None:
            merged_by_index[updated.index] = updated
        if updated.field_key:
            merged_by_key[updated.field_key] = updated
        if updated.field_label:
            merged_by_label[updated.field_label] = updated

    ordered = sorted(merged_by_index.values(), key=lambda item: item.index or 0)
    extras = [
        item
        for label, item in merged_by_label.items()
        if item.index is None and label
    ]
    return ordered + sorted(extras, key=lambda item: item.field_label or "")


def _question_type_to_field_type(question_type: str | None) -> str:
    if question_type in {"number", "integer"}:
        return "number"
    if question_type in {"date", "datetime", "timestamp_ms"}:
        return "date"
    return "text"


def _build_confirmation_question(*, confirm_label: str, confirm_description: str) -> GatewayReplyQuestion:
    return GatewayReplyQuestion(
        header="提交确认",
        question="这些信息是否正确？",
        multiSelect=False,
        options=[
            GatewayReplyOption(
                id="confirm",
                label=confirm_label,
                description=confirm_description,
            ),
            GatewayReplyOption(
                id="edit",
                label="我要修改",
                description="补充或更正信息后再确认",
            ),
            GatewayReplyOption(
                id="cancel",
                label="取消",
                description="停止本次操作",
            ),
        ],
    )


def _build_form_fill_ask_user_payload(
    *,
    form_name: str | None,
    draft_answers: list[FeishuFormAnswerDraft],
) -> GatewayReplyPayload:
    missing = [answer for answer in draft_answers if answer.required and not answer.proposed_value]
    summary = [
        GatewayReplySummaryItem(
            id=answer.field_key,
            label=answer.field_label,
            value=answer.proposed_value,
        )
        for answer in draft_answers
    ]
    fields = [
        GatewayReplyField(
            id=answer.field_key,
            label=answer.field_label,
            type=_question_type_to_field_type(answer.question_type),
            required=bool(answer.required),
            placeholder=f"请输入{answer.field_label}",
        )
        for answer in missing
    ]
    return GatewayReplyPayload(
        kind="ask_user",
        title=f"{form_name or '表单'}信息确认",
        text="请补充缺失字段，并确认以下信息是否提交。" if missing else "请确认以下信息是否提交。",
        summary=summary,
        fields=fields,
        questions=[
            _build_confirmation_question(
                confirm_label="确认提交",
                confirm_description="继续填写并提交表单",
            )
        ],
    )


def _build_bitable_ask_user_payload(*, form_name: str | None, draft_questions: list[Any]) -> GatewayReplyPayload:
    summary = [
        GatewayReplySummaryItem(
            id=str(question.index),
            label=f"题目 {question.index}",
            value=question.title,
        )
        for question in draft_questions
    ]
    return GatewayReplyPayload(
        kind="ask_user",
        title=f"{form_name or '问卷'}草稿确认",
        text="请确认以下问卷草稿是否可以发布。",
        summary=summary,
        questions=[
            _build_confirmation_question(
                confirm_label="确认发布",
                confirm_description="继续开启表单分享并返回问卷链接",
            )
        ],
    )


def _build_result_payload(
    *,
    title: str | None,
    text: str,
    summary: list[GatewayReplySummaryItem] | None = None,
) -> GatewayReplyPayload:
    return GatewayReplyPayload(
        kind="result",
        title=title,
        text=text,
        summary=summary or [],
    )


def _looks_like_form_submission_success(*values: str | None) -> bool:
    text = " ".join(value.strip() for value in values if value and value.strip())
    if not text:
        return False
    lowered = text.lower()
    negative_markers = (
        "not submitted",
        "submit failed",
        "submission failed",
        "failed to submit",
        "未提交",
        "提交失败",
        "无法提交",
    )
    if any(marker in lowered for marker in negative_markers):
        return False
    success_markers = (
        "submitted",
        "submission successful",
        "submit success",
        "提交成功",
        "已提交",
        "成功提交",
    )
    return any(marker in lowered for marker in success_markers)


def _confirmed_answer_by_key(
    request: BrowserAgentRunRequest,
    field_key: str,
) -> FeishuFormAnswerOverride | None:
    return next((answer for answer in request.confirmed_answers if answer.field_key == field_key), None)


def _confirmed_answer_text(
    request: BrowserAgentRunRequest,
    field_key: str,
) -> str:
    answer = _confirmed_answer_by_key(request, field_key)
    if answer is None:
        raise ValueError(f"Missing confirmed answer for {field_key}")
    values = answer.normalized_values or ([answer.confirmed_value] if answer.confirmed_value else [])
    value = next((str(item).strip() for item in values if str(item).strip()), "")
    if not value:
        raise ValueError(f"Confirmed answer is empty for {field_key}")
    return value


def _resolve_feishu_form_field_ids(request: BrowserAgentRunRequest) -> dict[str, str]:
    field_ids = {**PRESET_FEISHU_FORM_FIELD_IDS, **request.feishu_field_ids}
    required = ("name", "attendance_time", "attendance_count")
    missing = [key for key in required if not field_ids.get(key)]
    if missing:
        raise ValueError("Missing Feishu form field id mapping for: " + ", ".join(missing))
    return field_ids


def _build_feishu_form_submit_wire_data(request: BrowserAgentRunRequest) -> dict[str, Any]:
    field_ids = _resolve_feishu_form_field_ids(request)
    name = _confirmed_answer_text(request, "name")
    time_answer = _confirmed_answer_by_key(request, "attendance_time")
    if time_answer is None or not time_answer.confirmed_value:
        raise ValueError("Missing confirmed answer for attendance_time")
    timestamp_value = next(
        (
            str(item).strip()
            for item in time_answer.normalized_values
            if str(item).strip().isdigit()
        ),
        "",
    )
    timestamp_ms = int(timestamp_value) if timestamp_value else display_time_to_timestamp_ms(time_answer.confirmed_value)
    attendance_count = int(_confirmed_answer_text(request, "attendance_count"))

    return {
        field_ids["name"]: {
            "type": 1,
            "value": [{"type": "text", "text": name}],
        },
        field_ids["attendance_time"]: {
            "type": 5,
            "value": timestamp_ms,
        },
        field_ids["attendance_count"]: {
            "type": 2,
            "value": attendance_count,
        },
    }


def _build_feishu_form_fill_tools(
    request: BrowserAgentRunRequest,
    output_model: type[FeishuFormFillOutput],
) -> Controller:
    controller = Controller(output_model=output_model)
    wire_data = _build_feishu_form_submit_wire_data(request)

    @controller.action(
        "Install the Feishu form submit payload guard. Call once after opening the preset Feishu form and before clicking submit. "
        "It patches fetch/XMLHttpRequest so the final form submission carries the confirmed field IDs and values."
    )
    async def install_feishu_form_submit_payload_guard(browser_session) -> ActionResult:
        page = await browser_session.must_get_current_page()
        result = await page.evaluate(
            """(wireData) => {
                const guardKey = '__browserUseFeishuFormFillPayloadGuard';
                window[guardKey] = { wireData, installedAt: Date.now() };

                if (window.__browserUseFeishuFormFillPayloadGuardInstalled) {
                    return { installed: true, alreadyInstalled: true, wireData };
                }
                window.__browserUseFeishuFormFillPayloadGuardInstalled = true;

                function patchObject(obj) {
                    let changed = false;
                    if (!obj || typeof obj !== 'object') return false;

                    if (typeof obj.data === 'string') {
                        try {
                            const parsedData = JSON.parse(obj.data);
                            Object.assign(parsedData, wireData);
                            obj.data = JSON.stringify(parsedData);
                            changed = true;
                        } catch (error) {
                            // Not the Feishu form payload shape.
                        }
                    } else if (obj.data && typeof obj.data === 'object') {
                        Object.assign(obj.data, wireData);
                        changed = true;
                    }

                    for (const key of Object.keys(obj)) {
                        const value = obj[key];
                        if (value && typeof value === 'object') {
                            changed = patchObject(value) || changed;
                        }
                    }
                    return changed;
                }

                function patchBody(body) {
                    if (typeof body === 'string') {
                        try {
                            const parsed = JSON.parse(body);
                            const changed = patchObject(parsed);
                            return { body: changed ? JSON.stringify(parsed) : body, changed };
                        } catch (error) {
                            return { body, changed: false };
                        }
                    }

                    if (body instanceof URLSearchParams) {
                        const data = body.get('data');
                        if (!data) return { body, changed: false };
                        try {
                            const parsedData = JSON.parse(data);
                            Object.assign(parsedData, wireData);
                            const next = new URLSearchParams(body);
                            next.set('data', JSON.stringify(parsedData));
                            return { body: next, changed: true };
                        } catch (error) {
                            return { body, changed: false };
                        }
                    }

                    return { body, changed: false };
                }

                const originalFetch = window.fetch.bind(window);
                window.fetch = async function patchedFetch(input, init) {
                    let nextInput = input;
                    let nextInit = init ? { ...init } : init;

                    try {
                        if (nextInit && nextInit.body) {
                            const patched = patchBody(nextInit.body);
                            if (patched.changed) nextInit.body = patched.body;
                        } else if (input instanceof Request) {
                            const method = (input.method || 'GET').toUpperCase();
                            if (method !== 'GET' && method !== 'HEAD') {
                                const text = await input.clone().text();
                                const patched = patchBody(text);
                                if (patched.changed) {
                                    nextInput = new Request(input, { body: patched.body });
                                }
                            }
                        }
                    } catch (error) {
                        console.warn('Feishu form-fill payload guard fetch patch failed', error);
                    }

                    return originalFetch(nextInput, nextInit);
                };

                const originalSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.send = function patchedSend(body) {
                    try {
                        const patched = patchBody(body);
                        return originalSend.call(this, patched.changed ? patched.body : body);
                    } catch (error) {
                        console.warn('Feishu form-fill payload guard XHR patch failed', error);
                        return originalSend.call(this, body);
                    }
                };

                return { installed: true, alreadyInstalled: false, wireData };
            }""",
            wire_data,
        )
        return ActionResult(
            extracted_content=f"Installed Feishu form submit payload guard: {result}",
            long_term_memory="Installed Feishu form submit payload guard with confirmed field IDs and values.",
        )

    return controller


async def _extract_form_fill_with_llm(
    request: BrowserAgentRunRequest,
    settings: Settings,
) -> FeishuFormFillExtraction:
    llm_settings = _resolve_llm_settings(request, settings)
    llm = ChatOpenAI(
        base_url=llm_settings["base_url"],
        api_key=llm_settings["api_key"],
        model=llm_settings["model"],
        temperature=0,
    )
    now_text = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S %Z")
    system = SystemMessage(
        content=(
            "You extract three fields from a Chinese user message for a preset meeting registration form. "
            "Return structured JSON only.\n"
            "Fields:\n"
            "- name: attendee name\n"
            "- attendance_time: raw human-readable time expression exactly as implied by the user, such as '5月8号' or '下周三下午'\n"
            "- attendance_count: attendee count\n"
            "Rules:\n"
            "- Ignore action words like 登记下, 报名, 填写, 帮我处理.\n"
            "- Do NOT convert times into timestamps.\n"
            "- If a field is missing, leave it null.\n"
            "- raw_value should be the shortest supporting span copied from the user query.\n"
            f"- Current local time reference: {now_text}\n"
        )
    )
    user = UserMessage(content=request.query)
    result = await llm.ainvoke([system, user], output_format=FeishuFormFillExtraction)
    return result.completion


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
    _configure_browser_use_start_timeout(settings)

    auth = request.auth
    storage_state: str | dict[str, Any] | None = None

    if auth and auth.profile_id:
        profile = auth_store.get_profile(auth.profile_id)
        if profile is None:
            raise ValueError(f"Auth profile not found: {auth.profile_id}")
        storage_state = str(profile.storage_state_path)
    elif request.mode in {"feishu_bitable_to_form", "feishu_form_fill"}:
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
    elif request.mode == "feishu_form_fill":
        structured = history.structured_output or history.get_structured_output(FeishuFormFillOutput)
    else:
        structured = history.structured_output or history.get_structured_output(GenericBrowserTaskOutput)

    visited_urls = [url for url in (history.urls() if hasattr(history, "urls") else []) if url]
    screenshots = [path for path in (history.screenshot_paths() if hasattr(history, "screenshot_paths") else []) if path]
    errors = [error for error in (history.errors() if hasattr(history, "errors") else []) if error]
    history_excerpt = [_truncate(item) or "" for item in (history.extracted_content() or []) if item][-8:]
    current_url = visited_urls[-1] if visited_urls else None

    if isinstance(structured, FeishuBitableToFormOutput):
        is_waiting = structured.awaiting_human_confirmation
        final_text = (
            structured.form_url
            or ("Awaiting human confirmation for the draft questionnaire." if is_waiting else None)
            or "; ".join(structured.notes)
            or history.final_result()
        )
        return BrowserAgentRunResponse(
            run_id=run_id,
            success=structured.success and bool(structured.form_url) and not is_waiting,
            mode=request.mode,
            final_text=final_text,
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
            payload=(
                _build_bitable_ask_user_payload(
                    form_name=structured.form_name,
                    draft_questions=structured.draft_questions,
                )
                if is_waiting
                else _build_result_payload(
                    title=f"{structured.form_name or '问卷'}发布结果",
                    text=final_text or "任务已完成。",
                    summary=[
                        GatewayReplySummaryItem(label="问卷链接", value=structured.form_url),
                    ]
                    if structured.form_url
                    else [],
                )
            ),
            history_excerpt=history_excerpt,
        )

    if isinstance(structured, FeishuFormFillOutput):
        is_waiting = structured.awaiting_human_confirmation
        visible_submission_success = _looks_like_form_submission_success(
            structured.submission_result,
            history.final_result(),
        )
        success = not is_waiting and (structured.success or visible_submission_success)
        final_text = (
            ("Awaiting human confirmation for the drafted form answers." if is_waiting else None)
            or structured.submission_result
            or "; ".join(structured.notes)
            or history.final_result()
        )
        return BrowserAgentRunResponse(
            run_id=run_id,
            success=success,
            mode=request.mode,
            final_text=final_text,
            form_url=structured.form_url,
            form_name=structured.form_name,
            awaiting_human_confirmation=is_waiting,
            draft_answers=structured.draft_answers,
            submission_result=structured.submission_result,
            current_url=current_url,
            visited_urls=visited_urls,
            steps=history.number_of_steps(),
            duration_sec=duration_sec,
            screenshots=screenshots,
            errors=errors,
            notes=structured.notes,
            structured_output=structured.model_dump(),
            payload=(
                _build_form_fill_ask_user_payload(
                    form_name=structured.form_name,
                    draft_answers=structured.draft_answers,
                )
                if is_waiting
                else _build_result_payload(
                    title=f"{structured.form_name or '表单'}提交结果",
                    text=final_text or "表单已提交。",
                    summary=[
                        GatewayReplySummaryItem(label="表单链接", value=structured.form_url),
                        GatewayReplySummaryItem(label="提交结果", value=structured.submission_result),
                    ],
                )
            ),
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
    is_feishu_mode = request.mode in {"feishu_bitable_to_form", "feishu_form_fill"}
    is_phase_two = is_feishu_mode and bool(request.require_human_confirmation) and request.human_confirmation_granted

    try:
        # Phase 2 entry guard: consume the draft session (or refuse fast).
        if is_phase_two:
            assert request.draft_session_id is not None  # validator guarantees this
            try:
                session = await draft_store.consume(
                    session_id=request.draft_session_id,
                    mode=request.mode,
                    resource_url=request.bitable_url if request.mode == "feishu_bitable_to_form" else request.form_url,
                    profile_id=request.auth.profile_id if request.auth and request.auth.profile_id else None,
                )
                if request.mode == "feishu_form_fill":
                    merged_answers = _merge_confirmed_answers(session.draft_answers, request.confirmed_answers)
                    required_keys = {"name": "姓名", "attendance_time": "参会时间", "attendance_count": "参会人数"}
                    present_keys = {
                        answer.field_key
                        for answer in merged_answers
                        if answer.field_key and not answer.clear_value and (answer.confirmed_value or answer.normalized_values)
                    }
                    missing_fields = [label for key, label in required_keys.items() if key not in present_keys]
                    if missing_fields:
                        raise DraftSessionError(
                            "Cannot submit yet; the following required fields still need confirmation or correction: "
                            + "、".join(missing_fields)
                            + ". Re-run the prepare phase if the user added more information."
                        )
                    request = request.model_copy(
                        update={
                            "query": request.query or session.query or "",
                            "confirmed_answers": merged_answers,
                        }
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

        if request.mode == "feishu_form_fill" and not is_phase_two:
            await collector.emit(
                "run_started",
                {
                    "mode": request.mode,
                    "form_url": request.form_url or PRESET_FEISHU_FORM_URL,
                    "phase": "prepare",
                },
            )
            llm_extraction: FeishuFormFillExtraction | None = None
            try:
                llm_extraction = await _extract_form_fill_with_llm(request, settings)
            except Exception:
                logger.warning(
                    "LLM extraction failed for run_id=%s; falling back to rule-based parsing",
                    collector.run_id,
                    exc_info=True,
                )
            form_name, draft_answers, notes = parse_form_fill_query(request.query, llm_extraction=llm_extraction)
            response = BrowserAgentRunResponse(
                run_id=collector.run_id,
                success=False,
                mode=request.mode,
                final_text="Awaiting human confirmation for the drafted form answers.",
                form_url=request.form_url or PRESET_FEISHU_FORM_URL,
                form_name=form_name,
                awaiting_human_confirmation=True,
                draft_answers=draft_answers,
                current_url=None,
                visited_urls=[],
                steps=0,
                duration_sec=round(time.perf_counter() - started, 3),
                screenshots=[],
                errors=[],
                notes=notes,
                structured_output=FeishuFormFillOutput(
                    success=False,
                    form_url=request.form_url or PRESET_FEISHU_FORM_URL,
                    form_name=form_name,
                    awaiting_human_confirmation=True,
                    draft_answers=draft_answers,
                    submission_result=None,
                    notes=notes,
                ).model_dump(),
                payload=_build_form_fill_ask_user_payload(
                    form_name=form_name,
                    draft_answers=draft_answers,
                ),
                history_excerpt=[],
            )
            session = await draft_store.create(
                mode=request.mode,
                resource_url=request.form_url or PRESET_FEISHU_FORM_URL,
                profile_id=request.auth.profile_id if request.auth and request.auth.profile_id else None,
                draft_questions=[],
                draft_answers=[answer.model_dump() for answer in response.draft_answers],
                form_name=response.form_name,
                query=request.query,
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
                    "payload": response.payload.model_dump() if response.payload else None,
                    "steps": response.steps,
                    "duration_sec": response.duration_sec,
                },
            )
            return response

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
                "form_url": request.form_url,
                "max_steps": request.max_steps,
                "allowed_domains": effective_allowed_domains(request),
                "llm_model": llm_settings["model"],
                "auth_profile_id": request.auth.profile_id if request.auth and request.auth.profile_id else None,
                "feishu_default_profile_id": settings.feishu_default_profile_id if is_feishu_mode else None,
            },
        )

        browser = _build_browser(request, settings, run_dir, auth_store)
        task = build_task_prompt(request)
        if request.mode == "feishu_bitable_to_form":
            output_model = FeishuBitableToFormOutput
        elif request.mode == "feishu_form_fill":
            output_model = FeishuFormFillOutput
        else:
            output_model = GenericBrowserTaskOutput

        controller = (
            _build_feishu_form_fill_tools(request, FeishuFormFillOutput)
            if request.mode == "feishu_form_fill" and is_phase_two
            else None
        )
        initial_actions = (
            [{"navigate": {"url": request.bitable_url or request.form_url or request.start_url}}]
            if (request.bitable_url or request.form_url or request.start_url)
            else None
        )
        if controller is not None and initial_actions is not None:
            initial_actions.append({"install_feishu_form_submit_payload_guard": {}})

        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
            controller=controller,
            output_model_schema=output_model,
            sensitive_data=request.auth.sensitive_data if request.auth and request.auth.sensitive_data else None,
            use_vision={
                "auto": "auto",
                "always": True,
                "never": False,
            }[request.use_vision],
            initial_actions=initial_actions,
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
                mode=request.mode,
                resource_url=request.bitable_url,
                profile_id=request.auth.profile_id if request.auth and request.auth.profile_id else None,
                draft_questions=[q.model_dump() for q in response.draft_questions],
                draft_answers=[],
                form_name=response.form_name,
                query=request.query,
            )
            response.draft_session_id = session.session_id
            response.draft_session_expires_at = session.expires_at
        elif (
            request.mode == "feishu_form_fill"
            and not is_phase_two
            and response.awaiting_human_confirmation
            and response.draft_answers
            and request.form_url
        ):
            session = await draft_store.create(
                mode=request.mode,
                resource_url=request.form_url,
                profile_id=request.auth.profile_id if request.auth and request.auth.profile_id else None,
                draft_questions=[],
                draft_answers=[answer.model_dump() for answer in response.draft_answers],
                form_name=response.form_name,
                query=request.query,
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
                "submission_result": response.submission_result,
                "current_url": response.current_url,
                "payload": response.payload.model_dump() if response.payload else None,
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
