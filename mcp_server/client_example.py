"""Quick MCP client to verify the local MCP server is healthy and to drive
case-file-based tool calls.

Three usage modes:

1. List tools only (default; no upstream calls).

       ./.venv/bin/python -m mcp_server.client_example

2. Run one or more case files from ``examples/mcp/``.

       ./.venv/bin/python -m mcp_server.client_example \\
         --case examples/mcp/browser_agent_run.example_com.json

       ./.venv/bin/python -m mcp_server.client_example \\
         --case examples/mcp/feishu_form_fill_prepare.json \\
         --case examples/mcp/feishu_form_fill_submit.json

   When cases are run sequentially, a ``draft_session_id`` returned by phase 1
   is automatically injected into later case files that still contain a
   ``REPLACE_WITH_...`` placeholder. This lets the Feishu form-fill prepare +
   submit flow run end-to-end from one command.

3. Override case-file fields at the CLI (handy for ``draft_session_id``).

       ./.venv/bin/python -m mcp_server.client_example \\
         --case examples/mcp/feishu_bitable_publish_form.json \\
         --set draft_session_id=c18ecbc2-f8ea-4afd-9a33-4ee3ca4f739c

Override values are parsed as JSON when possible (so ``--set max_steps=10``
becomes an int and ``--set auth='{"profile_id":"alt"}'`` becomes a dict),
otherwise treated as a plain string.

Case file shape:

    {
      "tool": "<tool name>",
      "title": "<one-line description, optional>",
      "notes": "<longer note, optional>",
      "arguments": { ... }
    }

Server URL defaults to ``http://127.0.0.1:60000/mcp`` (the dedicated MCP port
when running the FastAPI app with ``ENABLE_MCP=true``). Override with
``--url`` or ``MCP_URL=...``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


DEFAULT_URL = os.getenv("MCP_URL", "http://127.0.0.1:60000/mcp")
EXPECTED_TOOLS = {
    "browser_agent_run",
    "feishu_form_fill_prepare",
    "feishu_form_fill_submit",
    "feishu_bitable_draft_form",
    "feishu_bitable_publish_form",
}


def _print_pass(msg: str) -> None:
    print(f"\033[92mPASS\033[0m {msg}")


def _print_fail(msg: str) -> None:
    print(f"\033[91mFAIL\033[0m {msg}")


def _print_info(msg: str) -> None:
    print(f"     {msg}")


def _print_section(msg: str) -> None:
    print(f"\n=== {msg} ===")


# ---- case file plumbing -------------------------------------------------- #


def _parse_set_value(raw: str) -> Any:
    """Accept JSON literal first, fall back to plain string."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _load_case(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"case file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "tool" not in data or "arguments" not in data:
        raise ValueError(f"{path}: case must define `tool` and `arguments`")
    if not isinstance(data["arguments"], dict):
        raise ValueError(f"{path}: `arguments` must be an object")
    return data


def _split_overrides(raw: list[str]) -> list[tuple[str, Any]]:
    """Pre-parse ``--set KEY=VALUE`` entries. Raises ValueError fast (before
    we open the MCP session) so users see a clear message rather than a
    TaskGroup-wrapped one."""
    parsed: list[tuple[str, Any]] = []
    for entry in raw:
        if "=" not in entry:
            raise ValueError(f"--set expects KEY=VALUE, got: {entry!r}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--set has empty key in: {entry!r}")
        parsed.append((key, _parse_set_value(value)))
    return parsed


