from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


_FEISHU_HOST_SUFFIXES = ("feishu.cn", "larksuite.com", "larkoffice.com")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；：、！？「」【】（）]+", re.UNICODE)
_TRAILING_PUNCT = "，。；：、！？,.;:!?）)]】」'\"<>"
PRESET_FEISHU_FORM_URL = "https://lexmount.feishu.cn/share/base/form/shrcnMEX6kDGkDArxCLgnsIWR8f"
PRESET_FEISHU_FORM_NAME = "参会登记问卷"


def extract_feishu_url(text: str | None) -> str | None:
    """Pick the best Feishu/Lark URL out of free-form text.

    Strips trailing CJK/ASCII punctuation, requires the host to be a Feishu
    or Lark domain, and prefers URLs that look like a bitable target
    (``/base/`` or ``/wiki/``) before falling back to the first match.
    """

    if not text:
        return None

    candidates: list[str] = []
    for raw in _URL_PATTERN.findall(text):
        url = raw.rstrip(_TRAILING_PUNCT)
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if not host:
            continue
        if any(host == suffix or host.endswith("." + suffix) for suffix in _FEISHU_HOST_SUFFIXES):
            candidates.append(url)

    if not candidates:
        return None

    preferred = [u for u in candidates if "/base/" in u or "/wiki/" in u]
    return preferred[0] if preferred else candidates[0]


def extract_feishu_form_url(text: str | None) -> str | None:
    """Pick the best Feishu/Lark form URL out of free-form text."""

    if not text:
        return None

    candidates: list[str] = []
    for raw in _URL_PATTERN.findall(text):
        url = raw.rstrip(_TRAILING_PUNCT)
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if not host:
            continue
        if any(host == suffix or host.endswith("." + suffix) for suffix in _FEISHU_HOST_SUFFIXES):
            candidates.append(url)

    if not candidates:
        return None

    preferred = [
        u
        for u in candidates
        if "/share/base/form/" in u or "/base/form/" in u or "form" in (urlparse(u).path or "")
    ]
    return preferred[0] if preferred else None


class LLMOverride(BaseModel):
    base_url: str | None = Field(default=None, description="OpenAI-compatible base URL.")
    api_key: str | None = Field(default=None, description="API key for the configured LLM endpoint.")
    model: str | None = Field(default=None, description="Model name for the configured LLM endpoint.")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class BrowserAuthConfig(BaseModel):
    profile_id: str | None = Field(
        default=None,
        description="Server-side stored auth profile id. Recommended for Feishu and other long-lived sessions.",
    )
    storage_state: dict[str, Any] | None = Field(
        default=None,
        description="Playwright storage state JSON content. Useful for logged-in browser sessions.",
    )
    storage_state_path: str | None = Field(
        default=None,
        description="Absolute path to an existing Playwright storage state file inside the container.",
    )
    sensitive_data: dict[str, Any] | None = Field(
        default=None,
        description="Optional Browser Use sensitive_data payload for credentials and secrets.",
    )


