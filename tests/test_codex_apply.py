import json
import subprocess
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


def test_acquire_job_does_not_exclude_companies(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "jobs.db")
    _insert_job(conn, company="Google", url="https://google.example/apply")
    _insert_job(conn, company="Oracle", url="https://careers.oracle.com/apply")
    _insert_job(conn, company="TikTok", url="https://lifeattiktok.com/search/1")

    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))

    job = launcher.acquire_job(min_score=0)

    assert job is not None
    assert job["site"] in {"Google", "Oracle", "TikTok"}


def test_acquire_job_can_select_coinbase(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "coinbase.db")
    _insert_job(conn, company="Coinbase", url="https://coinbase.example/apply")
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)

    job = launcher.acquire_job(min_score=0)

    assert job is not None
    assert job["site"] == "Coinbase"


@pytest.mark.parametrize("company", ["Google", "Oracle", "TikTok"])
def test_target_url_can_acquire_any_company(tmp_path, monkeypatch, company):
    conn = init_db(tmp_path / f"{company}.db")
    url = f"https://{company.lower()}.example/apply"
    _insert_job(conn, company=company, url=url)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    job = launcher.acquire_job(target_url=url, min_score=0)

    assert job is not None
    assert job["site"] == company


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


def test_sync_does_not_replace_populated_csv_with_header_only(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "csv-preserve.db")
    app_dir = tmp_path / "app"
    destination = app_dir / "applications.csv"
    destination.parent.mkdir()
    destination.write_text(
        "applied_at,site,title,application_url,url,apply_status,apply_error\n"
        ",Allowed Co,Existing,https://example/apply,https://example,failed,old\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher.config, "APP_DIR", app_dir)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "src/applypilot/apply/launcher.py"))

    launcher._sync_applications_csv(conn)

    assert destination.read_text(encoding="utf-8").count("\n") == 2


def test_acquire_job_claims_next_job_after_manual_ats(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "manual-skip.db")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/a-manual")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/z-next")
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda url: "a-manual" in url)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    job = launcher.acquire_job(min_score=0)

    assert job["url"] == "https://allowed.example/z-next"
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url = ?",
        ("https://allowed.example/a-manual",),
    ).fetchone()[0] == "manual"


def test_reset_retryable_failures_requeues_failed_jobs_at_end(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "retryable-reset.db")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/timeout")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/expired")
    _insert_job(
        conn,
        company="Allowed Co",
        url="https://allowed.example/phd-intern",
        title="Research Intern PhD",
    )
    _insert_job(conn, company="Vanity Co", url="https://careers.example.com/x?icims=1")
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='timeout', apply_attempts=2, "
        "apply_worker=3 WHERE url = ?",
        ("https://allowed.example/timeout",),
    )
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='expired', apply_attempts=99, "
        "apply_worker=3 WHERE url = ?",
        ("https://allowed.example/expired",),
    )
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='job_requires_PhD', "
        "apply_attempts=99, apply_worker=3 WHERE url = ?",
        ("https://allowed.example/phd-intern",),
    )
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='timeout', apply_attempts=2, "
        "apply_worker=3 WHERE url LIKE '%icims=%'"
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    assert launcher.reset_retryable_failures() == 1
    rows = {
        row["url"]: (row["apply_status"], row["apply_attempts"], row["apply_error"], row["apply_worker"])
        for row in conn.execute(
            "SELECT url, apply_status, apply_attempts, apply_error, apply_worker FROM jobs"
        )
    }
    assert rows["https://allowed.example/timeout"] == ("failed", 0, "timeout", 3)
    assert rows["https://allowed.example/expired"] == ("failed", 99, "expired", 3)
    assert rows["https://careers.example.com/x?icims=1"][1] == 2


