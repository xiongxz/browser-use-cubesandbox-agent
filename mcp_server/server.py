"""Optional MCP sidecar for discovering the HTTP SSE protocol.

The actual business flow is exposed as HTTP SSE. The MCP sidecar intentionally
does not proxy browser runs; it only publishes the current protocol
contract as a small helper tool.

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

from mcp.server.fastmcp import FastMCP


_MCP_INSTRUCTIONS = (
    "This server accompanies the HTTP service. The public agent surface is the "
    "Feishu form-fill segmented SSE protocol: POST /v1/feishu/form-fill/run "
    "streams until the next ask_user_question or terminal event, and "
    "POST /v1/feishu/form-fill/runs/{run_id}/input feeds human confirmation, "
    "edits, supplements, or cancellation back into that run and returns the next stream segment."
)


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
        name="feishu_form_fill_sse_contract",
        description=(
            "Return the current HTTP SSE protocol contract for Feishu form fill. "
            "Use the HTTP endpoints directly for the actual segmented run."
        ),
    )
    async def feishu_form_fill_sse_contract() -> dict[str, Any]:
        return {
            "run_endpoint": "POST /v1/feishu/form-fill/run",
            "input_endpoint": "POST /v1/feishu/form-fill/runs/{run_id}/input",
            "events": [
                "run_started",
                "phase_started",
                "ask_user_question",
                "user_response_received",
                "phase_completed",
                "step_start",
                "step_end",
                "run_completed",
                "run_failed",
                "run_cancelled",
                "heartbeat",
            ],
            "input_decisions": ["confirm", "edit", "cancel"],
            "natural_language_examples": {
                "confirm": ["没问题", "OK", "没错"],
                "cancel": ["取消", "算了", "别提交"],
                "edit_or_supplement": ["人数改成 6", "换一个 case：王小明 6月3日 3人"],
            },
            "note": "Each endpoint returns one SSE segment. When ask_user_question arrives, render its payload; later POST question_id plus content.text or content.decision to input_endpoint to continue with another SSE segment.",
        }

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
