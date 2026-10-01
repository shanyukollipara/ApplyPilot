#!/bin/zsh
set -u

ROOT="/Users/Claw/Documents/ChatGPT/jobs/ApplyPilot"
SUPERVISOR="/Users/Claw/.applypilot/bin/supervise_applypilot.sh"
PYTHON="$ROOT/.venv/bin/python"
PIDFILE="/tmp/applypilot-live.pid"
MONITOR_LOCK="/tmp/applypilot-monitor.lock"
LOG="/tmp/applypilot-health.log"
WORKER_PATTERN="^$ROOT/.venv/bin/python -m applypilot apply --continuous"
LAUNCHD_LABEL="com.applypilot.supervisor"
HERMES="/Users/Claw/.local/bin/hermes"
HERMES_LOCK="/tmp/applypilot-hermes-qc.lock"
HERMES_LOG="/tmp/applypilot-hermes-qc.log"
EXPECTED_WORKERS=4
STALE_SECONDS=1800
CHECK_SECONDS=15
ROGUE_SESSION_PATTERN="/Users/Claw/.applypilot/sessions/run_session.py"
ROGUE_TMUX_PREFIX="run-"

cd "$ROOT"

if ! mkdir "$MONITOR_LOCK" 2>/dev/null; then
  lock_pid=$(cat "$MONITOR_LOCK/pid" 2>/dev/null || true)
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    exit 0
  fi
  rm -f "$MONITOR_LOCK/pid"
  rmdir "$MONITOR_LOCK" 2>/dev/null || exit 0
  mkdir "$MONITOR_LOCK" || exit 0
fi
print -r -- "$$" > "$MONITOR_LOCK/pid"
cleanup_monitor() {
  rm -f "$MONITOR_LOCK/pid"
  rmdir "$MONITOR_LOCK" 2>/dev/null || true
}
trap cleanup_monitor EXIT
trap 'exit 0' TERM INT

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

kill_apply_fleet() {
  pkill -TERM -f "$WORKER_PATTERN" 2>/dev/null || true
  for _ in {1..8}; do
    pgrep -f "$WORKER_PATTERN" >/dev/null 2>&1 || break
    sleep 1
  done
  pkill -KILL -f "$WORKER_PATTERN" 2>/dev/null || true
  pkill -TERM -f "Google Chrome for Testing --remote-debugging-port=93" 2>/dev/null || true
  pkill -TERM -f "^codex exec" 2>/dev/null || true
  sleep 1
  pkill -KILL -f "Google Chrome for Testing --remote-debugging-port=93" 2>/dev/null || true
  pkill -KILL -f "^codex exec" 2>/dev/null || true
}

kill_rogue_session_fleet() {
  # Campaign run_session.py is intentional. Never pkill it.
  print -r -- "$(date -Iseconds) campaign session fleet left running (kill is a no-op)" >> "$LOG"
  return 0
}

start_supervisor() {
  kill_apply_fleet
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
  export EXPECTED_WORKERS STALE_SECONDS WORKER_PATTERN
  "$PYTHON" - <<'PY'
import os
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta
from applypilot import config

expected = int(os.environ.get("EXPECTED_WORKERS", "5"))
stale_after = timedelta(seconds=int(os.environ.get("STALE_SECONDS", "1800")))
pattern = os.environ["WORKER_PATTERN"]

def matching_processes(prefix: str) -> list[str]:
    """Return PIDs whose executable command starts with the worker prefix.

    pgrep -f also matches the monitor's inspection command, which caused
    false duplicate/missing-worker restarts and multi-PID ps arguments.
    """
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True,
    )
    matches = []
    for line in result.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and command.startswith(prefix):
            matches.append(pid)
    return matches

def parse_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def parse_etime(value: str) -> int:
    # ps can emit more than one formatted row during a restart race.
    value = next((line.strip() for line in value.splitlines() if line.strip()), "")
    if not value:
        return 0
    days = 0
    if '-' in value:
        day_text, value = value.split('-', 1)
        days = int(day_text)
    fields = [int(part) for part in value.split(':')]
    if len(fields) == 3:
        hours, minutes, seconds = fields
    elif len(fields) == 2:
        hours, minutes, seconds = 0, *fields
    else:
        hours, minutes, seconds = 0, 0, fields[0]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds

