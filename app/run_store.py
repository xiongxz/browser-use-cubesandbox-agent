from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .models import (
    FeishuFormAnswerOverride,
    FeishuFormFillRunRequest,
    FormFillRunInputRequest,
    GatewayReplyPayload,
)

_UNSET = object()


@dataclass
class FormFillRunState:
    run_id: str
    request: FeishuFormFillRunRequest
    created_at: float = field(default_factory=time.time)
    status: str = "created"
    current_question_id: str | None = None
    current_query: str = ""
    draft_session_id: str | None = None
    draft_session_expires_at: float | None = None
    payload: GatewayReplyPayload | None = None
    confirmed_by_key: dict[str, FeishuFormAnswerOverride] = field(default_factory=dict)


class FormFillRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, FormFillRunState] = {}
        self._lock = asyncio.Lock()

    async def create(self, run_id: str, request: FeishuFormFillRunRequest) -> FormFillRunState:
        async with self._lock:
            state = FormFillRunState(run_id=run_id, request=request, current_query=request.query)
            self._runs[run_id] = state
            return state

    async def get(self, run_id: str) -> FormFillRunState | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_question_id: str | None | object = _UNSET,
        current_query: str | object = _UNSET,
        draft_session_id: str | None | object = _UNSET,
        draft_session_expires_at: float | None | object = _UNSET,
        payload: GatewayReplyPayload | None | object = _UNSET,
        confirmed_by_key: dict[str, FeishuFormAnswerOverride] | object = _UNSET,
    ) -> None:
        async with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return
            if status is not None:
                state.status = status
            if current_question_id is not _UNSET:
                assert current_question_id is None or isinstance(current_question_id, str)
                state.current_question_id = current_question_id
            if current_query is not _UNSET:
                assert isinstance(current_query, str)
                state.current_query = current_query
            if draft_session_id is not _UNSET:
                assert draft_session_id is None or isinstance(draft_session_id, str)
                state.draft_session_id = draft_session_id
            if draft_session_expires_at is not _UNSET:
                assert draft_session_expires_at is None or isinstance(draft_session_expires_at, float)
                state.draft_session_expires_at = draft_session_expires_at
            if payload is not _UNSET:
                assert payload is None or isinstance(payload, GatewayReplyPayload)
                state.payload = payload
            if confirmed_by_key is not _UNSET:
                assert isinstance(confirmed_by_key, dict)
                state.confirmed_by_key = confirmed_by_key

    async def validate_input(self, run_id: str, request: FormFillRunInputRequest) -> FormFillRunState:
        state = await self.get(run_id)
        if state is None:
            raise KeyError(run_id)
        if state.status != "awaiting_user":
            raise ValueError(f"run is not awaiting user input: {state.status}")
        if state.current_question_id != request.question_id:
            raise ValueError(
                f"question_id mismatch. Expected {state.current_question_id}, got {request.question_id}."
            )
        if state.draft_session_expires_at is not None and time.time() >= state.draft_session_expires_at:
            raise TimeoutError("run interaction has expired")
        return state

    async def remove(self, run_id: str) -> None:
        async with self._lock:
            self._runs.pop(run_id, None)