def test_requeue_unfinished_non_icims_skips_holds(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "requeue-leftover.db")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/timeout")
    _insert_job(conn, company="TikTok", url="https://lifeattiktok.com/search/1")
    _insert_job(conn, company="Vanity Co", url="https://careers.example.com/x?icims=1")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/applied-already")
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='timeout', apply_attempts=3 "
        "WHERE url = ?",
        ("https://allowed.example/timeout",),
    )
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='site_blocked', apply_attempts=99 "
        "WHERE url LIKE '%lifeattiktok%'"
    )
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='timeout', apply_attempts=3 "
        "WHERE url LIKE '%icims=%'"
    )
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='already_applied', "
        "apply_attempts=3 WHERE url LIKE '%applied-already%'"
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: (["tiktok", "TikTok"], ["%lifeattiktok.com%"]))
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    assert launcher.requeue_unfinished_non_icims() == 1
    rows = {
        row["url"]: (row["apply_attempts"], row["apply_error"])
        for row in conn.execute("SELECT url, apply_attempts, apply_error FROM jobs")
    }
    assert rows["https://allowed.example/timeout"] == (0, "timeout")
    assert rows["https://lifeattiktok.com/search/1"][0] == 99
    assert rows["https://careers.example.com/x?icims=1"][0] == 3
    assert rows["https://allowed.example/applied-already"][0] == 3


def test_eligible_apply_count_skips_icims(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "eligible-icims.db")
    _insert_job(conn, company="Workday Co", url="https://company.myworkdayjobs.com/job")
    _insert_job(conn, company="Vanity Co", url="https://careers.example.com/jobs/2?icims=1")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    assert launcher.eligible_apply_count() == 1


def test_drain_idle_queue_reloads_retryable_then_stops(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "drain.db")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/timeout")
    conn.execute(
        "UPDATE jobs SET apply_status='failed', apply_error='timeout', apply_attempts=2"
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    launcher._idle_retry_done = False
    assert launcher.drain_idle_queue() == "work"
    conn.execute("UPDATE jobs SET apply_attempts=99")
    conn.commit()
    assert launcher.drain_idle_queue() == "stop"


def test_close_ineligible_jobs_marks_graduate_and_blocked_rows(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "close-ineligible.db")
    _insert_job(
        conn,
        company="Allowed Co",
        url="https://allowed.example/phd-intern",
        title="Research Intern PhD",
    )
    _insert_job(
        conn,
        company="Edison International",
        url="https://apply.edisoncareers.com/job/123",
        title="Computer Science Intern",
    )
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/keep")
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        launcher,
        "_load_blocked",
        lambda: (["Edison International"], ["%edisoncareers%"]),
    )
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr("applypilot.apply.icims.HOST_POLICY_PATH", tmp_path / "hosts.json")

    assert launcher.close_ineligible_jobs() >= 2
    statuses = {
        row["url"]: (row["apply_status"], row["apply_error"])
        for row in conn.execute("SELECT url, apply_status, apply_error FROM jobs")
    }
    assert statuses["https://allowed.example/phd-intern"][0] == "failed"
    assert statuses["https://apply.edisoncareers.com/job/123"] == ("failed", "site_blocked")
    assert statuses["https://allowed.example/keep"] == (None, None)


def test_acquire_job_finishes_pending_before_retrying_failed_jobs(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "pending-before-retry.db")
    failed_url = "https://allowed.example/a-failed"
    pending_url = "https://allowed.example/z-pending"
    _insert_job(conn, company="Allowed Co", url=failed_url)
    _insert_job(conn, company="Allowed Co", url=pending_url)
    conn.execute(
        "UPDATE jobs SET apply_status = 'failed', apply_attempts = 0 WHERE url = ?",
        (failed_url,),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    assert launcher.acquire_job(min_score=0)["url"] == pending_url


def test_acquire_job_prioritizes_robinhood_then_existing_before_new_simplify(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "robinhood-first.db")
    _insert_job(conn, company="Older Co", url="https://allowed.example/existing")
    _insert_job(conn, company="Robinhood", url="https://boards.greenhouse.io/robinhood/jobs/1")
    _insert_job(conn, company="New Co", url="https://allowed.example/new-simplify")
    conn.execute(
        "UPDATE jobs SET strategy = 'simplify-summer-2027' WHERE url LIKE '%new-simplify%'"
    )
    conn.execute(
        "UPDATE jobs SET strategy = 'simplify-summer-2027' WHERE url LIKE '%robinhood%'"
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    first = launcher.acquire_job(min_score=0)
    assert first["site"] == "Robinhood"
    conn.execute("UPDATE jobs SET apply_status = 'applied' WHERE url = ?", (first["url"],))
    conn.commit()
    second = launcher.acquire_job(min_score=0)
    assert second["url"] == "https://allowed.example/existing"


def test_acquire_job_shared_queue_takes_workday_before_another_workers_icims(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "shared-queue.db")
    _insert_job(conn, company="Workday Co", url="https://company.myworkdayjobs.com/en-US/job")
    _insert_job(conn, company="Icims Co", url="https://careers-icims.com/jobs/1")
    conn.execute("UPDATE jobs SET apply_worker = 0 WHERE url LIKE '%workday%'")
    conn.execute("UPDATE jobs SET apply_worker = 1 WHERE url LIKE '%icims%'")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr(launcher, "_worker_count", 2)

    job = launcher.acquire_job(min_score=0, worker_id=1)

    assert "workday" in job["url"]
    assert conn.execute(
        "SELECT apply_worker FROM jobs WHERE url LIKE '%workday%'"
    ).fetchone()[0] == 1


def test_acquire_job_skips_classic_icims_and_vanity(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "skip-icims.db")
    _insert_job(conn, company="Icims Co", url="https://a.icims.com/jobs/1")
    _insert_job(conn, company="Vanity Icims", url="https://careers.example.com/jobs/2?icims=1")
    _insert_job(conn, company="Workday Co", url="https://company.myworkdayjobs.com/en-US/job")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)

    job = launcher.acquire_job(min_score=0)

    assert "workday" in job["url"]
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url LIKE '%icims.com%'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url LIKE '%icims=%'"
    ).fetchone()[0] is None


