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


def test_supervisor_starts_ten_headless_luna_workers():
    text = (ROOT / "supervise_applypilot.sh").read_text(encoding="utf-8")
    assert "--workers 10" in text
    assert "--headless" in text
    assert "--model gpt-5.6-luna" in text
