"""Quick MCP health check.

The public business flow is HTTP SSE, not an MCP tool call. This client only
verifies that the optional MCP sidecar is reachable and exposes the current
SSE contract helper.

Usage:

    ./.venv/bin/python -m mcp_server.client_example

Server URL defaults to ``http://127.0.0.1:60000/mcp``. Override with
``--url`` or ``MCP_URL=...``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


DEFAULT_URL = os.getenv("MCP_URL", "http://127.0.0.1:60000/mcp")
EXPECTED_TOOL = "feishu_form_fill_sse_contract"


def _print_pass(msg: str) -> None:
    print(f"\033[92mPASS\033[0m {msg}")


def _print_fail(msg: str) -> None:
    print(f"\033[91mFAIL\033[0m {msg}")


async def _run(server_url: str) -> int:
    print(f"Connecting to MCP server: {server_url}")
    try:
        async with streamablehttp_client(server_url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                _print_pass(
                    f"MCP session initialized "
                    f"(server={init.serverInfo.name} v{init.serverInfo.version})"
                )
                tools = (await session.list_tools()).tools
                found = {tool.name for tool in tools}
                if EXPECTED_TOOL not in found:
                    _print_fail(f"missing expected tool: {EXPECTED_TOOL}")
                    print(f"registered tools: {sorted(found)}")
                    return 1

                _print_pass(f"tool registered: {EXPECTED_TOOL}")
                result = await session.call_tool(EXPECTED_TOOL, arguments={})
                if result.isError:
                    _print_fail("contract tool returned isError=true")
                    return 3
                for content in result.content:
                    text = getattr(content, "text", None)
                    if text:
                        print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
                        break
                return 0
    except Exception as exc:  # noqa: BLE001
        _print_fail(f"connection or session error: {type(exc).__name__}: {exc}")
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.url)))


if __name__ == "__main__":
    main()
