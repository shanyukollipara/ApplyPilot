#!/bin/zsh
set -u

ROOT="/Users/Claw/Documents/ChatGPT/jobs/ApplyPilot"
PYTHON="$ROOT/.venv/bin/python"
LOG="/tmp/applypilot-live.log"
LOCK="/tmp/applypilot-supervisor.lock"
PIDFILE="/tmp/applypilot-live.pid"

cd "$ROOT"

if ! mkdir "$LOCK" 2>/dev/null; then
  lock_pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    exit 0
  fi
  rm -f "$LOCK/pid"
  rmdir "$LOCK" 2>/dev/null || exit 0
  mkdir "$LOCK" || exit 0
fi
print -r -- "$$" > "$LOCK/pid"
print -r -- "$$" > "$PIDFILE"

reset_orphans() {
  "$PYTHON" - <<'PY'
import sqlite3
from applypilot import config
from applypilot.apply.launcher import _sync_applications_csv

conn = sqlite3.connect(config.DB_PATH)
conn.execute("""
    UPDATE jobs
       SET apply_status = NULL,
           apply_error = NULL,
           agent_id = NULL
     WHERE apply_status = 'in_progress'
""")
conn.commit()
_sync_applications_csv(conn)
conn.close()
PY
}

child_pid=""
shutdown() {
  trap - EXIT TERM INT
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    for _ in {1..10}; do
      kill -0 "$child_pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$child_pid" 2>/dev/null || true
  fi
  pkill -KILL -f "$PYTHON -m applypilot apply --continuous" 2>/dev/null || true
  reset_orphans
  rm -f "$PIDFILE" "$LOCK/pid"
  rmdir "$LOCK" 2>/dev/null || true
  exit 0
}
trap shutdown EXIT TERM INT

while true; do
  work=$("$PYTHON" - <<'PY'
from applypilot.apply.launcher import fleet_has_apply_work, reset_retryable_failures
if fleet_has_apply_work():
    print("work")
else:
    requeued = reset_retryable_failures()
    print("work" if requeued or fleet_has_apply_work() else "idle")
PY
)
  if [[ "$work" != "work" ]]; then
    print -r -- "$(date -Iseconds) queue drained; workers idle" >> "$LOG"
    while true; do
      sleep 60
      still=$("$PYTHON" - <<'PY'
from applypilot.apply.launcher import fleet_has_apply_work
print("work" if fleet_has_apply_work() else "idle")
PY
)
      [[ "$still" == "work" ]] && break
    done
    continue
  fi
  unset CAPSOLVER_EXTENSION
  "$PYTHON" -m applypilot apply --continuous --workers 4 --headless --model gpt-5.6-luna >> "$LOG" 2>&1 &
  child_pid=$!
  wait "$child_pid" || true
  child_pid=""
  pkill -KILL -f "$PYTHON -m applypilot apply --continuous" 2>/dev/null || true
  reset_orphans
  sleep 2
done