class BrowserAgentRunRequest(BaseModel):
    query: str = Field(
        default="",
        description="Natural language instruction for the browser agent.",
    )
    mode: Literal["general", "feishu_bitable_to_form", "feishu_form_fill"] | None = Field(
        default=None,
        description="Execution mode. Use feishu_bitable_to_form for bitable -> questionnaire conversion, or feishu_form_fill for filling a prebuilt Feishu questionnaire from natural language. Auto-detected when omitted and the request contains a recognizable Feishu URL.",
    )
    start_url: str | None = Field(default=None, description="Optional start URL for the first navigation step.")
    bitable_url: str | None = Field(
        default=None,
        description="Feishu bitable URL. Required when mode=feishu_bitable_to_form.",
    )
    form_url: str | None = Field(
        default=None,
        description="Feishu questionnaire/form URL. When mode=feishu_form_fill and omitted, the server falls back to the built-in preset form URL.",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Optional Browser Use allowed_domains restrictions.",
    )
    headless: bool | None = Field(default=None, description="Override container browser headless mode.")
    max_steps: int = Field(default=35, ge=1, le=120)
    timeout_sec: int = Field(default=600, ge=30, le=3600)
    use_vision: Literal["auto", "always", "never"] = Field(default="auto")
    require_human_confirmation: bool | None = Field(
        default=None,
        description="When true in Feishu mode, stop after capturing the draft questionnaire for human review. Defaults to true in Feishu mode and false in general mode.",
    )
    human_confirmation_granted: bool = Field(
        default=False,
        description="Phase-2 signal: set to true ONLY after a human has reviewed the draft questionnaire returned by the draft phase. Must be paired with draft_session_id.",
    )
    human_confirmation_notes: str | None = Field(
        default=None,
        description="Optional human feedback or edit instructions to apply before publishing/sharing the questionnaire.",
    )
    draft_session_id: str | None = Field(
        default=None,
        description="Phase-2 binding token. Pass back the draft_session_id returned by the draft phase response so the server can validate the publish call.",
    )
    confirmed_answers: list["FeishuFormAnswerOverride"] = Field(
        default_factory=list,
        description="Optional confirmed or corrected field values for mode=feishu_form_fill phase 2. When omitted, the server reuses the phase-1 drafted values.",
    )
    feishu_field_ids: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional Feishu form field id mapping for mode=feishu_form_fill phase 2. "
            "Keys: name, attendance_time, attendance_count. When omitted, the built-in preset form mapping is used."
        ),
    )
    llm: LLMOverride | None = Field(default=None)
    auth: BrowserAuthConfig | None = Field(default=None)

    @model_validator(mode="after")
    def validate_mode(self) -> "BrowserAgentRunRequest":
        # 1. Auto-promote to a Feishu mode when explicit URLs are present.
        # Only auto-detect when mode is not explicitly set
        if self.mode is None:
            extracted_form = self.form_url or extract_feishu_form_url(self.query)
            extracted_bitable = self.bitable_url or extract_feishu_url(self.query)
            if extracted_form:
                self.mode = "feishu_form_fill"
                self.form_url = extracted_form
            elif extracted_bitable:
                self.mode = "feishu_bitable_to_form"
                self.bitable_url = extracted_bitable
            else:
                self.mode = "general"

        # 2. HITL fields are only meaningful in Feishu modes.
        hitl_explicit = (
            self.require_human_confirmation is True
            or self.human_confirmation_granted is True
            or bool(self.human_confirmation_notes)
            or bool(self.draft_session_id)
            or bool(self.confirmed_answers)
        )
        if self.mode not in {"feishu_bitable_to_form", "feishu_form_fill"} and hitl_explicit:
            raise ValueError(
                "require_human_confirmation / human_confirmation_granted / "
                "human_confirmation_notes / draft_session_id / confirmed_answers only apply to "
                "mode=feishu_bitable_to_form or mode=feishu_form_fill. Either include a recognized "
                "Feishu/Lark URL in query so mode is auto-detected, or set mode explicitly."
            )

        # 3. Default require_human_confirmation per mode
        if self.require_human_confirmation is None:
            self.require_human_confirmation = self.mode in {"feishu_bitable_to_form", "feishu_form_fill"}

        # 4. mode-specific required fields
        if self.mode == "general":
            if not self.query.strip():
                raise ValueError("query is required when mode=general")
        elif self.mode == "feishu_bitable_to_form":
            if not self.bitable_url:
                self.bitable_url = extract_feishu_url(self.query)
            if not self.bitable_url:
                raise ValueError(
                    "feishu_bitable_to_form requires a Feishu bitable URL. "
                    "Pass it as bitable_url, or include a https://*.feishu.cn/base/... "
                    "or /wiki/... link inside query."
                )
            if not self.query.strip():
                self.query = "请将这个飞书多维表格转换为问卷，并返回最终问卷链接。"
        elif self.mode == "feishu_form_fill":
            if not self.form_url:
                self.form_url = extract_feishu_form_url(self.query) or PRESET_FEISHU_FORM_URL
            if not self.form_url:
                raise ValueError(
                    "feishu_form_fill requires a Feishu questionnaire/form URL. "
                    "Pass it as form_url, or include a https://*.feishu.cn/... form link inside query."
                )
            if not self.query.strip() and not self.human_confirmation_granted:
                raise ValueError("query is required for the draft phase when mode=feishu_form_fill")

        # 5. Phase-2 must include draft_session_id; phase-1 must NOT
        if self.mode in {"feishu_bitable_to_form", "feishu_form_fill"}:
            if self.human_confirmation_granted and not self.draft_session_id:
                raise ValueError(
                    "human_confirmation_granted=true requires draft_session_id. "
                    "Call the draft phase first, then echo its draft_session_id here."
                )
            if self.draft_session_id and not self.human_confirmation_granted:
                raise ValueError(
                    "draft_session_id is only valid when human_confirmation_granted=true. "
                    "Drop it for the draft phase."
                )

        return self


class GenericBrowserTaskOutput(BaseModel):
    success: bool = Field(description="Whether the task finished successfully.")
    final_answer: str = Field(description="Final answer for the user.")
    important_urls: list[str] = Field(default_factory=list, description="Important URLs collected during execution.")
    notes: list[str] = Field(default_factory=list, description="Warnings, blockers, or follow-up notes.")


