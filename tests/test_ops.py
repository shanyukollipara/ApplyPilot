import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launch_agents_are_job_dictionaries():
    for name in ("supervisor", "monitor", "caffeinate"):
        path = ROOT / "ops" / f"com.applypilot.{name}.plist"
        with path.open("rb") as handle:
            job = plistlib.load(handle)
        assert isinstance(job, dict)
        assert job["Label"] == f"com.applypilot.{name}"
        assert isinstance(job["ProgramArguments"], list)
        assert job["KeepAlive"] is True
        assert job["RunAtLoad"] is True


def test_supervisor_starts_four_headless_luna_workers():
    text = (ROOT / "supervise_applypilot.sh").read_text(encoding="utf-8")
    assert "--workers 4" in text
    assert "--headless" in text
    assert "CAPSOLVER_EXTENSION=1" not in text
    assert "--model gpt-5.6-luna" in text
    assert "queue drained; workers idle" in text
    assert "fleet_has_apply_work" in text


def test_monitor_checks_quickly_and_kills_duplicate_fleets():
    text = (ROOT / "monitor_applypilot.sh").read_text(encoding="utf-8")
    assert "CHECK_SECONDS=15" in text
    assert "EXPECTED_WORKERS=4" in text
    assert "STALE_SECONDS=1800" in text
    assert 'timedelta(seconds=int(os.environ.get("STALE_SECONDS", "1800")))' in text
    assert 'int(os.environ.get("EXPECTED_WORKERS", "5"))' in text
    assert 'WORKER_PATTERN="^$ROOT/.venv/bin/python -m applypilot apply --continuous"' in text
    assert "kill_apply_fleet" in text
    assert "missing = len(chrome_ports) == 0" in text
    assert '["ps", "-p", apply_procs[0], "-o", "etime="]' in text
    assert "seconds = parse_etime(etime_output)" in text
    assert "apply_attempts IS NULL OR apply_attempts < ?" in text
    assert '(config.DEFAULTS["max_apply_attempts"],)' in text
    assert "ok:drained" in text


def test_monitor_leaves_campaign_session_fleet_running():
    text = (ROOT / "monitor_applypilot.sh").read_text(encoding="utf-8")
    assert 'ROGUE_SESSION_PATTERN="/Users/Claw/.applypilot/sessions/run_session.py"' in text
    assert "kill_rogue_session_fleet" in text
    assert "kill is a no-op" in text
    assert 'pkill -TERM -f "$ROGUE_SESSION_PATTERN"' not in text
    assert 'pkill -TERM -f "$WORKER_PATTERN"' in text
