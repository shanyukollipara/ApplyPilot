import json

from applypilot import hermes_learning


def test_record_outcome_is_sanitized_and_local(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    hermes_learning.record_outcome(
        job={
            "site": "Example Co",
            "title": "Software Engineer Intern",
            "application_url": "https://example.test/apply?token=secret",
            "resume": "must not be written",
        },
        status="failed",
        error="captcha",
        duration_ms=123,
    )

    path = tmp_path / "memories" / "applypilot-outcomes.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["host"] == "example.test"
    assert "token" not in record["host"]
    assert "secret" not in path.read_text(encoding="utf-8")
    assert "must not be written" not in path.read_text(encoding="utf-8")
