from __future__ import annotations

import json

from .models import StreamEvent


def encode_sse(event: StreamEvent) -> bytes:
    payload = json.dumps(event.model_dump(), ensure_ascii=False)
    return f"event: {event.event}\ndata: {payload}\n\n".encode("utf-8")
