from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read a Playwright storage_state JSON file, build the upload payload, and optionally POST it to the local agent service."
    )
    parser.add_argument(
        "--input",
        default="./tmp/feishu.storage_state.json",
        help="Path to the exported Playwright storage_state JSON file.",
    )
    parser.add_argument(
        "--output",
        default="./tmp/feishu-profile.upload.json",
        help="Path to write the generated upload payload JSON.",
    )
    parser.add_argument(
        "--profile-id",
        default="feishu-default",
        help="Profile id to save on the agent service.",
    )
    parser.add_argument(
        "--description",
        default="local feishu login",
        help="Human-readable description for the saved profile.",
    )
    parser.add_argument(
        "--server",
        default="http://127.0.0.1:50001",
        help="Agent service base URL.",
    )
    parser.add_argument(
        "--write-only",
        action="store_true",
        help="Only generate the upload payload JSON file and do not POST it to the server.",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        print(f"storage_state file not found: {input_path}", file=sys.stderr)
        return 1

    storage_state = json.loads(input_path.read_text(encoding="utf-8"))
    payload = {
        "profile_id": args.profile_id,
        "set_as_feishu_default": True,
        "description": args.description,
        "storage_state": storage_state,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated upload payload: {output_path}")

    if args.write_only:
        return 0

    request = urllib.request.Request(
        url=args.server.rstrip("/") + "/v1/auth/storage-state",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            print("Upload response:")
            print(body)
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"Upload failed with HTTP {exc.code}", file=sys.stderr)
        print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Upload failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
