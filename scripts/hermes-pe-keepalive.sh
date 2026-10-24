#!/usr/bin/env bash
# Keep the voice adapter running where there is no systemd.
#
# Meant for a cron entry every few minutes, or for docker's restart policy to sit on top
# of. It starts the daemon mode of the adapter, which keeps one ESPHome subscription open
# and therefore keeps the wake word armed on the device.
#
#   */2 * * * *  /path/to/repo/scripts/hermes-pe-keepalive.sh
#
# Exit codes: 0 when the adapter is running or was just started, 1 on a setup problem.
# Nothing is printed on the normal path, so cron stays quiet.

set -euo pipefail

REPO="${HERMES_PE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG="${HERMES_PE_CONFIG:-$REPO/config.json}"
ENV_FILE="${HERMES_PE_ENV:-$HOME/.config/voice-pe/env}"
LOG="${HERMES_PE_LOG:-$REPO/hermes-pe.log}"
PYTHON="${HERMES_PE_PYTHON:-$REPO/.venv/bin/python}"

fail() {
  echo "hermes-pe-keepalive: $*" >&2
  exit 1
}

[[ -d "$REPO" ]] || fail "repo not found: $REPO"
[[ -f "$CONFIG" ]] || fail "config not found: $CONFIG"
[[ -x "$PYTHON" ]] || fail "python not found: $PYTHON (create the venv or set HERMES_PE_PYTHON)"

# The key is exported, not merely assigned: an adapter without it starts, transcribes
# nothing and answers every turn with "Invalid API key". Better to refuse loudly here.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
[[ -n "${MISTRAL_API_KEY:-}" ]] || fail "MISTRAL_API_KEY is not set (looked in $ENV_FILE)"

# `pgrep -f` also matches the shell that runs this script, because the pattern is part of
# its command line. Requiring the candidate to be a python process is what tells the two
# apart; otherwise the watchdog reports itself as the adapter and never starts anything.
adapter_pid() {
  local pid comm
  for pid in $(pgrep -f "hermes_pe/bridge.py" 2>/dev/null || true); do
    [[ "$pid" == "$$" ]] && continue
    comm="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
    case "$comm" in
      python*) echo "$pid"; return 0 ;;
    esac
  done
  return 1
}

if pid="$(adapter_pid)"; then
  exit 0
fi

# Two schedulers firing at the same second would start two adapters, and two ESPHome
# clients split the microphone audio. The lock makes the second one lose.
exec 9>"$LOG.lock"
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || exit 0
fi

if adapter_pid >/dev/null; then
  exit 0
fi

mkdir -p "$(dirname "$LOG")"
# setsid detaches the daemon from this shell's session: a scheduler that exits right after
# starting it does not take it down. </dev/null because there is no terminal to read from.
cd "$REPO"
setsid nohup "$PYTHON" -u -m hermes_pe.bridge \
  --config "$CONFIG" \
  daemon >>"$LOG" 2>&1 </dev/null &

exit 0