def test_acquire_job_never_claims_vanity_icims(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "vanity-icims.db")
    _insert_job(conn, company="Icims Co", url="https://a.icims.com/jobs/1")
    _insert_job(conn, company="Vanity Icims", url="https://careers.example.com/jobs/2?icims=1")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr("applypilot.apply.icims.blocked_hostnames", lambda: [])

    job = launcher.acquire_job(min_score=0)
    assert job is None
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url LIKE '%icims.com%'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url LIKE '%icims=%'"
    ).fetchone()[0] is None


def test_acquire_job_skips_company_already_in_progress_for_another_worker(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "busy-company.db")
    _insert_job(conn, company="Same Co", url="https://allowed.example/active")
    _insert_job(conn, company="Same Co", url="https://allowed.example/owned-1-same")
    _insert_job(conn, company="Other Co", url="https://allowed.example/owned-1-other")
    conn.execute(
        "UPDATE jobs SET apply_worker = 0, apply_status = 'in_progress' WHERE url LIKE '%/active'"
    )
    conn.execute("UPDATE jobs SET apply_worker = 1 WHERE url LIKE '%owned-1-%'")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr(launcher, "_worker_count", 2)

    job = launcher.acquire_job(min_score=0, worker_id=1)

    assert job["url"] == "https://allowed.example/owned-1-other"


def test_acquire_job_allows_parallel_robinhood_while_another_is_in_progress(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "robinhood-parallel.db")
    _insert_job(conn, company="Robinhood", url="https://boards.greenhouse.io/robinhood/jobs/1")
    _insert_job(conn, company="Robinhood", url="https://boards.greenhouse.io/robinhood/jobs/2")
    conn.execute(
        "UPDATE jobs SET apply_worker = 0, apply_status = 'in_progress' WHERE url LIKE '%/jobs/1'"
    )
    conn.execute("UPDATE jobs SET apply_worker = 1 WHERE url LIKE '%/jobs/2'")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr(launcher, "_worker_count", 2)

    job = launcher.acquire_job(min_score=0, worker_id=1)

    assert job["url"] == "https://boards.greenhouse.io/robinhood/jobs/2"


def test_acquire_job_resumes_existing_in_progress_instead_of_claiming_another(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "resume-in-progress.db")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/active")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/next")
    conn.execute("UPDATE jobs SET apply_worker = 0")
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress' WHERE url LIKE '%/active'"
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher.config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr(launcher, "_worker_count", 2)

    job = launcher.acquire_job(min_score=0, worker_id=0)

    assert job["url"] == "https://allowed.example/active"
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'in_progress'"
    ).fetchone()[0] == 1


def test_assign_worker_batches_persists_even_partition(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "worker-batches.db")
    for index in range(7):
        _insert_job(
            conn,
            company="Allowed Co",
            url=f"https://allowed.example/job-{index}",
        )
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.assign_worker_batches(3) == 7
    counts = conn.execute(
        "SELECT apply_worker, COUNT(*) FROM jobs GROUP BY apply_worker ORDER BY apply_worker"
    ).fetchall()
    assert [tuple(row) for row in counts] == [(0, 3), (1, 2), (2, 2)]
    assert launcher.assign_worker_batches(3) == 0
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/job-7")
    assert launcher.assign_worker_batches(3) == 1
    assert conn.execute(
        "SELECT apply_worker FROM jobs WHERE url = ?",
        ("https://allowed.example/job-7",),
    ).fetchone()[0] == 1


