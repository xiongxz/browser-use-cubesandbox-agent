from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


def prepare_local_playwright_env() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    configured_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    project_default_path = (project_root / ".playwright-browsers").resolve()

    if configured_path:
        candidate_path = Path(configured_path).expanduser().resolve()
        if candidate_path.exists():
            browsers_path = candidate_path
        elif project_default_path.exists():
            browsers_path = project_default_path
        else:
            browsers_path = candidate_path
    else:
        browsers_path = project_default_path

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
    return browsers_path


def main() -> None:
    browsers_path = prepare_local_playwright_env()

    parser = argparse.ArgumentParser(description="Log in to Feishu manually and export Playwright storage_state.")
    parser.add_argument(
        "--output",
        default="feishu.storage_state.json",
        help="Path to write the exported storage_state JSON.",
    )
    parser.add_argument(
        "--url",
        default="https://feishu.cn",
        help="Initial URL to open for manual login.",
    )
    args = parser.parse_args()

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        print(f"Using Playwright browsers from: {browsers_path}")
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        print(f"Opened {args.url}")
        print("Please complete Feishu login in the browser window.")
        input("After login is complete and the target workspace is accessible, press Enter to export storage_state...")
        context.storage_state(path=str(output_path))
        browser.close()

    print(f"Saved storage_state to: {output_path}")


if __name__ == "__main__":
    main()
