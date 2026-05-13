from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    log_level: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    browser_headless: bool
    browser_window_width: int
    browser_window_height: int
    browser_start_timeout_sec: float
    browser_artifacts_dir: Path
    auth_state_dir: Path
    feishu_default_profile_id: str
    draft_session_ttl_sec: int


def load_settings() -> Settings:
    artifacts_dir = Path(os.getenv("BROWSER_ARTIFACTS_DIR", "/tmp/browser-agent-artifacts"))
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    auth_state_dir = Path(os.getenv("AUTH_STATE_DIR", "/tmp/browser-agent-auth"))
    auth_state_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        host=os.getenv("HOST", "0.0.0.0"),
        port=_get_int("PORT", 49999),
        log_level=os.getenv("LOG_LEVEL", "info"),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.0),
        browser_headless=_get_bool("BROWSER_HEADLESS", True),
        browser_window_width=_get_int("BROWSER_WINDOW_WIDTH", 1440),
        browser_window_height=_get_int("BROWSER_WINDOW_HEIGHT", 900),
        browser_start_timeout_sec=_get_float("BROWSER_START_TIMEOUT_SEC", 120.0),
        browser_artifacts_dir=artifacts_dir,
        auth_state_dir=auth_state_dir,
        feishu_default_profile_id=os.getenv("FEISHU_DEFAULT_PROFILE_ID", "feishu-default"),
        draft_session_ttl_sec=_get_int("DRAFT_SESSION_TTL_SEC", 1800),
    )