def test_assign_worker_batches_preserves_completed_jobs(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "batch-completed.db")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/done")
    _insert_job(conn, company="Allowed Co", url="https://allowed.example/pending")
    conn.execute(
        "UPDATE jobs SET apply_status = 'applied', apply_worker = 1 WHERE url = ?",
        ("https://allowed.example/done",),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.assign_worker_batches(10) == 1
    assert conn.execute(
        "SELECT apply_worker FROM jobs WHERE url = ?",
        ("https://allowed.example/done",),
    ).fetchone()[0] == 1


def test_rebalance_worker_batches_evens_remaining_work(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "rebalance.db")
    for index in range(5):
        _insert_job(conn, company="Allowed Co", url=f"https://allowed.example/job-{index}")
    conn.execute("UPDATE jobs SET apply_worker = 0")
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress', apply_worker = 1 "
        "WHERE url LIKE '%/job-0'"
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.rebalance_worker_batches(3) == 4
    assert conn.execute(
        "SELECT apply_worker FROM jobs WHERE url LIKE '%/job-0'"
    ).fetchone()[0] == 1
    leftover = conn.execute(
        """
        SELECT apply_worker, COUNT(*) FROM jobs
         WHERE apply_status IS NULL OR apply_status = 'in_progress'
         GROUP BY apply_worker ORDER BY apply_worker
        """
    ).fetchall()
    assert [tuple(row) for row in leftover] == [(0, 2), (1, 2), (2, 1)]


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


def test_worker_loop_reuses_one_chrome_across_jobs(monkeypatch):
    jobs = [
        {"url": "https://allowed.example/one", "title": "One", "site": "A"},
        {"url": "https://allowed.example/two", "title": "Two", "site": "B"},
    ]
    launches = []

    class FakeChrome:
        def poll(self):
            return None

    def acquire(**_):
        return jobs.pop(0) if jobs else None

    monkeypatch.setitem(launcher.config.DEFAULTS, "job_gap_seconds", 0)
    monkeypatch.setattr(launcher, "acquire_job", acquire)
    monkeypatch.setattr(
        launcher,
        "launch_chrome",
        lambda *args, **kwargs: launches.append(FakeChrome()) or launches[-1],
    )
    monkeypatch.setattr(launcher, "run_job", lambda *args, **kwargs: ("applied", 1))
    monkeypatch.setattr(launcher, "cleanup_browser_tabs", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    assert launcher.worker_loop(limit=2) == (2, 0)
    assert len(launches) == 1


def test_headless_worker_records_unsolved_captcha_instead_of_hanging(monkeypatch):
    job = {
        "url": "https://allowed.example/captcha",
        "title": "Captcha job",
        "site": "Allowed Co",
    }
    results = iter([job])
    marked = []
    monkeypatch.setattr(launcher, "acquire_job", lambda **_: next(results, None))
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "run_job", lambda *args, **kwargs: ("captcha", 1))
    monkeypatch.setattr(launcher, "_resolve_captcha", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr(launcher, "cleanup_browser_tabs", lambda *args: None)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args: None)
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    assert launcher.worker_loop(limit=1, headless=True) == (0, 1)
    assert marked[0][0][:3] == (
        job["url"], "failed", "captcha_unsolved",
    )
    assert marked[0][1]["permanent"] is False


def test_run_job_enforces_wall_clock_timeout(tmp_path, monkeypatch):
    class HangingProcess:
        pid = 12345
        returncode = None

        def communicate(self, input, timeout):
            assert input == "prompt"
            assert timeout == launcher.config.DEFAULTS["apply_timeout"]
            raise subprocess.TimeoutExpired("codex", timeout)

        def poll(self):
            return None

    killed = []
    popen_options = {}
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {"personal": {"email": "x@example.com", "password": "pw"}},
    )
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **_: "prompt")
    monkeypatch.setattr("applypilot.apply.capsolver.is_enabled", lambda: False)
    monkeypatch.setattr(launcher, "build_codex_command", lambda *args, **kwargs: ["codex"])
    def fake_popen(*args, **kwargs):
        popen_options.update(kwargs)
        return HangingProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "_kill_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    result, _ = launcher.run_job(
        {"url": "https://example.test", "title": "Intern", "site": "Example"},
        port=9322,
    )

    assert result == "failed:timeout"
    assert killed == [12345]
    assert popen_options["start_new_session"] is True


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
    assert 'model_reasoning_effort="medium"' in rendered
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--disable shell_tool" in rendered
    assert 'web_search="disabled"' in rendered
    assert "tools.view_image=false" in rendered
    assert "--secrets=" in rendered
    assert "mcp_servers.playwright" in rendered
    assert "mcp_servers.playwright.startup_timeout_sec=90" in rendered
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
    assert "do not solve, click, or reason through a captcha" in lower
    assert "output result:captcha immediately" in lower
    assert "never use tab/shift+tab/arrow keys" in lower or "never use tab" in lower
    assert "workday / ats click rules" in lower
    assert "dismiss cookie" in lower or "accept cookies" in lower
    assert "unknown" in lower and "manual_question" in lower
    assert "google" in lower and "oracle" in lower and "tiktok" in lower
    assert "google, coinbase, oracle" not in lower
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


