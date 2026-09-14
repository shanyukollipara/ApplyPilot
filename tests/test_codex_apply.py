import json
from pathlib import Path

import pytest

from applypilot.apply import launcher, codex_prompt as prompt
from applypilot.database import init_db


def _insert_job(conn, *, company: str, url: str, score: int = 10,
                title: str = "Software Engineer Intern"):
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, application_url, fit_score,
            tailored_resume_path, apply_attempts
        ) VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (url, title, company, url, score, "/tmp/resume.txt"),
    )
    conn.commit()


def test_acquire_job_never_selects_google_or_coinbase(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "jobs.db")
    _insert_job(conn, company="Google", url="https://google.example/apply")
    _insert_job(conn, company="Coinbase", url="https://coinbase.example/apply")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/apply", score=9)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))

    job = launcher.acquire_job(min_score=0)

    assert job is not None
    assert job["site"] == "Allowed Co"


@pytest.mark.parametrize("company", ["Google", "Coinbase"])
def test_target_url_cannot_bypass_company_exclusion(tmp_path, monkeypatch, company):
    conn = init_db(tmp_path / f"{company}.db")
    url = f"https://{company.lower()}.example/apply"
    _insert_job(conn, company=company, url=url)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.acquire_job(target_url=url, min_score=0) is None


def test_target_url_can_acquire_job_with_null_status(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "target-null.db")
    url = "https://allowed.example/apply"
    _insert_job(conn, company="Allowed Co", url=url)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)

    job = launcher.acquire_job(target_url=url, min_score=0)

    assert job is not None
    assert job["url"] == url


@pytest.mark.parametrize("target_url", [None, "https://allowed.example/Current-PhD-AI-Intern"])
def test_acquire_job_excludes_degree_required_roles_in_url(tmp_path, monkeypatch, target_url):
    conn = init_db(tmp_path / "degree-url.db")
    url = "https://allowed.example/Current-PhD-AI-Intern"
    _insert_job(conn, company="Allowed Co", url=url)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)

    assert launcher.acquire_job(target_url=target_url, min_score=0) is None


@pytest.mark.parametrize("marker", [
    "Graduate-Level-Co-op",
    "Graduate-Researcher-Program",
    "Graduate-Apprenticeship-Programme",
])
def test_acquire_job_excludes_graduate_roles_in_url(tmp_path, monkeypatch, marker):
    conn = init_db(tmp_path / "graduate-url.db")
    url = f"https://allowed.example/{marker}"
    _insert_job(conn, company="Allowed Co", url=url)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)

    assert launcher.acquire_job(target_url=url, min_score=0) is None


def test_acquire_job_excludes_graduate_level_role_in_title(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "graduate-title.db")
    url = "https://allowed.example/data-scientist"
    _insert_job(conn, company="Allowed Co", url=url,
                title="Data Scientist Graduate Level Co-op")
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)

    assert launcher.acquire_job(min_score=0) is None


@pytest.mark.parametrize("result", [
    "failed:job_requires_PhD",
    "failed:job_requires_masters",
    "failed:job_requires_MBA",
    "failed:job_requires_doctorate",
])
def test_degree_requirement_failures_are_permanent(result):
    assert launcher._is_permanent_failure(result)


def test_acquire_job_clears_stale_error_while_marking_in_progress(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "stale-error.db")
    url = "https://allowed.example/stale-error"
    _insert_job(conn, company="Allowed Co", url=url)
    conn.execute(
        "UPDATE jobs SET apply_status = 'failed', apply_error = 'old_failure' WHERE url = ?",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)

    assert launcher.acquire_job(min_score=0)["url"] == url
    row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert tuple(row) == ("in_progress", None)


def test_acquire_job_removes_stale_failed_csv_row(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "csv-cleanup.db")
    url = "https://allowed.example/csv-cleanup"
    _insert_job(conn, company="Allowed Co", url=url)
    conn.execute(
        "UPDATE jobs SET apply_status = 'failed', apply_error = 'old_failure' WHERE url = ?",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    sync_calls = []
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda db: sync_calls.append(db))

    assert launcher.acquire_job(min_score=0)["url"] == url
    assert sync_calls == [conn]


def test_acquire_job_finishes_pending_queue_before_retrying_failures(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "pending-before-retry.db")
    failed_url = "https://allowed.example/a-failed"
    pending_url = "https://allowed.example/z-pending"
    _insert_job(conn, company="Allowed Co", url=failed_url)
    _insert_job(conn, company="Allowed Co", url=pending_url)
    conn.execute(
        "UPDATE jobs SET apply_status = 'failed', apply_attempts = 1 WHERE url = ?",
        (failed_url,),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    assert launcher.acquire_job(min_score=0)["url"] == pending_url


def test_worker_loop_dry_run_does_not_record_result(monkeypatch):
    job = {"url": "https://allowed.example/dry-run", "title": "Dry run job"}
    results = iter([job])
    marked = []

    monkeypatch.setattr(launcher, "acquire_job", lambda **_: next(results, None))
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "run_job", lambda *args, **kwargs: ("failed:browser", 1))
    monkeypatch.setattr(launcher, "cleanup_browser_tabs", lambda *args: None)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args: None)
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: marked.append(args))
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    assert launcher.worker_loop(limit=1, dry_run=True) == (0, 0)
    assert marked == []


