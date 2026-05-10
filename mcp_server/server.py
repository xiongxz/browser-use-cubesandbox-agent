"""MCP server that exposes the local HTTP API as named tools.

Mirrors ``schemas/mcp.tools.catalog.json`` — the tools have the same names,
signatures, and bodyTemplate semantics. Internally each tool POSTs to the
local FastAPI service over HTTP, so the MCP server is a thin adapter and the
FastAPI service stays the single source of agent behaviour.

Run modes:

- Started by the FastAPI app on the dedicated MCP port by default. Set
  ``ENABLE_MCP=false`` to disable it. See ``app/main.py``.
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
    "Use `browser_agent_run` for arbitrary browser automation. "
    "\n\n"
    "For the Feishu prebuilt questionnaire fill flow, there are TWO phases:\n"
    "1. Call `feishu_form_fill_prepare` FIRST - it uses the built-in preset form definition, "
    "parses the natural-language query into the three required fields (姓名, 参会时间, 参会人数), "
    "normalizes the meeting time into a human-readable value, and stops for human review. "
    "The response includes `draft_session_id`, `draft_session_expires_at`, and `draft_answers`.\n"
    "2. AFTER a human reviews and approves or edits those answers, call `feishu_form_fill_submit` "
    "with the `draft_session_id` from phase 1 plus any `confirmed_answers` overrides. "
    "The agent will reopen the same form, fill the confirmed answers, submit it, and return the submission result.\n"
    "IMPORTANT: if the user provides NEW supplemental information after phase 1, do NOT call submit yet. "
    "Instead call `feishu_form_fill_prepare` again with the updated natural-language request so the parse-confirm flow runs again.\n"
    "\n"
    "For the Feishu bitable -> questionnaire flow, there are TWO phases:\n"
    "1. Call `feishu_bitable_draft_form` FIRST - it opens the bitable, creates/opens "
    "the form editor, captures draft questions, and stops for human review. "
    "The response includes `draft_session_id` and `draft_session_expires_at`.\n"
    "2. AFTER a human reviews and approves, call `feishu_bitable_publish_form` with "
    "the `draft_session_id` from phase 1. The agent will find the existing form view, "
    "click 'Share Form', enable sharing, and return the final questionnaire URL.\n"
    "\n"
    "IMPORTANT: `draft_session_id` is REQUIRED for phase 2 and must match exactly. "
    "Do NOT call phase 2 before phase 1 completes successfully."
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


async def _post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    cleaned = {k: v for k, v in body.items() if v is not None}
    base = _proxy_base()
    timeout = _proxy_timeout()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base}{path}", json=cleaned)
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
        port=int(os.getenv("MCP_PORT", "60000")),
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
        return await _post_json(
            "/v1/agent/run",
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
        name="feishu_form_fill_prepare",
        description=(
            "Phase 1 of filling the built-in preset Feishu questionnaire from natural language. "
            "Parses `query` into the fixed fields 姓名 / 参会时间 / 参会人数, normalizes the meeting time into a human-readable value, "
            "and stops before submission for human review. Returns `draft_answers`, "
            "`draft_session_id`, and `draft_session_expires_at`."
        ),
    )
    async def feishu_form_fill_prepare(
        query: str,
        llm: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _post_json(
            "/v1/feishu/form-fill/prepare",
            {
                "query": query,
                "llm": llm,
            }
        )

    @server.tool(
        name="feishu_form_fill_submit",
        description=(
            "Phase 2 of filling the built-in preset Feishu questionnaire. ONLY call this AFTER "
            "`feishu_form_fill_prepare` returned a draft and a human has approved or corrected it with no additional free-form supplement. "
            "You MUST pass the exact `draft_session_id` from phase 1. Optional `confirmed_answers` "
            "override the drafted answers before the agent fills and submits the form. If the user adds new data in natural language, rerun phase 1 instead."
        ),
    )
    async def feishu_form_fill_submit(
        draft_session_id: str,
        human_confirmation_notes: str | None = None,
        confirmed_answers: list[dict[str, Any]] | None = None,
        field_ids: dict[str, str] | None = None,
        allowed_domains: list[str] | None = None,
        headless: bool | None = None,
        max_steps: int = 35,
        timeout_sec: int = 900,
        use_vision: str = "auto",
        llm: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _post_json(
            "/v1/feishu/form-fill/submit",
            {
                "draft_session_id": draft_session_id,
                "human_confirmation_notes": human_confirmation_notes,
                "confirmed_answers": confirmed_answers or [],
                "field_ids": field_ids or {},
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
        return await _post_json(
            "/v1/agent/run",
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
            "Phase 2 of the Feishu bitable -> questionnaire flow. ONLY call AFTER: "
            "(1) feishu_bitable_draft_form returned a draft, AND (2) a human has approved it. "
            "You MUST pass the exact draft_session_id from phase 1's response. "
            "The agent will: check if a form view exists (create it if needed by clicking 'Generate Form/生成表单'), "
            "enter the form editor, apply any human_confirmation_notes edits, "
            "click the 'Share Form/分享表单' button in the top-right, "
            "enable the 'Enable form sharing/开启表单分享' switch, "
            "wait ~2 seconds for the link to appear, and return the final shareable questionnaire URL in form_url."
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
        return await _post_json(
            "/v1/agent/run",
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