def test_headless_worker_records_unsupported_hcaptcha_and_caches_host(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "hcaptcha-cache.db")
    job_url = "https://careers.example.com/jobs/1?icims=1"
    sibling_url = "https://careers.example.com/jobs/2?icims=1"
    other_url = "https://company.myworkdayjobs.com/en-US/job"
    _insert_job(conn, company="Vanity Icims", url=job_url)
    _insert_job(conn, company="Vanity Icims", url=sibling_url)
    _insert_job(conn, company="Workday Co", url=other_url)
    conn.commit()
    results = iter([{
        "url": job_url,
        "application_url": job_url,
        "title": "Captcha job",
        "site": "Vanity Icims",
    }])
    marked = []
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr("applypilot.apply.icims.HOST_POLICY_PATH", tmp_path / "hosts.json")
    monkeypatch.setattr(launcher, "acquire_job", lambda **_: next(results, None))
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "run_job", lambda *args, **kwargs: ("captcha", 1))
    monkeypatch.setattr(launcher, "_resolve_captcha", lambda *args, **kwargs: (False, "hcaptcha"))
    monkeypatch.setattr(launcher, "_try_icims_network_reroute", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "cleanup_browser_tabs", lambda *args: None)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args: None)
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    assert launcher.worker_loop(limit=1, headless=True) == (0, 1)
    assert marked[0][0][:3] == (job_url, "failed", "captcha_unsupported:hcaptcha")
    assert marked[0][1]["permanent"] is True
    sibling = conn.execute(
        "SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url = ?",
        (sibling_url,),
    ).fetchone()
    other = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = ?",
        (other_url,),
    ).fetchone()
    assert tuple(sibling) == ("failed", "captcha_unsupported:hcaptcha", 99)
    assert tuple(other) == (None, None)


def test_close_ineligible_jobs_parks_classic_and_vanity_icims(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "close-icims.db")
    _insert_job(conn, company="Icims Co", url="https://a.icims.com/jobs/1")
    _insert_job(conn, company="Vanity Icims", url="https://careers.example.com/jobs/2?icims=1")
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher, "_sync_applications_csv", lambda _: None)
    monkeypatch.setattr("applypilot.apply.icims.HOST_POLICY_PATH", tmp_path / "icims_hosts.json")

    launcher.close_ineligible_jobs()
    statuses = {
        row["url"]: (row["apply_status"], row["apply_error"], row["apply_attempts"])
        for row in conn.execute(
            "SELECT url, apply_status, apply_error, apply_attempts FROM jobs"
        )
    }
    assert statuses["https://a.icims.com/jobs/1"] == ("failed", "icims_unsupported", 99)
    assert statuses["https://careers.example.com/jobs/2?icims=1"] == (
        "failed", "icims_unsupported", 99,
    )


def test_manual_questions_are_not_retried_until_answered():
    assert launcher._is_permanent_failure("failed:manual_question") is True
    assert launcher._is_permanent_failure("failed:captcha_unsupported:hcaptcha") is True
    assert launcher._is_permanent_failure("failed:captcha_unsolved") is False
