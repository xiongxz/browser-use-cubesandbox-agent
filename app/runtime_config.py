"""Runtime config overlay — bridges the gap between sandbox env vars and a
running container that was started without any custom env injection.

Workflow:

1. Service boots from ``Settings`` loaded out of the process env (which may be
   empty / placeholder values). It does not crash if LLM creds are missing.
2. The operator POSTs to ``/v1/init`` with whichever subset of config they want
   to override. The overlay is held in memory only; it is NOT persisted.
3. Every subsequent request reads ``runtime_config.settings``, which produces a
   merged frozen ``Settings`` snapshot (overlay > env > default).
4. ``/healthz`` reports the merged snapshot (with secrets masked) plus a small
   battery of cheap checks so the operator can confirm the values landed.

The overlay is single-instance and lost on restart — fine for sandbox showcase
usage; a multi-instance prod deployment needs a shared store (Redis etc.).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .config import Settings


ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_temperature",
        "browser_headless",
        "browser_window_width",
        "browser_window_height",
        "feishu_default_profile_id",
    }
)

SENSITIVE_KEYS: frozenset[str] = frozenset({"llm_api_key"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


class RuntimeConfigStore:
    def __init__(self, base: Settings) -> None:
        self._base = base
        self._overlay: dict[str, Any] = {}
        self._initialized_at: str | None = None
        self._lock = asyncio.Lock()

    @property
    def base(self) -> Settings:
        return self._base

    @property
    def settings(self) -> Settings:
        if not self._overlay:
            return self._base
        snapshot = dict(self._overlay)
        if "llm_base_url" in snapshot and isinstance(snapshot["llm_base_url"], str):
            snapshot["llm_base_url"] = snapshot["llm_base_url"].rstrip("/")
        return replace(self._base, **snapshot)

    @property
    def initialized_at(self) -> str | None:
        return self._initialized_at

    @property
    def initialized_keys(self) -> list[str]:
        return sorted(self._overlay.keys())

    async def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = {k: v for k, v in payload.items() if v is not None}
        unknown = set(cleaned.keys()) - ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"unsupported runtime config keys: {sorted(unknown)}. "
                f"Allowed keys: {sorted(ALLOWED_KEYS)}"
            )
        async with self._lock:
            self._overlay.update(cleaned)
            self._initialized_at = _utc_now()
        return self.snapshot()

    async def reset(self) -> None:
        async with self._lock:
            self._overlay.clear()
            self._initialized_at = None

    def snapshot(self) -> dict[str, Any]:
        s = self.settings
        return {
            "llm_base_url": s.llm_base_url,
            "llm_api_key_set": bool(s.llm_api_key),
            "llm_api_key_preview": mask_secret(s.llm_api_key),
            "llm_model": s.llm_model,
            "llm_temperature": s.llm_temperature,
            "browser_headless": s.browser_headless,
            "browser_window_width": s.browser_window_width,
            "browser_window_height": s.browser_window_height,
            "feishu_default_profile_id": s.feishu_default_profile_id,
            "draft_session_ttl_sec": s.draft_session_ttl_sec,
            "browser_artifacts_dir": str(s.browser_artifacts_dir),
            "auth_state_dir": str(s.auth_state_dir),
            "log_level": s.log_level,
            "host": s.host,
            "port": s.port,
        }