class FeishuQuestionDraft(BaseModel):
    index: int = Field(description="1-based order of the question in the visible form editor.")
    title: str = Field(description="Visible question title or label.")
    question_type: str | None = Field(default=None, description="Detected question type if visible, otherwise null.")
    required: bool | None = Field(default=None, description="Whether the question is marked required if visible.")


class FeishuFormAnswerDraft(BaseModel):
    index: int = Field(description="1-based order of the question in the visible form.")
    field_key: str = Field(description="Stable internal key for the preset field.")
    field_label: str = Field(description="Visible field or question label.")
    question_type: str | None = Field(default=None, description="Detected field type if visible, otherwise null.")
    required: bool | None = Field(default=None, description="Whether the field is marked required if visible.")
    proposed_value: str | None = Field(
        default=None,
        description="Proposed answer derived from the user's natural-language query. Null when the agent could not infer a safe answer.",
    )
    raw_value: str | None = Field(
        default=None,
        description="Raw fragment extracted from the user query before normalization.",
    )
    normalized_values: list[str] = Field(
        default_factory=list,
        description="Structured option values for choice-style fields. Use one item for single-select and multiple items for checkbox-like fields.",
    )
    confidence: Literal["high", "medium", "low"] | None = Field(
        default=None,
        description="How confident the agent is that the proposed answer matches the user's intent.",
    )
    source_excerpt: str | None = Field(
        default=None,
        description="Short excerpt from the original user query that supports the answer, when available.",
    )


class FeishuFormAnswerOverride(BaseModel):
    index: int | None = Field(default=None, description="Target field index from the phase-1 draft.")
    field_key: str | None = Field(default=None, description="Stable internal key from the phase-1 draft.")
    field_label: str | None = Field(default=None, description="Target field label from the phase-1 draft.")
    confirmed_value: str | None = Field(
        default=None,
        description="Human-confirmed final answer. Use null together with clear_value=true to intentionally clear the field.",
    )
    normalized_values: list[str] = Field(
        default_factory=list,
        description="Optional structured option values that should replace the drafted selection list.",
    )
    clear_value: bool = Field(
        default=False,
        description="When true, intentionally clear the previously drafted answer for this field.",
    )

    @model_validator(mode="after")
    def validate_target(self) -> "FeishuFormAnswerOverride":
        if self.index is None and not self.field_label and not self.field_key:
            raise ValueError("Each confirmed_answers item requires index, field_key, or field_label")
        if self.confirmed_value is None and not self.normalized_values and not self.clear_value:
            raise ValueError(
                "Each confirmed_answers item must provide confirmed_value, normalized_values, or clear_value=true"
            )
        return self


class FeishuBitableToFormOutput(BaseModel):
    success: bool = Field(description="Whether the conversion finished successfully.")
    bitable_url: str = Field(description="The source bitable URL that the agent operated on.")
    form_url: str | None = Field(default=None, description="The final generated questionnaire URL, if successful.")
    form_name: str | None = Field(default=None, description="Questionnaire title or name when available.")
    awaiting_human_confirmation: bool = Field(
        default=False,
        description="True when the agent has prepared the questionnaire draft and is waiting for a human to confirm before sharing.",
    )
    draft_questions: list[FeishuQuestionDraft] = Field(
        default_factory=list,
        description="Visible draft questionnaire questions captured from the Feishu form editor for human review.",
    )
    notes: list[str] = Field(default_factory=list, description="Warnings, blockers, or follow-up notes.")


class FeishuFormFillOutput(BaseModel):
    success: bool = Field(description="Whether the form-fill flow finished successfully.")
    form_url: str = Field(description="The Feishu questionnaire/form URL that the agent operated on.")
    form_name: str | None = Field(default=None, description="Questionnaire title when visible.")
    awaiting_human_confirmation: bool = Field(
        default=False,
        description="True when the agent has prepared draft answers and is waiting for a human to confirm before final submission.",
    )
    draft_answers: list[FeishuFormAnswerDraft] = Field(
        default_factory=list,
        description="Draft answers inferred from the user query for human review.",
    )
    submission_result: str | None = Field(
        default=None,
        description="Visible confirmation text after successful submission, when available.",
    )
    notes: list[str] = Field(default_factory=list, description="Warnings, blockers, or follow-up notes.")


class FeishuFieldExtraction(BaseModel):
    value: str | None = Field(default=None, description="Extracted field value.")
    raw_value: str | None = Field(default=None, description="Shortest supporting span copied from the user query.")
    confidence: Literal["high", "medium", "low"] | None = Field(default=None)