def test_playwright_mcp_is_pinned_and_is_the_only_mcp(tmp_path):
    secrets = tmp_path / ".secrets.env"
    config = launcher._make_mcp_config(9222, secrets)

    assert set(config["mcpServers"]) == {"playwright"}
    args = config["mcpServers"]["playwright"]["args"]
    assert "@playwright/mcp@0.0.80" in args
    assert not any("latest" in arg for arg in args)
    assert "--snapshot-mode=incremental" in args
    assert "--image-responses=omit" in args
    assert f"--secrets={secrets}" in args
    playwright = config["mcpServers"]["playwright"]
    assert playwright["default_tools_approval_mode"] == "approve"
    assert "browser_run_code_unsafe" not in playwright["enabled_tools"]
    assert "browser_evaluate" not in playwright["enabled_tools"]


def test_codex_command_is_ephemeral_read_only_and_ignores_user_config(tmp_path):
    command = launcher.build_codex_command(
        model="gpt-5.6-luna",
        port=9222,
        worker_dir=tmp_path,
        final_output_path=tmp_path / "final.txt",
        secrets_path=tmp_path / ".secrets.env",
    )
    rendered = " ".join(command)

    assert command[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--ephemeral" in command
    assert "--sandbox read-only" in rendered
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--disable shell_tool" in rendered
    assert 'web_search="disabled"' in rendered
    assert "tools.view_image=false" in rendered
    assert "--secrets=" in rendered
    assert "mcp_servers.playwright" in rendered
    assert "@playwright/mcp@0.0.80" in rendered
    assert "default_tools_approval_mode" in rendered
    assert "enabled_tools" in rendered
    assert "gmail" not in rendered.lower()
    assert "claude" not in rendered.lower()


def test_prompt_is_internship_safe_and_treats_pages_as_untrusted(tmp_path, monkeypatch):
    profile = json.loads(Path("/Users/Claw/.applypilot/profile.json").read_text())
    base = tmp_path / "resume"
    base.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    worker_dir = tmp_path / "workers"
    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)
    monkeypatch.setattr(prompt.config, "load_search_config", lambda: {})
    monkeypatch.setattr(prompt.config, "load_blocked_sso", lambda: [])
    monkeypatch.setattr(prompt.config, "APPLY_WORKER_DIR", worker_dir)

    text = prompt.build_prompt(
        job={
            "url": "https://allowed.example/apply",
            "application_url": "https://allowed.example/apply",
            "title": "Software Engineer Intern",
            "site": "Allowed Co",
            "fit_score": 10,
            "tailored_resume_path": str(base.with_suffix(".txt")),
        },
        tailored_resume="Candidate resume facts",
    )

    lower = text.lower()
    assert "$30/hour" in text
    assert 'plain "Desired salary" field does not specify a unit' in text
    assert "$62,400 USD only when the form explicitly requires an annual amount" in text
    assert "internship" in lower
    assert "untrusted" in lower
    assert "captcha" in lower and "do not solve" in lower
    assert "fill and verify every non-captcha field first" in lower
    assert "never use tab/shift+tab/arrow keys" in lower or "never use tab" in lower
    assert "workday / ats click rules" in lower
    assert "dismiss cookie" in lower or "accept cookies" in lower
    assert "unknown" in lower and "manual_question" in lower
    assert "google" in lower and "coinbase" in lower
    assert "full-time salaried positions only" not in lower
    assert "do whatever it takes" not in lower
    assert "send_email" not in lower
    assert "search_emails" not in lower
    assert "APPLYPILOT_PASSWORD" in text
    assert profile["personal"]["password"] not in text
    assert "create an employer portal account" in lower
    assert "gmail" in lower and "verification" in lower


def test_captcha_gate_resumes_from_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher, "_send_notification", lambda message: None)
    launcher._stop_event.clear()
    marker = tmp_path / "captcha-worker-4.resolved"
    marker.write_text("done")

    resumed = launcher._wait_for_captcha_resolution(
        {"site": "Example Co", "title": "Intern"}, worker_id=4
    )

    assert resumed is True
    assert not marker.exists()


def test_resumed_prompt_preserves_an_existing_valid_resume_field(tmp_path, monkeypatch):
    profile = json.loads(Path("/Users/Claw/.applypilot/profile.json").read_text())
    base = tmp_path / "resume"
    base.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)

    text = prompt.build_prompt(
        job={
            "url": "https://allowed.example/apply",
            "application_url": "https://allowed.example/apply",
            "title": "Software Engineer Intern",
            "site": "Allowed Co",
            "fit_score": 10,
            "tailored_resume_path": str(base.with_suffix(".txt")),
        },
        tailored_resume="Candidate resume facts",
        upload_dir=tmp_path / "worker",
        resume_current_page=True,
    )

    assert "Preserve the existing resume field" in text
    assert "Do not re-upload or switch its input mode" in text


def test_manual_questions_are_not_retried_until_answered():
    assert launcher._is_permanent_failure("failed:manual_question") is True
