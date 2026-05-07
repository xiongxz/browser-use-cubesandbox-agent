#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export BROWSER_USE_CONFIG_DIR="${BROWSER_USE_CONFIG_DIR:-$ROOT_DIR/.browseruse-config}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT_DIR/.playwright-browsers}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-49999}"

exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
