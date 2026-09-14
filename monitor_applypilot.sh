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
STALE_SECONDS=900

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
  worker_pids=(${(f)"$(pgrep -f "$WORKER_PATTERN" | awk -v self="$$" '$1 != self' || true)"})
  if (( ${#worker_pids[@]} )); then
    kill -TERM "${worker_pids[@]}" 2>/dev/null || true
    for _ in {1..10}; do
      remaining=()
      for worker_pid in "${worker_pids[@]}"; do
        kill -0 "$worker_pid" 2>/dev/null && remaining+=("$worker_pid")
      done
      (( ${#remaining[@]} == 0 )) && break
      sleep 1
    done
    (( ${#remaining[@]} == 0 )) || kill -KILL "${remaining[@]}" 2>/dev/null || true
  fi
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

fleet_health() {
  "$PYTHON" - <<PY
import sqlite3
from datetime import datetime, timezone, timedelta
from applypilot import config

conn = sqlite3.connect(config.DB_PATH)
cutoff = (datetime.now(timezone.utc) - timedelta(seconds=$STALE_SECONDS)).isoformat()
stale = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE apply_status='in_progress' AND last_attempted_at < ?",
    (cutoff,),
).fetchone()[0]
missing = []
for worker in range(10):
    pending = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_worker=? AND apply_status IS NULL",
        (worker,),
    ).fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_worker=? AND apply_status='in_progress'",
        (worker,),
    ).fetchone()[0]
    if pending and not active:
        missing.append(str(worker))
conn.close()
print(stale, ",".join(missing) or "-")
PY
}

missing_checks=0
last_qc=0
while true; do
  pid=""
  [[ -f "$PIDFILE" ]] && pid=$(cat "$PIDFILE" 2>/dev/null || true)

  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    start_supervisor
    print -r -- "$(date -Iseconds) restarted supervisor" >> "$LOG"
  else
    health=$(fleet_health)
    stale_count=${health%% *}
    missing_workers=${health#* }
    if (( stale_count > 0 )); then
      print -r -- "$(date -Iseconds) stale workers=$stale_count; restarting" >> "$LOG"
      start_supervisor
      missing_checks=0
    elif [[ "$missing_workers" != "-" ]]; then
      (( missing_checks += 1 ))
      print -r -- "$(date -Iseconds) missing workers=$missing_workers check=$missing_checks" >> "$LOG"
      if (( missing_checks >= 3 )); then
        start_supervisor
        missing_checks=0
      fi
    else
      missing_checks=0
      print -r -- "$(date -Iseconds) healthy supervisor=$pid workers=10" >> "$LOG"
    fi
  fi

  now=$(date +%s)
  if (( now - last_qc >= 1800 )); then
    run_hermes_qc
    last_qc=$now
  fi
  sleep 60
done