apply_procs = matching_processes(pattern.removeprefix("^"))
chrome_ports = set()
for line in subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout.splitlines():
    marker = "--remote-debugging-port="
    if "Google Chrome for Testing" in line and marker in line:
        port = line.split(marker, 1)[1].split()[0]
        chrome_ports.add(port)
luna = 0
for line in subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout.splitlines():
    if line.startswith("codex exec") and "--model gpt-5.6-luna" in line:
        luna += 1

conn = sqlite3.connect(config.DB_PATH)
now = datetime.now(timezone.utc)
in_progress = conn.execute(
    "SELECT apply_worker, last_attempted_at FROM jobs WHERE apply_status = 'in_progress'"
).fetchall()
claimable = conn.execute(
    """
    SELECT COUNT(*) FROM jobs
     WHERE (apply_status IS NULL OR apply_status = 'failed')
       AND (apply_attempts IS NULL OR apply_attempts < ?)
    """
, (config.DEFAULTS["max_apply_attempts"],)
).fetchone()[0]
stale = 0
for _worker, attempted in in_progress:
    started = parse_iso(attempted)
    if started is not None and now - started > stale_after:
        stale += 1
conn.close()

active = max(len(in_progress), len(chrome_ports), luna)
# Luna dips for a few seconds between jobs; DB may have only one active claim
# even while all browser slots are healthy. One live Chrome is enough while
# leftover work is serialized one company at a time. Restart only if every
# fleet browser is gone.
missing = len(chrome_ports) == 0
if claimable == 0 and len(in_progress) == 0:
    print(
        f"ok:drained procs={len(apply_procs)} active={active} "
        f"db={len(in_progress)} chrome={len(chrome_ports)} luna={luna} stale=0"
    )
elif len(apply_procs) != 1:
    print(
        f"restart:duplicates procs={len(apply_procs)} active={active} "
        f"db={len(in_progress)} chrome={len(chrome_ports)} luna={luna} stale={stale}"
    )
elif stale:
    print(
        f"restart:stale procs=1 active={active} db={len(in_progress)} "
        f"chrome={len(chrome_ports)} luna={luna} stale={stale}"
    )
elif claimable > 0 and len(apply_procs) == 1 and missing:
    etime_output = subprocess.run(
        ["ps", "-p", apply_procs[0], "-o", "etime="],
        capture_output=True, text=True,
    ).stdout
    seconds = parse_etime(etime_output)
    if seconds >= 45:
        print(
            f"restart:missing procs=1 active={active} db={len(in_progress)} "
            f"chrome={len(chrome_ports)} luna={luna} stale=0"
        )
    else:
        print(
            f"ok:warming procs=1 active={active} db={len(in_progress)} "
            f"chrome={len(chrome_ports)} luna={luna} stale=0"
        )
else:
    print(
        f"ok:healthy procs=1 active={active} db={len(in_progress)} "
        f"chrome={len(chrome_ports)} luna={luna} stale=0"
    )
PY
}

missing_checks=0
last_qc=0
while true; do
  # Campaign run_session.py is allowed.

  pid=""
  [[ -f "$PIDFILE" ]] && pid=$(cat "$PIDFILE" 2>/dev/null || true)

  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    start_supervisor
    missing_checks=0
    print -r -- "$(date -Iseconds) restarted supervisor" >> "$LOG"
  else
    health=$(fleet_health)
    print -r -- "$(date -Iseconds) $health supervisor=$pid workers=$EXPECTED_WORKERS" >> "$LOG"
    if [[ "$health" == restart:duplicates* || "$health" == restart:stale* ]]; then
      start_supervisor
      missing_checks=0
    elif [[ "$health" == restart:missing* ]]; then
      (( missing_checks += 1 ))
      if (( missing_checks >= 2 )); then
        start_supervisor
        missing_checks=0
      fi
    else
      missing_checks=0
    fi
  fi

  now=$(date +%s)
  if (( now - last_qc >= 1800 )); then
    run_hermes_qc
    last_qc=$now
  fi
  sleep "$CHECK_SECONDS"
done