class FeishuFormFillExtraction(BaseModel):
    name: FeishuFieldExtraction = Field(default_factory=FeishuFieldExtraction)
    attendance_time: FeishuFieldExtraction = Field(default_factory=FeishuFieldExtraction)
    attendance_count: FeishuFieldExtraction = Field(default_factory=FeishuFieldExtraction)
    notes: list[str] = Field(default_factory=list)


class GatewayReplySummaryItem(BaseModel):
    id: str | None = Field(default=None, description="Optional machine-readable item id.")
    label: str = Field(description="Human-readable label.")
    value: Any = Field(default=None, description="Display value.")


class GatewayReplyOption(BaseModel):
    id: str = Field(description="Machine-readable option id.")
    label: str = Field(description="Human-readable option label.")
    description: str | None = Field(default=None, description="Short explanation of what this option does.")
    preview: str | None = Field(default=None, description="Optional preview text for rich cards.")


class GatewayReplyQuestion(BaseModel):
    header: str = Field(description="Short section header for this question.")
    question: str = Field(description="Question text shown to the user.")
    options: list[GatewayReplyOption] = Field(default_factory=list)
    multiSelect: bool = Field(default=False, description="Whether multiple options can be selected.")


class GatewayReplyField(BaseModel):
    id: str = Field(description="Machine-readable field id.")
    label: str = Field(description="Human-readable field label.")
    type: str = Field(default="text", description="Input type hint, for example text, number, date.")
    required: bool = False
    placeholder: str | None = None


class GatewayReplyPayload(BaseModel):
    version: str = Field(default="goclaw.gateway.reply.v1")
    kind: Literal["text", "ask_user", "result", "error"] = Field(default="text")
    title: str | None = None
    text: str
    summary: list[GatewayReplySummaryItem] = Field(default_factory=list)
    questions: list[GatewayReplyQuestion] = Field(default_factory=list)
    fields: list[GatewayReplyField] = Field(default_factory=list)


class BrowserAgentRunResponse(BaseModel):
    run_id: str
    success: bool
    mode: str
    final_text: str | None = None
    form_url: str | None = None
    form_name: str | None = None
    awaiting_human_confirmation: bool = False
    draft_session_id: str | None = Field(
        default=None,
        description="Issued by the draft phase when awaiting_human_confirmation=true. Pass it back into the publish phase request to bind the two calls.",
    )
    draft_session_expires_at: float | None = Field(
        default=None,
        description="Unix epoch seconds when the draft_session_id expires. Null when no session was issued.",
    )
    draft_questions: list[FeishuQuestionDraft] = Field(default_factory=list)
    draft_answers: list[FeishuFormAnswerDraft] = Field(default_factory=list)
    submission_result: str | None = Field(default=None, description="Visible submission success message or receipt text when available.")
    current_url: str | None = None
    visited_urls: list[str] = Field(default_factory=list)
    steps: int = 0
    duration_sec: float = 0.0
    screenshots: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    payload: GatewayReplyPayload | None = Field(
        default=None,
        description="Gateway/UI rendering payload. Existing orchestration fields remain at the response top level.",
    )
    history_excerpt: list[str] = Field(default_factory=list)


class StreamEvent(BaseModel):
    event: str
    run_id: str
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)


class AuthProfileUpsertRequest(BaseModel):
    profile_id: str | None = Field(
        default=None,
        description="Custom profile id. If omitted, the server uses FEISHU_DEFAULT_PROFILE_ID for Feishu flows or generates one.",
    )
    storage_state: dict[str, Any] = Field(
        ...,
        description="Playwright storage_state JSON object containing cookies and origins.",
    )
    set_as_feishu_default: bool = Field(
        default=False,
        description="Whether to save this profile as the default Feishu login profile.",
    )
    description: str | None = Field(default=None, description="Optional human-readable note.")


class AuthProfileSummary(BaseModel):
    profile_id: str
    storage_state_path: str
    created_at: str
    updated_at: str
    description: str | None = None
    is_feishu_default: bool = False


class AuthProfileListResponse(BaseModel):
    items: list[AuthProfileSummary] = Field(default_factory=list)


