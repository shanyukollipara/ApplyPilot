#!/bin/zsh
set -u

ROOT="/Users/Claw/Documents/ChatGPT/jobs/ApplyPilot"
SUPERVISOR="/Users/Claw/.applypilot/bin/supervise_applypilot.sh"
PYTHON="$ROOT/.venv/bin/python"
PIDFILE="/tmp/applypilot-live.pid"
LOG="/tmp/applypilot-health.log"
WORKER_PATTERN="$ROOT/.venv/bin/python -m applypilot apply --continuous"
LAUNCHD_LABEL="com.applypilot.supervisor"
HERMES="/Users/Claw/.local/bin/hermes"
HERMES_LOCK="/tmp/applypilot-hermes-qc.lock"
HERMES_LOG="/tmp/applypilot-hermes-qc.log"

cd "$ROOT"

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

start_supervisor() {
  # Never allow a dead supervisor and orphan worker to become a second queue.
  worker_pids=$(pgrep -f "$WORKER_PATTERN" | awk -v self="$$" '$1 != self' || true)
  [[ -z "$worker_pids" ]] || kill $worker_pids 2>/dev/null || true
  reset_orphans
  if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    if launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
      return
    fi
  fi
  nohup "$SUPERVISOR" >/tmp/applypilot-supervisor.out 2>&1 &
}

run_hermes_qc() {
  [[ -x "$HERMES" ]] || return
  if [[ -d "$HERMES_LOCK" ]]; then
    qc_pid=$(cat "$HERMES_LOCK/pid" 2>/dev/null || true)
    if [[ -z "$qc_pid" ]] || ! kill -0 "$qc_pid" 2>/dev/null; then
      rm -f "$HERMES_LOCK/pid"
      rmdir "$HERMES_LOCK" 2>/dev/null || true
    fi
  fi
  if mkdir "$HERMES_LOCK" 2>/dev/null; then
    (
      trap 'rm -f "$HERMES_LOCK/pid"; rmdir "$HERMES_LOCK" 2>/dev/null || true' EXIT
      "$HERMES" chat --oneshot -Q \
        --in "$ROOT" \
        --skills applypilot-operator \
        --yolo \
        --source tool \
        --run-budget 600 \
        --query-file "$ROOT/ops/hermes-qc-prompt.md" >> "$HERMES_LOG" 2>&1
    ) &
    print -r -- "$!" > "$HERMES_LOCK/pid"
  fi
}

while true; do
  pid=""
  [[ -f "$PIDFILE" ]] && pid=$(cat "$PIDFILE" 2>/dev/null || true)

  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    start_supervisor
    print -r -- "$(date -Iseconds) restarted supervisor" >> "$LOG"
  else
    print -r -- "$(date -Iseconds) healthy supervisor=$pid" >> "$LOG"
  fi

  run_hermes_qc
  sleep 1800
done
