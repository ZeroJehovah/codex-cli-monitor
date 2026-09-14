#!/usr/bin/env bash
set -euo pipefail
umask 077

# Restart a collector without systemd, retaining its private configuration.
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CODEX_MONITOR_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/codex-cli-monitor/collector.env}"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ -r "$CONFIG_FILE" ]] || fail "collector configuration is missing: $CONFIG_FILE"
set -a
source "$CONFIG_FILE"
set +a

[[ "${CODEX_MONITOR_COLLECTOR_ENABLED:-1}" == "1" ]] || fail "collector is disabled in its configuration"
[[ -n "${CODEX_MONITOR_AGGREGATOR_URL:-}" ]] || fail "CODEX_MONITOR_AGGREGATOR_URL is required"
[[ "$CODEX_MONITOR_AGGREGATOR_URL" =~ ^https?:// ]] || fail "aggregator URL must use HTTP or HTTPS"
[[ -n "${CODEX_MONITOR_COLLECTOR_TOKEN:-}" ]] || fail "CODEX_MONITOR_COLLECTOR_TOKEN is required"

PYTHON_BIN="${CODEX_MONITOR_PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null || fail "Python interpreter is unavailable"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-cli-monitor"
SERVER_ID="${CODEX_MONITOR_SERVER_ID:-$(hostname -s)}"

"$PYTHON_BIN" -m codex_cli_monitor --stop --pid-file "$STATE_DIR/codex-monitor.pid"
exec "$PYTHON_BIN" -m codex_cli_monitor \
  --serve --daemon --ws-enabled \
  --host "${CODEX_MONITOR_LOCAL_HOST:-127.0.0.1}" \
  --port "${CODEX_MONITOR_LOCAL_PORT:-8765}" \
  --server-id "$SERVER_ID" \
  --server-name "${CODEX_MONITOR_SERVER_NAME:-$SERVER_ID}" \
  --collector-url "$CODEX_MONITOR_AGGREGATOR_URL" \
  --collector-interval "${CODEX_MONITOR_COLLECTOR_INTERVAL:-0.5}" \
  --cache-seconds "${CODEX_MONITOR_LOCAL_CACHE_SECONDS:-0.05}" \
  --pid-file "$STATE_DIR/codex-monitor.pid" \
  --log-file "$STATE_DIR/codex-monitor.log"
