"""MCP server that exposes the same HTTP endpoint as three named tools.

Mirrors ``schemas/mcp.tools.catalog.json`` — the tools have the same names,
signatures, and bodyTemplate semantics. Internally each tool POSTs to the
local ``/v1/agent/run`` endpoint over HTTP, so the MCP server is a thin
adapter and the FastAPI service stays the single source of agent behaviour.

Run modes:

- Mounted into the FastAPI app at ``/mcp`` (same port, default behaviour when
  ``ENABLE_MCP=true``). See ``app/main.py``.
- Standalone via ``python -m mcp_server.server`` for stdio (Claude Desktop /
  IDE clients) or ``--transport streamable-http`` to host on its own port.
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


logger = logging.getLogger(__name__)


_MCP_INSTRUCTIONS = (
    "This server wraps a Browser Use agent that drives a real Chromium browser. "
    "Use `browser_agent_run` for arbitrary browser automation. For the Feishu "
    "bitable -> questionnaire showcase, call `feishu_bitable_draft_form` first, "
    "wait for human review of the returned draft_questions, then call "
    "`feishu_bitable_publish_form` with the draft_session_id from the first "
    "response."
)


def _proxy_base() -> str:
    """HTTP base URL for the FastAPI service we delegate to. Defaults to the
    loopback so that when MCP is mounted on the same port everything stays
    in-process."""
    base = os.getenv("MCP_PROXY_BASE")
    if base:
        return base.rstrip("/")
    port = os.getenv("PORT", "49999")
    return f"http://127.0.0.1:{port}"


def _proxy_timeout() -> float:
    return float(os.getenv("MCP_PROXY_TIMEOUT_SEC", "1200"))


async def _post_run(body: dict[str, Any]) -> dict[str, Any]:
    cleaned = {k: v for k, v in body.items() if v is not None}
    base = _proxy_base()
    timeout = _proxy_timeout()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base}/v1/agent/run", json=cleaned)
        if response.status_code >= 400:
            logger.warning(
                "Proxied run returned %s: %s", response.status_code, response.text[:300]
            )
            response.raise_for_status()
        return response.json()


def build_server(*, streamable_http_path: str | None = None) -> FastMCP:
    """Build the FastMCP server.

    ``streamable_http_path`` controls the internal HTTP route inside the
    Starlette sub-app. When mounting into the FastAPI app this should be ``/``
    so that the outer mount path (``/mcp``) becomes the externally visible URL.
    When running standalone via ``mcp.run("streamable-http")`` we want the
    default ``/mcp`` so ``http://host:port/mcp`` lands on the MCP route.
    """

    if streamable_http_path is None:
        streamable_http_path = os.getenv("MCP_STREAMABLE_PATH", "/mcp")

    server = FastMCP(
        "browser-use-cubesandbox-agent",
        instructions=_MCP_INSTRUCTIONS,
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "49998")),
        streamable_http_path=streamable_http_path,
        stateless_http=True,
    )

    @server.tool(
        name="browser_agent_run",
        description=(
            "Run a general Browser Use task. Use for arbitrary browser automation "
            "when no Feishu-specific flow applies. Returns the structured run "
            "result including final_text, visited_urls, steps, and screenshots."
        ),
    )
    async def browser_agent_run(
        query: str,
        start_url: str | None = None,
        allowed_domains: list[str] | None = None,
        headless: bool | None = None,
        max_steps: int = 35,
        timeout_sec: int = 600,
        use_vision: str = "auto",
        llm: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _post_run(
            {
                "mode": "general",
                "query": query,
                "start_url": start_url,
                "allowed_domains": allowed_domains or [],
                "headless": headless,
                "max_steps": max_steps,
                "timeout_sec": timeout_sec,
                "use_vision": use_vision,
                "llm": llm,
                "auth": auth,
            }
        )

    @server.tool(
        name="feishu_bitable_draft_form",
        description=(
            "Phase 1 of the Feishu bitable -> questionnaire flow. Opens the "
            "bitable, switches into the built-in questionnaire/form editor, "
            "captures the visible draft questions, and stops for human review. "
            "Does NOT enable form sharing. The response carries `draft_session_id` "
            "and `draft_session_expires_at`; pass `draft_session_id` back into "
            "`feishu_bitable_publish_form` once a human has approved the draft. "
            "Embed the bitable URL in `query` (server auto-extracts) or pass "
            "`bitable_url` explicitly."
        ),
    )
    async def feishu_bitable_draft_form(
        query: str,
        bitable_url: str | None = None,
        allowed_domains: list[str] | None = None,
        headless: bool | None = None,
        max_steps: int = 35,
        timeout_sec: int = 600,
        use_vision: str = "auto",
        llm: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _post_run(
            {
                "mode": "feishu_bitable_to_form",
                "query": query,
                "bitable_url": bitable_url,
                "allowed_domains": allowed_domains or [],
                "headless": headless,
                "max_steps": max_steps,
                "timeout_sec": timeout_sec,
                "use_vision": use_vision,
                "llm": llm,
                "auth": auth,
                "require_human_confirmation": True,
                "human_confirmation_granted": False,
            }
        )

    @server.tool(
        name="feishu_bitable_publish_form",
        description=(
            "Phase 2 of the Feishu bitable -> questionnaire flow. Only call "
            "AFTER `feishu_bitable_draft_form` has returned a draft and a human "
            "has approved it. Pass the `draft_session_id` from phase 1's "
            "response. The server validates the session, applies any "
            "`human_confirmation_notes`, opens 分享表单 / 开启表单分享, and "
            "returns the real shareable questionnaire URL in `form_url`."
        ),
    )
    async def feishu_bitable_publish_form(
        query: str,
        draft_session_id: str,
        bitable_url: str | None = None,
        human_confirmation_notes: str | None = None,
        allowed_domains: list[str] | None = None,
        headless: bool | None = None,
        max_steps: int = 35,
        timeout_sec: int = 900,
        use_vision: str = "auto",
        llm: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _post_run(
            {
                "mode": "feishu_bitable_to_form",
                "query": query,
                "bitable_url": bitable_url,
                "draft_session_id": draft_session_id,
                "human_confirmation_notes": human_confirmation_notes,
                "allowed_domains": allowed_domains or [],
                "headless": headless,
                "max_steps": max_steps,
                "timeout_sec": timeout_sec,
                "use_vision": use_vision,
                "llm": llm,
                "auth": auth,
                "require_human_confirmation": True,
                "human_confirmation_granted": True,
            }
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MCP server standalone.")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="Which MCP transport to serve on. Default: stdio (for IDE/desktop clients).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    server = build_server()
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