class RuntimeConfigUpdateRequest(BaseModel):
    """Subset of Settings fields that can be injected at runtime via /v1/init.

    Use this when the sandbox cannot accept env vars at creation time. Only
    non-null fields are merged into the in-memory overlay; existing values are
    kept for keys that are omitted. The endpoint accepts either the canonical
    snake_case fields directly or env-style keys nested under ``config``.
    """

    llm_base_url: str | None = Field(default=None, description="OpenAI-compatible base URL.")
    llm_api_key: str | None = Field(default=None, description="API key for the configured LLM endpoint.")
    llm_model: str | None = Field(default=None, description="Model name for the configured LLM endpoint.")
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    browser_headless: bool | None = Field(default=None)
    browser_window_width: int | None = Field(default=None, ge=320, le=4096)
    browser_window_height: int | None = Field(default=None, ge=320, le=4096)
    browser_start_timeout_sec: float | None = Field(default=None, ge=1.0, le=600.0)
    feishu_default_profile_id: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def normalize_runtime_config_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        alias_map = {
            "LLM_BASE_URL": "llm_base_url",
            "OPENAI_BASE_URL": "llm_base_url",
            "OPENAI_API_BASE": "llm_base_url",
            "BASE_URL": "llm_base_url",
            "LLM_API_KEY": "llm_api_key",
            "OPENAI_API_KEY": "llm_api_key",
            "API_KEY": "llm_api_key",
            "LLM_MODEL": "llm_model",
            "OPENAI_MODEL": "llm_model",
            "MODEL": "llm_model",
            "LLM_TEMPERATURE": "llm_temperature",
            "OPENAI_TEMPERATURE": "llm_temperature",
            "TEMPERATURE": "llm_temperature",
            "BROWSER_HEADLESS": "browser_headless",
            "BROWSER_WINDOW_WIDTH": "browser_window_width",
            "BROWSER_WINDOW_HEIGHT": "browser_window_height",
            "BROWSER_START_TIMEOUT_SEC": "browser_start_timeout_sec",
            "FEISHU_DEFAULT_PROFILE_ID": "feishu_default_profile_id",
        }

        canonical_keys = {
            "llm_base_url",
            "llm_api_key",
            "llm_model",
            "llm_temperature",
            "browser_headless",
            "browser_window_width",
            "browser_window_height",
            "browser_start_timeout_sec",
            "feishu_default_profile_id",
        }
        for key in canonical_keys:
            alias_map[key.upper()] = key

        def normalize_key(key: Any) -> str:
            with_word_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
            normalized = re.sub(r"[^0-9A-Za-z]+", "_", with_word_breaks).strip("_")
            return normalized.upper()

        payload: dict[str, Any] = {}
        config_key = next((key for key in data if normalize_key(key) == "CONFIG"), None)
        if config_key is not None and isinstance(data.get(config_key), dict):
            payload.update(data[config_key])
        for key, value in data.items():
            if normalize_key(key) != "CONFIG":
                payload[key] = value

        normalized_payload: dict[str, Any] = {}
        for key, value in payload.items():
            canonical = alias_map.get(normalize_key(key))
            if canonical:
                normalized_payload[canonical] = value
        return normalized_payload


class RuntimeConfigSnapshot(BaseModel):
    initialized_at: str | None
    initialized_keys: list[str] = Field(default_factory=list)
    runtime_config: dict[str, Any] = Field(default_factory=dict)


class FeishuFormFillRunRequest(BaseModel):
    query: str = Field(description="Natural-language source text that should be parsed into the form fields.")
    form_url: str | None = Field(default=None, description="Optional Feishu form URL. Defaults to the built-in preset form.")
    allowed_domains: list[str] = Field(default_factory=list)
    headless: bool | None = None
    max_steps: int = Field(default=60, ge=1, le=120)
    timeout_sec: int = Field(default=900, ge=30, le=3600)
    use_vision: Literal["auto", "always", "never"] = Field(default="auto")
    llm: LLMOverride | None = None
    auth: BrowserAuthConfig | None = None
    field_ids: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional Feishu form field id mapping for non-preset or rebuilt forms. "
            "Keys: name, attendance_time, attendance_count. Omit to use the built-in preset form mapping."
        ),
    )


class FormFillRunInputRequest(BaseModel):
    question_id: str
    action: Literal["accept", "decline", "cancel", "message"] = Field(
        default="message",
        description="Optional UI action hint. Omit for natural-language replies; use content.decision for button semantics.",
    )
    content: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class FormFillUserIntent(BaseModel):
    intent: Literal["confirm", "cancel", "edit", "supplement", "unknown"] = Field(
        description="Semantic intent of the user's response."
    )
    confidence: Literal["high", "medium", "low"] = "medium"
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Field edits extracted from the response. Keys: name, attendance_time, attendance_count.",
    )
    supplement: str | None = Field(
        default=None,
        description="Free-form supplementary information that should be appended and reparsed.",
    )
    reason: str | None = None


BrowserAgentRunRequest.model_rebuild()
