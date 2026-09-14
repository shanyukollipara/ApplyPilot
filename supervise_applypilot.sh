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
trap 'rm -f "$PIDFILE" "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || true' EXIT

while true; do
  "$PYTHON" -m applypilot apply --continuous --workers 10 --headless --model gpt-5.6-luna >> "$LOG" 2>&1
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
  sleep 2
done
