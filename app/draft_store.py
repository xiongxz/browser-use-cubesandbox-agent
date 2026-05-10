"""In-memory draft session store binding the two phases of the Feishu HITL flow.

A draft session is created when phase 1 (`feishu_bitable_draft_form`) returns a
draft for human review. Phase 2 (`feishu_bitable_publish_form`) must echo the
returned ``draft_session_id`` to prove the publish call was preceded by a
human-reviewed draft. The store enforces:

- TTL based eviction (default 30 minutes).
- One-shot consumption — the session is deleted on a successful match so the
  same draft cannot be replayed.
- ``bitable_url`` and ``auth.profile_id`` must match between phase 1 and
  phase 2; otherwise the publish call is rejected before the agent runs.

Note: state lives in process memory. Multi-instance deployments need to back
this with Redis or another shared store; the showcase runs single-instance.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class DraftSession:
    session_id: str
    mode: str
    resource_url: str
    profile_id: str | None
    draft_questions: list[dict[str, Any]]
    draft_answers: list[dict[str, Any]]
    form_name: str | None
    query: str | None
    created_at: float
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


class DraftSessionError(ValueError):
    """Raised when a draft session lookup or match fails."""


class DraftSessionStore:
    def __init__(self, ttl_sec: int = 1800) -> None:
        self._ttl_sec = ttl_sec
        self._sessions: dict[str, DraftSession] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_sec(self) -> int:
        return self._ttl_sec

    async def create(
        self,
        *,
        mode: str,
        resource_url: str,
        profile_id: str | None,
        draft_questions: list[dict[str, Any]],
        draft_answers: list[dict[str, Any]],
        form_name: str | None,
        query: str | None,
    ) -> DraftSession:
        async with self._lock:
            self._evict_expired_locked()
            session_id = str(uuid.uuid4())
            now = time.time()
            session = DraftSession(
                session_id=session_id,
                mode=mode,
                resource_url=resource_url,
                profile_id=profile_id,
                draft_questions=list(draft_questions),
                draft_answers=list(draft_answers),
                form_name=form_name,
                query=query,
                created_at=now,
                expires_at=now + self._ttl_sec,
            )
            self._sessions[session_id] = session
            return session

    async def consume(
        self,
        *,
        session_id: str,
        mode: str,
        resource_url: str,
        profile_id: str | None,
    ) -> DraftSession:
        async with self._lock:
            self._evict_expired_locked()
            session = self._sessions.get(session_id)
            if session is None:
                raise DraftSessionError(
                    f"draft_session_id not found or already consumed: {session_id}"
                )
            if session.is_expired():
                del self._sessions[session_id]
                raise DraftSessionError(
                    f"draft_session_id expired: {session_id}. Re-run the draft phase."
                )
            if session.mode != mode:
                raise DraftSessionError(
                    f"draft_session_id mode mismatch. Expected {session.mode}, got {mode}."
                )
            if session.resource_url != resource_url:
                raise DraftSessionError(
                    "draft_session_id does not match the target resource URL. "
                    f"Expected {session.resource_url}, got {resource_url}."
                )
            if session.profile_id != profile_id:
                raise DraftSessionError(
                    "draft_session_id does not match auth profile. "
                    f"Expected {session.profile_id or '(none)'}, "
                    f"got {profile_id or '(none)'}."
                )
            del self._sessions[session_id]
            return session

    async def peek(self, session_id: str) -> DraftSession | None:
        async with self._lock:
            self._evict_expired_locked()
            return self._sessions.get(session_id)

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired_ids = [sid for sid, sess in self._sessions.items() if sess.is_expired(now)]
        for sid in expired_ids:
            del self._sessions[sid]