def _apply_overrides(arguments: dict[str, Any], overrides: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(arguments)
    for key, value in overrides:
        out[key] = value
    return out


def _inject_context(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Replace common placeholders with values captured from previous cases."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            if "REPLACE_WITH_draft_session_id" in value and context.get("draft_session_id"):
                return context["draft_session_id"]
            return value
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(arguments)


def _detect_placeholders(arguments: dict[str, Any]) -> list[str]:
    """Walk arguments and report keys whose string values still contain a
    REPLACE_WITH_ marker. Helps users notice they forgot to fill in
    draft_session_id etc."""

    bad: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, str) and "REPLACE_WITH_" in value:
            bad.append(prefix)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(f"{prefix}[{i}]", v)

    for k, v in arguments.items():
        walk(k, v)
    return bad


# ---- result interpretation ---------------------------------------------- #


def _decode_payload(result: Any) -> dict[str, Any] | None:
    for content in result.content:
        text = getattr(content, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def _summarize_result(tool: str, payload: dict[str, Any]) -> bool:
    """Return True on a successful or awaiting-confirmation outcome."""

    awaiting = payload.get("awaiting_human_confirmation")
    success = payload.get("success")
    duration = payload.get("duration_sec")
    steps = payload.get("steps")

    if awaiting:
        sid = payload.get("draft_session_id")
        _print_pass(
            f"{tool}: phase 1 returned a draft for human review"
            + (f" (steps={steps}, duration={duration}s)" if steps is not None else "")
        )
        if sid:
            _print_info(f"draft_session_id: {sid}")
            expires = payload.get("draft_session_expires_at")
            if expires:
                _print_info(f"expires_at:       {expires}")
        draft_items = payload.get("draft_questions") or payload.get("draft_answers") or []
        for q in draft_items[:5]:
            idx = q.get("index")
            title = q.get("title") or q.get("field_label")
            qtype = q.get("question_type")
            required = q.get("required")
            extras = []
            if qtype:
                extras.append(qtype)
            if required is not None:
                extras.append("required" if required else "optional")
            tag = f" [{', '.join(extras)}]" if extras else ""
            answer = q.get("proposed_value")
            suffix = f" -> {answer}" if answer else ""
            _print_info(f"  Q{idx}: {title}{tag}{suffix}")
        if (more := len(draft_items)) > 5:
            _print_info(f"  ... and {more - 5} more")
        if sid:
            print()
            next_case = (
                "examples/mcp/feishu_form_fill_submit.json"
                if tool == "feishu_form_fill_prepare"
                else "examples/mcp/feishu_bitable_publish_form.json"
            )
            _print_info(
                "Next step (phase 2): "
                f"--case {next_case} "
                f"--set draft_session_id={sid}"
            )
        return True

    if success:
        msg = f"{tool}: success"
        if steps is not None:
            msg += f" (steps={steps}, duration={duration}s)"
        _print_pass(msg)
        if (form_url := payload.get("form_url")):
            _print_info(f"form_url: {form_url}")
        if (submission_result := payload.get("submission_result")):
            submission_result = str(submission_result).strip().replace("\n", " ")
            _print_info(f"submission_result: {submission_result[:200]}")
        if (final := payload.get("final_text")):
            final = final.strip().replace("\n", " ")
            _print_info(f"final_text: {final[:200]}")
        return True

    _print_fail(f"{tool}: success=false")
    if (final := payload.get("final_text")):
        _print_info(f"final_text: {str(final)[:240]}")
    for err in (payload.get("errors") or [])[:3]:
        _print_info(f"error: {str(err)[:200]}")
    return False


async def _call_case(
    session: ClientSession,
    case_path: Path,
    overrides: list[tuple[str, Any]],
    context: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    case = _load_case(case_path)
    tool = case["tool"]
    title = case.get("title") or ""
    arguments = _inject_context(_apply_overrides(case["arguments"], overrides), context)

    bad = _detect_placeholders(arguments)
    if bad:
        _print_fail(f"{case_path.name}: placeholder values not filled in: {bad}")
        _print_info(
            "Fix the file in place, or pass --set "
            + ", --set ".join(f"{k}=<value>" for k in bad)
        )
        return False, None

    if tool not in EXPECTED_TOOLS:
        _print_info(
            f"{case_path.name}: tool '{tool}' is not one of "
            f"{sorted(EXPECTED_TOOLS)}; will still attempt the call."
        )

    print()
    print(f">>> {case_path.name}")
    if title:
        print(f"    {title}")
    print(f"    tool: {tool}")
    print(f"    arguments: {json.dumps(arguments, ensure_ascii=False)[:240]}")

    try:
        result = await session.call_tool(tool, arguments=arguments)
    except Exception as exc:  # noqa: BLE001
        _print_fail(f"call_tool raised: {type(exc).__name__}: {exc}")
        return False, None

    if result.isError:
        _print_fail(f"{tool}: tool reported isError=true")
        for c in result.content:
            txt = getattr(c, "text", None)
            if txt:
                _print_info(txt[:300])
        return False, None

    payload = _decode_payload(result)
    if payload is None:
        _print_fail(f"{tool}: could not decode JSON payload from tool result")
        for c in result.content:
            _print_info(repr(c)[:200])
        return False, None

    ok = _summarize_result(tool, payload)
    if payload.get("draft_session_id"):
        context["draft_session_id"] = payload["draft_session_id"]
    return ok, payload


# ---- list_tools and main loop ------------------------------------------- #


async def _list_tools(session: ClientSession) -> bool:
    result = await session.list_tools()
    tools = result.tools
    found = {t.name for t in tools}
    missing = EXPECTED_TOOLS - found

    if missing:
        _print_fail(f"missing expected tools: {sorted(missing)}")
        return False
    if not tools:
        _print_fail("no tools registered")
        return False

    _print_pass(f"all {len(EXPECTED_TOOLS)} expected tools registered")

    print()
    print(f"Tools registered ({len(tools)}):")
    for t in tools:
        desc = (t.description or "").replace("\n", " ").strip()
        if len(desc) > 110:
            desc = desc[:107] + "..."
        print(f"  - {t.name}")
        print(f"      {desc}")
    return True


async def _run(server_url: str, case_paths: list[Path], overrides: list[tuple[str, Any]]) -> int:
    print(f"Connecting to MCP server: {server_url}")
    try:
        async with streamablehttp_client(server_url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                _print_pass(
                    f"MCP session initialized "
                    f"(server={init.serverInfo.name} v{init.serverInfo.version})"
                )
                _print_section("list_tools")
                tools_ok = await _list_tools(session)
                if not tools_ok:
                    return 1

                if not case_paths:
                    return 0

                _print_section(f"call_tool ({len(case_paths)} case(s))")
                all_ok = True
                context: dict[str, Any] = {}
                for path in case_paths:
                    case_ok, _payload = await _call_case(session, path, overrides, context)
                    all_ok = all_ok and case_ok
                return 0 if all_ok else 3
    except Exception as exc:  # noqa: BLE001
        _print_fail(f"connection or session error: {type(exc).__name__}: {exc}")
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"MCP streamable HTTP endpoint (default: {DEFAULT_URL}).",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="PATH",
        help="Path to a case JSON. Can be repeated to run several cases sequentially.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="overrides",
        metavar="KEY=VALUE",
        help="Override a case-file argument. VALUE is parsed as JSON when possible. Repeatable.",
    )
    args = parser.parse_args()

    case_paths: list[Path] = []
    for raw in args.case:
        path = Path(raw).expanduser()
        if not path.exists():
            print(f"FAIL: case file not found: {path}", file=sys.stderr)
            sys.exit(4)
        case_paths.append(path)

    try:
        overrides = _split_overrides(args.overrides)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(5)

    code = asyncio.run(_run(args.url, case_paths, overrides))
    sys.exit(code)


if __name__ == "__main__":
    main()
