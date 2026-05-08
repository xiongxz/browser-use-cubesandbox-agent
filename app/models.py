from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


_FEISHU_HOST_SUFFIXES = ("feishu.cn", "larksuite.com", "larkoffice.com")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；：、！？「」【】（）]+", re.UNICODE)
_TRAILING_PUNCT = "，。；：、！？,.;:!?）)]】」'\"<>"


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
    mode: Literal["general", "feishu_bitable_to_form"] | None = Field(
        default=None,
        description="Execution mode. Use feishu_bitable_to_form for the dedicated showcase. Auto-detected when omitted and query contains a Feishu URL.",
    )
    start_url: str | None = Field(default=None, description="Optional start URL for the first navigation step.")
    bitable_url: str | None = Field(
        default=None,
        description="Feishu bitable URL. Required when mode=feishu_bitable_to_form.",
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
    llm: LLMOverride | None = Field(default=None)
    auth: BrowserAuthConfig | None = Field(default=None)

    @model_validator(mode="after")
    def validate_mode(self) -> "BrowserAgentRunRequest":
        # 1. Auto-promote to feishu mode when query/bitable_url contains a Feishu URL
        # Only auto-detect when mode is not explicitly set
        if self.mode is None:
            extracted = extract_feishu_url(self.query) or extract_feishu_url(self.bitable_url or "")
            if extracted:
                self.mode = "feishu_bitable_to_form"
                if not self.bitable_url:
                    self.bitable_url = extracted
            else:
                self.mode = "general"

        # 2. HITL fields are only meaningful in feishu mode
        hitl_explicit = (
            self.require_human_confirmation is True
            or self.human_confirmation_granted is True
            or bool(self.human_confirmation_notes)
            or bool(self.draft_session_id)
        )
        if self.mode != "feishu_bitable_to_form" and hitl_explicit:
            raise ValueError(
                "require_human_confirmation / human_confirmation_granted / "
                "human_confirmation_notes / draft_session_id only apply to "
                "mode=feishu_bitable_to_form. Either include a Feishu/Lark URL "
                "in query so mode is auto-detected, or set mode explicitly."
            )

        # 3. Default require_human_confirmation per mode
        if self.require_human_confirmation is None:
            self.require_human_confirmation = self.mode == "feishu_bitable_to_form"

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

        # 5. Phase-2 must include draft_session_id; phase-1 must NOT
        if self.mode == "feishu_bitable_to_form":
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
    current_url: str | None = None
    visited_urls: list[str] = Field(default_factory=list)
    steps: int = 0
    duration_sec: float = 0.0
    screenshots: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] | None = None
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
    kept for keys that are omitted.
    """

    llm_base_url: str | None = Field(default=None, description="OpenAI-compatible base URL.")
    llm_api_key: str | None = Field(default=None, description="API key for the configured LLM endpoint.")
    llm_model: str | None = Field(default=None, description="Model name for the configured LLM endpoint.")
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    browser_headless: bool | None = Field(default=None)
    browser_window_width: int | None = Field(default=None, ge=320, le=4096)
    browser_window_height: int | None = Field(default=None, ge=320, le=4096)
    feishu_default_profile_id: str | None = Field(default=None)


class RuntimeConfigSnapshot(BaseModel):
    initialized_at: str | None
    initialized_keys: list[str] = Field(default_factory=list)
    runtime_config: dict[str, Any] = Field(default_factory=dict)
