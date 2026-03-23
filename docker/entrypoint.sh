#!/usr/bin/env bash
set -euo pipefail

PORT="${STREAMLIT_PORT:-8503}"
DISPLAY_NAME="${DISPLAY:-:99}"

Xvfb "${DISPLAY_NAME}" -screen 0 1920x1080x24 -ac -nolisten tcp &
XVFB_PID=$!

cleanup() {
  if kill -0 "${XVFB_PID}" >/dev/null 2>&1; then
    kill "${XVFB_PID}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

exec streamlit run ui.py \
  --server.address=0.0.0.0 \
  --server.port="${PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
