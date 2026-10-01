"""Apply orchestration: acquire jobs, spawn Codex CLI sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Codex for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import csv
import json
import logging
import os
import platform
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.apply import chrome, dashboard, codex_prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, cleanup_browser_tabs, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Codex processes for skip (Ctrl+C) handling
_codex_procs: dict[int, subprocess.Popen] = {}
_codex_lock = threading.Lock()
_csv_lock = threading.Lock()
_drain_lock = threading.Lock()
_idle_retry_done = False
_worker_count = 1

_ICIMS_SKIP_SQL = """
                      AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims.com%'
                      AND LOWER(url) NOT LIKE '%icims.com%'
                      AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims=%'
                      AND LOWER(url) NOT LIKE '%icims=%'
"""

PLAYWRIGHT_MCP_VERSION = "0.0.80"
# Company name is not an eligibility criterion; only explicit graduate-only
# requirements are excluded by the queue predicates below.
EXCLUDED_COMPANIES: tuple[str, ...] = ()
_EXCLUDED_COMPANY_SQL = "''"
PLAYWRIGHT_ENABLED_TOOLS = [
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_click",
    "browser_fill_form",
    "browser_type",
    "browser_select_option",
    "browser_file_upload",
    "browser_press_key",
    "browser_wait_for",
    "browser_tabs",
    "browser_hover",
    "browser_drag",
    "browser_handle_dialog",
]

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int, secrets_path: Path | None = None) -> dict:
    """Build the single, pinned MCP definition used by Codex."""
    args = [
        "-y",
        f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
        f"--cdp-endpoint=http://localhost:{cdp_port}",
        f"--viewport-size={config.DEFAULTS['viewport']}",
        "--snapshot-mode=incremental",
        "--image-responses=omit",
        "--codegen=none",
    ]
    if secrets_path is not None:
        args.append(f"--secrets={secrets_path}")
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": args,
                "required": True,
                "enabled_tools": PLAYWRIGHT_ENABLED_TOOLS,
                "default_tools_approval_mode": "approve",
            },
        }
    }


def build_codex_command(model: str, port: int, worker_dir: Path,
                        final_output_path: Path,
                        secrets_path: Path | None = None) -> list[str]:
    """Build a locked-down, non-interactive Codex command."""
    args = _make_mcp_config(port, secrets_path)["mcpServers"]["playwright"]["args"]
    args_toml = json.dumps(args)
    tools_toml = json.dumps(PLAYWRIGHT_ENABLED_TOOLS)
    return [
        "codex", "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--disable", "shell_tool",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--model", model,
        "-c", 'model_reasoning_effort="medium"',
        "-C", str(worker_dir),
        "-c", 'mcp_servers.playwright.command="npx"',
        "-c", f"mcp_servers.playwright.args={args_toml}",
        "-c", "mcp_servers.playwright.required=true",
        "-c", "mcp_servers.playwright.startup_timeout_sec=90",
        "-c", f"mcp_servers.playwright.enabled_tools={tools_toml}",
        "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
        "-c", 'web_search="disabled"',
        "-c", "tools.view_image=false",
        "--json",
        "--output-last-message", str(final_output_path),
        "-",
    ]


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 0,
                worker_id: int = 0, reserve: bool = True) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific job URL instead of picking from queue.
        min_score: Unused (scoring removed); kept for CLI compatibility.
        worker_id: Worker claiming this job (for tracking).
        reserve: Mark the selected job in progress. Dry runs set this to False.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not target_url and _worker_count > 1:
                existing = conn.execute(
                    """
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE apply_worker = ? AND apply_status = 'in_progress'
                    LIMIT 1
                    """,
                    (worker_id,),
                ).fetchone()
                if existing:
                    conn.rollback()
                    job = dict(existing)
                    job["tailored_resume_path"] = str(config.RESUME_PATH)
                    return job

            if target_url:
                like = f"%{target_url.split('?')[0].rstrip('/')}%"
                row = conn.execute(f"""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                      AND COALESCE(apply_status, '') NOT IN ('applied', 'in_progress')
                      AND COALESCE(apply_error, '') NOT IN (
                            'application_outcome_unknown', 'no_result_line',
                            'captcha_submit_checkpoint'
                      )
                      AND LOWER(TRIM(site)) NOT IN ({_EXCLUDED_COMPANY_SQL})
                      AND LOWER(REPLACE(url, 'mastercard', '')) NOT LIKE '%master%'
                      AND LOWER(url) NOT LIKE '%mba%'
                      AND LOWER(url) NOT LIKE '%phd%'
                      AND LOWER(url) NOT LIKE '%ph.d%'
                      AND LOWER(url) NOT LIKE '%doctoral%'
                      AND LOWER(url) NOT LIKE '%doctorate%'
                      AND LOWER(url) NOT LIKE '%graduate-student%'
                      AND LOWER(REPLACE(LOWER(url), 'undergraduate', '')) NOT LIKE '%graduate-intern%'
                      AND LOWER(url) NOT LIKE '%graduate-level%'
                      AND LOWER(url) NOT LIKE '%graduate-researcher%'
                      AND LOWER(url) NOT LIKE '%graduate-apprentice%'
                    LIMIT 1
                """, (target_url, target_url, like, like)).fetchone()
            else:
                blocked_sites, blocked_patterns = _load_blocked()
                params: list = [config.DEFAULTS["max_apply_attempts"]]
                # Shared global queue: every worker takes the next
                # highest-priority job, same ordering as the solo worker.
                busy_site_clause = ""
                if _worker_count > 1:
                    # Robinhood is allowed in parallel so leftover intern roles
                    # can finish instead of waiting behind one Greenhouse tab.
                    busy_site_clause = """
                      AND (
                        LOWER(TRIM(COALESCE(site, ''))) = 'robinhood'
                        OR site IS NULL
                        OR TRIM(site) = ''
                        OR LOWER(TRIM(site)) NOT IN (
                          SELECT LOWER(TRIM(j2.site)) FROM jobs j2
                          WHERE j2.apply_status = 'in_progress'
                            AND j2.site IS NOT NULL
                            AND TRIM(j2.site) != ''
                            AND LOWER(TRIM(j2.site)) != 'robinhood'
                        )
                      )
                    """
                site_clause = ""
                if blocked_sites:
                    blocked_lower = sorted({str(site).lower() for site in blocked_sites})
                    placeholders = ",".join("?" * len(blocked_lower))
                    site_clause = f"AND LOWER(TRIM(site)) NOT IN ({placeholders})"
                    params.extend(blocked_lower)
                url_clauses = ""
                if blocked_patterns:
                    url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                    params.extend(blocked_patterns)
                from applypilot.apply import icims as icims_mod
                host_clauses = ""
                for host in icims_mod.blocked_hostnames():
                    host_clauses += (
                        " AND LOWER(COALESCE(application_url, url)) NOT LIKE ?"
                        " AND LOWER(url) NOT LIKE ?"
                    )
                    params.extend([f"%{host}%", f"%{host}%"])
                row = conn.execute(f"""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE (apply_status IS NULL OR apply_status = 'failed')
                      AND (apply_attempts IS NULL OR apply_attempts < ?)
                      AND LOWER(TRIM(site)) NOT IN ({_EXCLUDED_COMPANY_SQL})
                      AND LOWER(title) NOT LIKE '%master%'
                      AND LOWER(title) NOT LIKE '%mba%'
                      AND LOWER(title) NOT LIKE '%phd%'
                      AND LOWER(title) NOT LIKE '%ph.d%'
                      AND LOWER(title) NOT LIKE '%doctoral%'
                      AND LOWER(title) NOT LIKE '%doctorate%'
                      AND LOWER(title) NOT LIKE '%graduate student%'
                      AND LOWER(REPLACE(LOWER(title), 'undergraduate', '')) NOT LIKE '%graduate intern%'
                      AND LOWER(title) NOT LIKE '%graduate-level%'
                      AND LOWER(title) NOT LIKE '%graduate level%'
                      AND LOWER(title) NOT LIKE '%graduate researcher%'
                      AND LOWER(title) NOT LIKE '%graduate apprentice%'
                      AND LOWER(REPLACE(url, 'mastercard', '')) NOT LIKE '%master%'
                      AND LOWER(url) NOT LIKE '%mba%'
                      AND LOWER(url) NOT LIKE '%phd%'
                      AND LOWER(url) NOT LIKE '%ph.d%'
                      AND LOWER(url) NOT LIKE '%doctoral%'
                      AND LOWER(url) NOT LIKE '%doctorate%'
                      AND LOWER(url) NOT LIKE '%graduate-student%'
                      AND LOWER(REPLACE(LOWER(url), 'undergraduate', '')) NOT LIKE '%graduate-intern%'
                      AND LOWER(url) NOT LIKE '%graduate-level%'
                      AND LOWER(url) NOT LIKE '%graduate-researcher%'
                      AND LOWER(url) NOT LIKE '%graduate-apprentice%'
                      {_ICIMS_SKIP_SQL}
                      {busy_site_clause}
                      {site_clause}
                      {url_clauses}
                      {host_clauses}
                    ORDER BY
                      CASE WHEN LOWER(TRIM(site)) = 'robinhood' THEN 0 ELSE 1 END,
                      CASE
                        WHEN (
                            LOWER(COALESCE(application_url, url)) LIKE '%icims=%'
                         OR LOWER(url) LIKE '%icims=%'
                        ) AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims.com%' THEN 3
                        WHEN COALESCE(strategy, '') = 'simplify-summer-2027'
                         AND LOWER(TRIM(site)) != 'robinhood' THEN 2
                        ELSE 1
                      END,
                      CASE WHEN apply_status IS NULL THEN 0 ELSE 1 END,
                      CASE
                        WHEN COALESCE(application_url, url) LIKE '%myworkdayjobs.com%' THEN 0
                        WHEN COALESCE(application_url, url) LIKE '%workday%' THEN 1
                        WHEN COALESCE(application_url, url) LIKE '%greenhouse%' THEN 2
                        WHEN COALESCE(application_url, url) LIKE '%icims.com%' THEN 5
                        WHEN COALESCE(application_url, url) LIKE '%icims=%' THEN 6
                        WHEN COALESCE(application_url, url) LIKE '%lever.co%' THEN 4
                        ELSE 3
                      END,
                      url
                    LIMIT 1
                """, params).fetchone()

            if not row:
                conn.rollback()
                return None

            apply_url = row["application_url"] or row["url"]
            if config.is_manual_ats(apply_url):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                logger.info("Skipping manual ATS: %s", row["url"][:80])
                if target_url:
                    return None
                continue

            job = dict(row)
            if not reserve:
                conn.rollback()
                # Always use the master resume — enrich/score/tailor stages are removed.
                job["tailored_resume_path"] = str(config.RESUME_PATH)
                return job

            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                UPDATE jobs SET apply_status = 'in_progress',
                               apply_worker = ?,
                               agent_id = ?,
                               last_attempted_at = ?,
                               apply_error = NULL
                WHERE url = ?
                  AND (apply_status IS NULL OR apply_status = 'failed')
            """, (worker_id, f"worker-{worker_id}", now, row["url"]))
            if cursor.rowcount != 1:
                conn.rollback()
                if target_url:
                    return None
                continue
            conn.commit()
            # Remove a previous failed export while this attempt is active.  The
            # CSV is an issues/results export, so it must not retain stale rows.
            _sync_applications_csv(conn)

            # Always use the master resume — enrich/score/tailor stages are removed.
            job["tailored_resume_path"] = str(config.RESUME_PATH)
            return job
        except sqlite3.IntegrityError:
            conn.rollback()
            if _worker_count > 1:
                existing = conn.execute(
                    """
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE apply_worker = ? AND apply_status = 'in_progress'
                    LIMIT 1
                    """,
                    (worker_id,),
                ).fetchone()
                if existing:
                    job = dict(existing)
                    job["tailored_resume_path"] = str(config.RESUME_PATH)
                    return job
            if target_url:
                return None
            continue
        except Exception:
            conn.rollback()
            raise


def assign_worker_batches(worker_count: int) -> int:
    """Persistently divide eligible jobs across fixed worker IDs."""
    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assigned = _assign_unassigned_batches(conn, worker_count)
        conn.commit()
        return assigned
    except Exception:
        conn.rollback()
        raise


def _assign_unassigned_batches(conn, worker_count: int) -> int:
    """Assign unowned eligible rows to the currently smallest fixed batch."""
    counts = {worker: 0 for worker in range(worker_count)}
    for worker, count in conn.execute(
        """
        SELECT apply_worker, COUNT(*)
        FROM jobs
        WHERE apply_worker IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'in_progress'
               OR (apply_status = 'failed'
                   AND (apply_attempts IS NULL OR apply_attempts < ?)))
        GROUP BY apply_worker
        """,
        (config.DEFAULTS["max_apply_attempts"],),
    ):
        if worker in counts:
            counts[worker] = count
    rows = conn.execute(
            """
            SELECT url
            FROM jobs
            WHERE apply_worker IS NULL
              AND (apply_status IS NULL OR apply_status = 'failed')
              AND (apply_attempts IS NULL OR apply_attempts < ?)
            ORDER BY CASE WHEN apply_status IS NULL THEN 0 ELSE 1 END, url
            """,
            (config.DEFAULTS["max_apply_attempts"],),
        ).fetchall()
    for row in rows:
        worker = min(counts, key=lambda candidate: (counts[candidate], candidate))
        conn.execute(
            "UPDATE jobs SET apply_worker = ? WHERE url = ? AND apply_worker IS NULL",
            (worker, row["url"]),
        )
        counts[worker] += 1
    return len(rows)


def rebalance_worker_batches(worker_count: int) -> int:
    """Evenly split remaining eligible jobs across workers without stealing in-progress ones."""
    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        counts = {worker: 0 for worker in range(worker_count)}
        for worker, count in conn.execute(
            """
            SELECT apply_worker, COUNT(*)
            FROM jobs
            WHERE apply_status = 'in_progress' AND apply_worker IS NOT NULL
            GROUP BY apply_worker
            """
        ):
            if worker in counts:
                counts[worker] = count
        rows = conn.execute(
            """
            SELECT url
            FROM jobs
            WHERE apply_status IS NULL
               OR (apply_status = 'failed'
                   AND (apply_attempts IS NULL OR apply_attempts < ?))
            ORDER BY url
            """,
            (config.DEFAULTS["max_apply_attempts"],),
        ).fetchall()
        moved = 0
        for row in rows:
            worker = min(counts, key=lambda candidate: (counts[candidate], candidate))
            conn.execute(
                "UPDATE jobs SET apply_worker = ? WHERE url = ?",
                (worker, row["url"]),
            )
            counts[worker] += 1
            moved += 1
        conn.commit()
        return moved
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    from applypilot.hermes_learning import record_outcome

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()
    _sync_applications_csv(conn)
    job = conn.execute(
        "SELECT site, title, application_url, url FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if job:
        try:
            record_outcome(
                job=dict(job),
                status=status,
                error=error,
                duration_ms=duration_ms,
            )
        except Exception:
            # Learning is advisory and must never affect queue progress.
            pass


def _sync_applications_csv(conn=None) -> None:
    """Keep both application CSV exports current, including issue rows."""
    close_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute("""
            SELECT applied_at, site, title, application_url, url,
                   apply_status, apply_error
            FROM jobs
            WHERE apply_status IN ('applied', 'failed')
            ORDER BY COALESCE(applied_at, ''), site, title
        """).fetchall()
        headers = [
            "applied_at", "site", "title", "application_url", "url",
            "apply_status", "apply_error",
        ]
        destinations = [
            config.APP_DIR / "applications.csv",
            Path(__file__).resolve().parents[3] / "applications.csv",
        ]
        with _csv_lock:
            for destination in destinations:
                destination.parent.mkdir(parents=True, exist_ok=True)
                # A concurrent/stale SQLite connection can transiently return
                # no terminal rows while another worker still has the real
                # queue state. Never destroy a populated export in that case.
                if not rows and destination.exists():
                    with destination.open(newline="", encoding="utf-8") as existing:
                        if next(csv.DictReader(existing), None) is not None:
                            continue
                # Replace atomically so readers never observe a truncated export
                # while the live worker is syncing results.
                with tempfile.NamedTemporaryFile(
                    mode="w", newline="", encoding="utf-8",
                    dir=destination.parent, prefix=f".{destination.name}.tmp-",
                    delete=False,
                ) as handle:
                    writer = csv.writer(handle)
                    writer.writerow(headers)
                    writer.writerows(rows)
                    temporary = Path(handle.name)
                os.replace(temporary, destination)
    finally:
        if close_conn:
            conn.close()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def _send_notification(message: str) -> None:
    """Retained for compatibility; external notifications are disabled."""
    del message


def _wait_for_captcha_resolution(job: dict, worker_id: int,
                                 timeout: float | None = None) -> bool:
    """Wait for a visible CAPTCHA to clear, with a bounded unattended timeout."""
    marker = config.APP_DIR / f"captcha-worker-{worker_id}.resolved"
    if timeout is None:
        timeout = float(config.DEFAULTS.get("captcha_headed_wait_seconds") or 90)
    _send_notification(
        f"CAPTCHA needs you: {job.get('title', 'job')} at {job.get('site', 'employer')}. "
        "Solve it in the open Chrome window, then reply 'done' in Codex."
    )
    add_event(f"[W{worker_id}] CAPTCHA waiting up to {int(timeout)}s for extension/user")
    update_state(worker_id, status="captcha", last_action="waiting for captcha")
    deadline = time.time() + timeout
    while not _stop_event.wait(timeout=2):
        if marker.exists():
            marker.unlink(missing_ok=True)
            add_event(f"[W{worker_id}] CAPTCHA marked solved; resuming")
            return True
        if time.time() >= deadline:
            add_event(f"[W{worker_id}] CAPTCHA wait timed out after {int(timeout)}s")
            return False
    return False


def _hostname(url: str | None) -> str:
    """Return a lowercase hostname, or empty if the URL has none."""
    host = (urlparse(url or "").hostname or "").lower()
    if host in {"localhost", "127.0.0.1"}:
        return ""
    return host


def _negative_cache_captcha_host(source_url: str, error: str) -> int:
    """Permanently skip other pending jobs on the same hostname."""
    host = _hostname(source_url)
    if not host:
        return 0
    like = f"%{host}%"
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE jobs
           SET apply_status = 'failed',
               apply_error = ?,
               apply_attempts = 99,
               agent_id = NULL
         WHERE COALESCE(apply_status, '') NOT IN ('applied', 'in_progress')
           AND url != ?
           AND (url LIKE ? OR COALESCE(application_url, '') LIKE ?)
        """,
        (error, source_url, like, like),
    )
    conn.commit()
    if cursor.rowcount:
        _sync_applications_csv(conn)
    return cursor.rowcount


def _resolve_captcha(
    job: dict,
    worker_id: int,
    port: int,
    *,
    allow_manual_wait: bool = True,
):
    """Pause Luna, try the configured provider on this same Chrome session.

    hCaptcha uses NopeCHA. Other CAPTCHAs use CapSolver. Returns a
    CaptchaResolution that unpacks as (solved, unsupported).
    """
    from applypilot.apply.captcha import resolve_live_captcha

    return resolve_live_captcha(
        port,
        worker_id=worker_id,
        allow_manual_wait=allow_manual_wait,
        wait_fn=(lambda: _wait_for_captcha_resolution(job, worker_id)) if allow_manual_wait else None,
        add_event=add_event,
        update_state=update_state,
    )


def _try_icims_network_reroute(
    job: dict,
    worker_id: int,
    port: int,
    model: str,
) -> tuple[str, int] | None:
    """If hCaptcha blocked iCIMS, try an official Apply Network mirror once."""
    from applypilot.apply import icims as icims_mod

    if not icims_mod.is_icims_job(job):
        return None
    add_event(f"[W{worker_id}] iCIMS hCaptcha; searching Apply Network mirror")
    try:
        mirror = icims_mod.maybe_apply_network_url(job)
    except Exception:
        logger.exception("Apply Network search failed")
        mirror = None
    host = icims_mod.listing_hostname(job.get("application_url") or job.get("url") or "")
    if not mirror:
        icims_mod.record_host_policy(host, icims_mod.POLICY_BLOCK, "live_hcaptcha")
        return None
    add_event(f"[W{worker_id}] Apply Network mirror {mirror[:80]}")
    job["application_url"] = mirror
    job["apply_route"] = icims_mod.ROUTE_APPLY_NETWORK
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET application_url = ?, apply_route = ? WHERE url = ?",
        (mirror, icims_mod.ROUTE_APPLY_NETWORK, job["url"]),
    )
    conn.commit()
    icims_mod.record_host_policy(host, icims_mod.POLICY_BLOCK, "live_hcaptcha_rerouted")
    return run_job(job, port=port, worker_id=worker_id, model=model, dry_run=False)


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "gpt-5.6-luna", worker_id: int = 0) -> Path | None:
    """Generate a prompt file for manual Codex debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


def close_ineligible_jobs() -> int:
    """Mark leftover graduate and blocked rows so workers stop polling them."""
    conn = get_connection()
    blocked_sites, blocked_patterns = _load_blocked()
    closed = 0
    cursor = conn.execute("""
        UPDATE jobs
           SET apply_status = 'failed',
               apply_error = 'job_requires_grad_school',
               apply_attempts = 99,
               agent_id = NULL
         WHERE apply_status IS NULL
           AND (
                LOWER(title) LIKE '%master%'
             OR LOWER(title) LIKE '%mba%'
             OR LOWER(title) LIKE '%phd%'
             OR LOWER(title) LIKE '%ph.d%'
             OR LOWER(title) LIKE '%doctoral%'
             OR LOWER(title) LIKE '%doctorate%'
             OR LOWER(title) LIKE '%graduate student%'
             OR LOWER(REPLACE(LOWER(title), 'undergraduate', '')) LIKE '%graduate intern%'
             OR LOWER(title) LIKE '%graduate-level%'
             OR LOWER(title) LIKE '%graduate level%'
             OR LOWER(title) LIKE '%graduate researcher%'
             OR LOWER(title) LIKE '%graduate apprentice%'
             OR LOWER(REPLACE(url, 'mastercard', '')) LIKE '%master%'
             OR LOWER(url) LIKE '%mba%'
             OR LOWER(url) LIKE '%phd%'
             OR LOWER(url) LIKE '%ph.d%'
             OR LOWER(url) LIKE '%doctoral%'
             OR LOWER(url) LIKE '%doctorate%'
             OR LOWER(url) LIKE '%graduate-student%'
             OR LOWER(REPLACE(LOWER(url), 'undergraduate', '')) LIKE '%graduate-intern%'
             OR LOWER(url) LIKE '%graduate-level%'
             OR LOWER(url) LIKE '%graduate-researcher%'
             OR LOWER(url) LIKE '%graduate-apprentice%'
           )
    """)
    closed += cursor.rowcount
    from applypilot.apply import icims as icims_mod
    icims_mod.seed_policy_from_jobs(conn)
    synced = icims_mod.sync_jobs_to_host_policy(conn)
    if synced.get("unheld") or synced.get("parked"):
        logger.info(
            "iCIMS host policy parked=%d unheld=%d",
            synced.get("parked", 0), synced.get("unheld", 0),
        )
    cursor = conn.execute("""
        UPDATE jobs
           SET apply_status = 'failed',
               apply_error = 'icims_unsupported',
               apply_attempts = 99,
               agent_id = NULL
         WHERE COALESCE(apply_status, '') NOT IN ('applied', 'in_progress')
           AND (
                LOWER(COALESCE(application_url, url)) LIKE '%icims.com%'
             OR LOWER(url) LIKE '%icims.com%'
             OR LOWER(COALESCE(application_url, url)) LIKE '%icims=%'
             OR LOWER(url) LIKE '%icims=%'
           )
    """)
    closed += cursor.rowcount
    cursor = conn.execute(f"""
        UPDATE jobs
           SET apply_status = 'failed',
               apply_error = 'site_blocked',
               apply_attempts = 99,
               agent_id = NULL
         WHERE COALESCE(apply_status, '') NOT IN ('applied', 'in_progress')
           AND (
                LOWER(TRIM(site)) IN ({_EXCLUDED_COMPANY_SQL})
             OR LOWER(COALESCE(application_url, url)) LIKE '%lifeattiktok.com%'
             OR LOWER(url) LIKE '%lifeattiktok.com%'
             OR LOWER(COALESCE(application_url, url)) LIKE '%careers.tiktok.com%'
             OR LOWER(url) LIKE '%careers.tiktok.com%'
           )
    """)
    closed += cursor.rowcount
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        cursor = conn.execute(
            f"""
            UPDATE jobs
               SET apply_status = 'failed',
                   apply_error = 'site_blocked',
                   apply_attempts = 99,
                   agent_id = NULL
             WHERE apply_status IS NULL
               AND LOWER(TRIM(site)) IN ({placeholders})
            """,
            tuple(str(site).lower() for site in blocked_sites),
        )
        closed += cursor.rowcount
    for pattern in blocked_patterns:
        cursor = conn.execute(
            """
            UPDATE jobs
               SET apply_status = 'failed',
                   apply_error = 'site_blocked',
                   apply_attempts = 99,
                   agent_id = NULL
             WHERE apply_status IS NULL
               AND url LIKE ?
            """,
            (pattern,),
        )
        closed += cursor.rowcount
    conn.commit()
    _sync_applications_csv(conn)
    return closed


def reset_retryable_failures() -> int:
    """Put failed jobs back at the end of each worker's owned queue.

    Keeps apply_status='failed' and apply_worker so workers finish fresh
    pending jobs first, then retry their own failures without reshuffling.
    Graduate and blocked-site rows stay closed.
    """
    conn = get_connection()
    blocked_sites, blocked_patterns = _load_blocked()
    max_attempts = config.DEFAULTS["max_apply_attempts"]
    sql = f"""
        UPDATE jobs
           SET apply_attempts = 0,
               agent_id = NULL
         WHERE apply_status = 'failed'
           AND apply_attempts IS NOT NULL
           AND apply_attempts > 0
           AND apply_attempts < ?
           AND LOWER(TRIM(site)) NOT IN ({_EXCLUDED_COMPANY_SQL})
           AND LOWER(title) NOT LIKE '%master%'
           AND LOWER(title) NOT LIKE '%mba%'
           AND LOWER(title) NOT LIKE '%phd%'
           AND LOWER(title) NOT LIKE '%ph.d%'
           AND LOWER(title) NOT LIKE '%doctoral%'
           AND LOWER(title) NOT LIKE '%doctorate%'
           AND LOWER(title) NOT LIKE '%graduate student%'
           AND LOWER(REPLACE(LOWER(title), 'undergraduate', '')) NOT LIKE '%graduate intern%'
           AND LOWER(title) NOT LIKE '%graduate-level%'
           AND LOWER(title) NOT LIKE '%graduate level%'
           AND LOWER(title) NOT LIKE '%graduate researcher%'
           AND LOWER(title) NOT LIKE '%graduate apprentice%'
           AND LOWER(REPLACE(url, 'mastercard', '')) NOT LIKE '%master%'
           AND LOWER(url) NOT LIKE '%mba%'
           AND LOWER(url) NOT LIKE '%phd%'
           AND LOWER(url) NOT LIKE '%ph.d%'
           AND LOWER(url) NOT LIKE '%doctoral%'
           AND LOWER(url) NOT LIKE '%doctorate%'
           AND LOWER(url) NOT LIKE '%graduate-student%'
           AND LOWER(REPLACE(LOWER(url), 'undergraduate', '')) NOT LIKE '%graduate-intern%'
           AND LOWER(url) NOT LIKE '%graduate-level%'
           AND LOWER(url) NOT LIKE '%graduate-researcher%'
           AND LOWER(url) NOT LIKE '%graduate-apprentice%'
           AND COALESCE(apply_error, '') NOT IN (
               'job_requires_grad_school', 'job_requires_PhD',
               'job_requires_masters', 'job_requires_MBA',
               'job_requires_doctorate', 'site_blocked',
               'icims_unsupported', 'icims_lab_hold', 'icims_blocked_hcaptcha',
               'expired', 'login_issue', 'already_applied', 'captcha',
               'manual_question', 'account_required', 'sso_required'
           )
           AND COALESCE(apply_error, '') NOT LIKE 'captcha%'
           AND COALESCE(apply_error, '') NOT LIKE 'hcaptcha%'
           AND COALESCE(apply_error, '') NOT LIKE 'possible_spam%'
           AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims.com%'
           AND LOWER(url) NOT LIKE '%icims.com%'
           AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims=%'
           AND LOWER(url) NOT LIKE '%icims=%'
    """
    params: list = [max_attempts]
    if blocked_sites:
        blocked_lower = sorted({str(site).lower() for site in blocked_sites})
        placeholders = ",".join("?" * len(blocked_lower))
        sql += f" AND LOWER(TRIM(site)) NOT IN ({placeholders})"
        params.extend(blocked_lower)
    for pattern in blocked_patterns:
        sql += " AND url NOT LIKE ?"
        params.append(pattern)
    cursor = conn.execute(sql, params)
    conn.commit()
    _sync_applications_csv(conn)
    return cursor.rowcount


def requeue_unfinished_non_icims() -> int:
    """Reset leftover failed non-iCIMS jobs for one more full apply pass.

    Leaves iCIMS, excluded companies, graduate roles, already-applied rows,
    site blocks, and arbitration/SSN holds parked.
    """
    conn = get_connection()
    blocked_sites, blocked_patterns = _load_blocked()
    sql = f"""
        UPDATE jobs
           SET apply_attempts = 0,
               apply_worker = NULL,
               agent_id = NULL
         WHERE apply_status = 'failed'
           AND LOWER(TRIM(site)) NOT IN ({_EXCLUDED_COMPANY_SQL})
           AND LOWER(title) NOT LIKE '%master%'
           AND LOWER(title) NOT LIKE '%mba%'
           AND LOWER(title) NOT LIKE '%phd%'
           AND LOWER(title) NOT LIKE '%ph.d%'
           AND LOWER(title) NOT LIKE '%doctoral%'
           AND LOWER(title) NOT LIKE '%doctorate%'
           {_ICIMS_SKIP_SQL}
           AND COALESCE(apply_error, '') NOT IN (
               'job_requires_grad_school', 'job_requires_PhD',
               'job_requires_masters', 'job_requires_MBA',
               'job_requires_doctorate', 'site_blocked',
               'icims_unsupported', 'icims_lab_hold', 'icims_blocked_hcaptcha',
               'already_applied', 'uk_work_authorization_required',
               'role_requires_masters_degree'
           )
           AND COALESCE(apply_error, '') NOT LIKE 'job_requires_%'
           AND COALESCE(apply_error, '') NOT LIKE '%arbitration%'
           AND COALESCE(apply_error, '') NOT LIKE '%Arbitrate%'
           AND COALESCE(apply_error, '') NOT LIKE '%SSN%'
           AND COALESCE(apply_error, '') NOT LIKE '%Social Security%'
           AND COALESCE(apply_error, '') NOT LIKE '%Social Insurance%'
           AND COALESCE(apply_error, '') NOT LIKE '%WOTC%'
           AND COALESCE(apply_error, '') NOT LIKE 'maximum_application%'
    """
    params: list = []
    if blocked_sites:
        blocked_lower = sorted({str(site).lower() for site in blocked_sites})
        placeholders = ",".join("?" * len(blocked_lower))
        sql += f" AND LOWER(TRIM(site)) NOT IN ({placeholders})"
        params.extend(blocked_lower)
    for pattern in blocked_patterns:
        sql += " AND url NOT LIKE ?"
        params.append(pattern)
    cursor = conn.execute(sql, params)
    conn.commit()
    _sync_applications_csv(conn)
    return cursor.rowcount


def in_progress_count() -> int:
    conn = get_connection()
    return int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'in_progress'"
    ).fetchone()[0])


def eligible_apply_count() -> int:
    """Pending or retryable-failed jobs the live fleet may still claim."""
    conn = get_connection()
    blocked_sites, blocked_patterns = _load_blocked()
    params: list = [config.DEFAULTS["max_apply_attempts"]]
    site_clause = ""
    if blocked_sites:
        blocked_lower = sorted({str(site).lower() for site in blocked_sites})
        placeholders = ",".join("?" * len(blocked_lower))
        site_clause = f"AND LOWER(TRIM(site)) NOT IN ({placeholders})"
        params.extend(blocked_lower)
    url_clauses = ""
    if blocked_patterns:
        url_clauses = " ".join(
            "AND LOWER(url) NOT LIKE ? AND LOWER(COALESCE(application_url, url)) NOT LIKE ?"
            for _ in blocked_patterns
        )
        for pattern in blocked_patterns:
            params.extend([pattern, pattern])
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM jobs
         WHERE (apply_status IS NULL OR apply_status = 'failed')
           AND (apply_attempts IS NULL OR apply_attempts < ?)
           AND LOWER(TRIM(site)) NOT IN ({_EXCLUDED_COMPANY_SQL})
           AND LOWER(title) NOT LIKE '%master%'
           AND LOWER(title) NOT LIKE '%mba%'
           AND LOWER(title) NOT LIKE '%phd%'
           AND LOWER(title) NOT LIKE '%ph.d%'
           AND LOWER(title) NOT LIKE '%doctoral%'
           AND LOWER(title) NOT LIKE '%doctorate%'
           {_ICIMS_SKIP_SQL}
           {site_clause}
           {url_clauses}
        """,
        params,
    ).fetchone()
    return int(row[0] if row else 0)


def fleet_has_apply_work() -> bool:
    return in_progress_count() > 0 or eligible_apply_count() > 0


def drain_idle_queue() -> str:
    """Decide what an idle continuous worker should do.

    Returns:
        work: claimable rows remain or other workers are still in progress
        retrying: requeued retryable failures; acquire again immediately
        stop: nothing left after one reload pass
    """
    global _idle_retry_done
    with _drain_lock:
        if in_progress_count() > 0 or eligible_apply_count() > 0:
            return "work"
        if not _idle_retry_done:
            _idle_retry_done = True
            requeued = reset_retryable_failures()
            if requeued:
                logger.info("Requeued %d retryable failures before drain", requeued)
                return "retrying"
        return "stop"


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "gpt-5.6-luna", dry_run: bool = False,
            resume_current_page: bool = False) -> tuple[str, int]:
    """Spawn a Codex CLI session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    worker_dir = reset_worker_dir(worker_id)

    profile = config.load_profile()
    secrets_path = worker_dir / ".secrets.env"
    secrets_path.write_text(
        "APPLYPILOT_EMAIL=" + profile["personal"]["email"] + "\n"
        "APPLYPILOT_PASSWORD=" + profile["personal"].get("password", "") + "\n",
        encoding="utf-8",
    )
    secrets_path.chmod(0o600)

    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    from applypilot.apply.capsolver import is_enabled as capsolver_enabled

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        upload_dir=worker_dir,
        resume_current_page=resume_current_page,
        capsolver_enabled=capsolver_enabled(),
    )

    env = os.environ.copy()
    final_output_path = config.LOG_DIR / f"codex-final-{worker_id}.txt"
    final_output_path.unlink(missing_ok=True)
    cmd = build_codex_command(model, port, worker_dir, final_output_path, secrets_path)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
            start_new_session=platform.system() != "Windows",
        )
        with _codex_lock:
            _codex_procs[worker_id] = proc

        raw_output, _ = proc.communicate(
            input=agent_prompt,
            timeout=config.DEFAULTS["apply_timeout"],
        )
        returncode = proc.returncode
        proc = None
        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in raw_output.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type", "event")
                    lf.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    if msg_type == "item.completed":
                        item = msg.get("item", {})
                        if item.get("type") == "agent_message":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") in {"mcp_tool_call", "command_execution"}:
                            ws = get_state(worker_id)
                            cur_actions = ws.actions if ws else 0
                            desc = item.get("name") or item.get("command") or msg_type
                            update_state(worker_id, actions=cur_actions + 1,
                                         last_action=str(desc)[:35])
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        if final_output_path.exists():
            text_parts.append(final_output_path.read_text(encoding="utf-8"))
        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"codex_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        if "required MCP servers failed to initialize" in raw_output:
            add_event(f"[W{worker_id}] AGENT STARTUP FAILED ({elapsed}s)")
            update_state(worker_id, status="failed", last_action="MCP startup failed")
            return "failed:agent_startup_failed", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        with _codex_lock:
            _codex_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary", "no_base_pay",
    "job_requires_PhD", "job_requires_masters", "job_requires_MBA",
    "job_requires_doctorate", "job_requires_grad_school",
    "manual_question",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "workday_unavailable", "workday_maintenance", "site_unavailable",
    "captcha_token_not_accepted",
    "hcaptcha_blocked",
    "icims_lab_hold",
    "icims_unsupported",
    "icims_blocked_hcaptcha",
    "captcha_provider_not_loaded",
    "captcha_provider_unsupported",
    "captcha_provider_timeout",
    "captcha_completed_but_login_rejected",
    "application_outcome_unknown",
}

PERMANENT_PREFIXES: tuple[str, ...] = (
    "site_blocked", "cloudflare", "blocked_by", "manual_question",
    "hcaptcha", "cookie_banner", "captcha_unsupported", "icims_lab",
    "captcha_provider", "captcha_completed", "application_outcome",
)


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


def _should_recycle_chrome(result: str, worker_id: int = 0) -> bool:
    """Restart a worker browser after session-level failures.

    Campaign workers 50-89 reuse the same minimized Chrome. Killing it after
    every blocked click spawned a new window on screen.
    """
    reason = result.split(":", 1)[-1] if ":" in result else result
    if 50 <= worker_id <= 89:
        return reason in {
            "browser_unavailable",
            "browser_transport_closed",
            "browser_server_unavailable",
            "browser_session_unavailable",
            "agent_startup_failed",
        }
    if reason.startswith("captcha"):
        return True
    return reason in {
        "timeout",
        "agent_startup_failed",
        "browser_unavailable",
        "browser_transport_closed",
        "browser_server_unavailable",
        "browser_session_unavailable",
        "browser_interaction_blocked",
        "403_forbidden",
        "site_access_403",
        "job_page_403",
        "employer_site_403",
    }


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "gpt-5.6-luna", dry_run: bool = False,
                url_queue=None) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Codex model name.
        dry_run: Don't click Submit.
        url_queue: Optional queue of URLs. Isolated iCIMS workers use this
            instead of the live shared queue.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    chrome_proc = None
    if worker_id and worker_id < _worker_count and _stop_event.wait(timeout=worker_id * 3):
        return applied, failed

    try:
        while not _stop_event.is_set():
            if not continuous and jobs_done >= limit:
                break

            update_state(worker_id, status="idle", job_title="", company="",
                         last_action="waiting for job", actions=0)

            try:
                next_url = target_url
                if url_queue is not None:
                    try:
                        next_url = url_queue.get_nowait()
                    except Exception as exc:
                        from queue import Empty
                        if not isinstance(exc, Empty):
                            raise
                        add_event(f"[W{worker_id}] Isolated queue empty")
                        update_state(worker_id, status="done", last_action="queue empty")
                        break
                job = acquire_job(target_url=next_url, min_score=min_score,
                                  worker_id=worker_id, reserve=not dry_run)
            except Exception:
                logger.exception("Worker %d failed to acquire a job", worker_id)
                if _stop_event.wait(timeout=POLL_INTERVAL):
                    break
                continue
            if not job:
                if url_queue is not None:
                    continue
                if not continuous:
                    add_event(f"[W{worker_id}] Queue empty")
                    update_state(worker_id, status="done", last_action="queue empty")
                    break
                action = drain_idle_queue()
                if action == "retrying":
                    add_event(f"[W{worker_id}] Reloaded retryable failures")
                    continue
                if action == "stop":
                    add_event(f"[W{worker_id}] Queue drained; stopping workers")
                    update_state(worker_id, status="done", last_action="queue drained")
                    _stop_event.set()
                    break
                empty_polls += 1
                update_state(worker_id, status="idle",
                             last_action=f"polling ({empty_polls})")
                if empty_polls == 1:
                    add_event(f"[W{worker_id}] Waiting on in-progress jobs...")
                if _stop_event.wait(timeout=POLL_INTERVAL):
                    break
                continue

            empty_polls = 0
            recycle_chrome = False
            is_campaign = 50 <= worker_id <= 89
            try:
                if chrome_proc is None or chrome_proc.poll() is not None:
                    add_event(f"[W{worker_id}] Launching Chrome...")
                    chrome_proc = launch_chrome(worker_id, port=port, headless=headless)
                else:
                    cleanup_browser_tabs(
                        port, keep_pages=1, park=is_campaign,
                    )

                result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                                model=model, dry_run=dry_run)

                from applypilot.apply.captcha import (
                    CHECKPOINT_ATTEMPT_COMPLETED,
                    CHECKPOINT_NOT_LOADED,
                    CHECKPOINT_TIMEOUT,
                    CHECKPOINT_UNKNOWN,
                    infer_funnel_checkpoint,
                    provider_config_id,
                )
                from applypilot.apply.experiment import persist_trial, trial_from_job
                from applypilot.apply import icims as icims_mod

                trial = trial_from_job(job, worker_id=worker_id)
                trial_started = time.time()
                provider_solved = False
                captcha_rounds = 0
                allow_manual = (not headless) and worker_id < icims_mod.LAB_WORKER_ID
                while result == "captcha" and not dry_run:
                    if captcha_rounds >= 3:
                        add_event(f"[W{worker_id}] CAPTCHA loop limit; marking failed")
                        result = "failed:captcha"
                        break
                    resolution = _resolve_captcha(
                        job, worker_id, port, allow_manual_wait=allow_manual
                    )
                    if not hasattr(resolution, "checkpoint"):
                        solved, unsupported = resolution
                        from applypilot.apply.captcha import CaptchaResolution as _CR
                        resolution = _CR(
                            solved=bool(solved),
                            unsupported=unsupported,
                            provider="none" if unsupported else "capsolver",
                            checkpoint=(
                                CHECKPOINT_TIMEOUT if not solved and not unsupported
                                else (f"captcha_unsupported:{unsupported}" if unsupported else CHECKPOINT_ATTEMPT_COMPLETED)
                            ),
                            captcha_type=unsupported,
                        )
                        if not solved and not unsupported:
                            resolution.checkpoint = "captcha_unsolved"
                    trial.captcha_provider = resolution.provider
                    trial.captcha_type = resolution.captcha_type or trial.captcha_type
                    trial.provider_result = resolution.checkpoint
                    trial.time_to_checkpoint = resolution.elapsed_ms
                    trial.captcha_checkpoint = resolution.checkpoint
                    if resolution.unsupported and not resolution.solved:
                        rerouted = _try_icims_network_reroute(
                            job, worker_id, port, model,
                        )
                        if rerouted:
                            result, extra_ms = rerouted
                            duration_ms += extra_ms
                            trial.route_used = icims_mod.ROUTE_APPLY_NETWORK
                            break
                        result = f"failed:captcha_unsupported:{resolution.unsupported}"
                        break
                    if resolution.checkpoint == CHECKPOINT_TIMEOUT:
                        result = f"failed:{CHECKPOINT_TIMEOUT}"
                        break
                    if resolution.checkpoint == CHECKPOINT_NOT_LOADED and not resolution.solved:
                        result = f"failed:{CHECKPOINT_NOT_LOADED}"
                        break
                    if not resolution.solved:
                        result = f"failed:{resolution.checkpoint or 'captcha_unsolved'}"
                        break
                    provider_solved = True
                    captcha_rounds += 1
                    resumed_result, resumed_ms = run_job(
                        job, port=port, worker_id=worker_id,
                        model=model, dry_run=False, resume_current_page=True,
                    )
                    result = resumed_result
                    duration_ms += resumed_ms
                    signals = icims_mod.inspect_page_signals(port)
                    if signals:
                        trial.final_host = icims_mod.listing_hostname(
                            str(signals.get("url") or "")
                        )
                    trial.captcha_checkpoint = infer_funnel_checkpoint(
                        result, signals, provider_solved=provider_solved,
                    )
                    if trial.captcha_checkpoint == "captcha_login_accepted":
                        trial.login_result = "accepted"
                    elif trial.captcha_checkpoint == "captcha_completed_but_login_rejected":
                        trial.login_result = "rejected"
                    if trial.captcha_checkpoint == "captcha_profile_checkpoint":
                        trial.profile_result = "reached"
                    if trial.captcha_checkpoint == "captcha_submit_checkpoint":
                        trial.submit_result = "checkpoint"
                    if result == "applied":
                        trial.confirmation_detected = True
                        trial.submit_result = "confirmed"

                if worker_id >= icims_mod.LAB_WORKER_ID or icims_mod.is_icims_job(job):
                    if result == "applied":
                        trial.confirmation_detected = True
                        trial.captcha_checkpoint = "application_confirmed"
                        trial.submit_result = "confirmed"
                    elif result == "login_issue" and provider_solved:
                        result = "failed:captcha_completed_but_login_rejected"
                        trial.login_result = "rejected"
                        trial.captcha_checkpoint = "captcha_completed_but_login_rejected"
                    elif result in {"failed:no_result_line", "failed:timeout"} and provider_solved:
                        result = f"failed:{CHECKPOINT_UNKNOWN}"
                        trial.captcha_checkpoint = CHECKPOINT_UNKNOWN
                    trial.apply_status = "applied" if result == "applied" else "failed"
                    trial.total_runtime = int((time.time() - trial_started) * 1000) + duration_ms
                    trial.finished_at = datetime.now(timezone.utc).isoformat()
                    if not trial.final_host:
                        trial.final_host = icims_mod.listing_hostname(
                            job.get("application_url") or job.get("url") or ""
                        )
                    try:
                        persist_trial(get_connection(), trial)
                    except Exception:
                        logger.exception("Could not persist apply trial")

                if dry_run:
                    add_event(
                        f"[W{worker_id}] Dry run finished: {job['title'][:40]}"
                    )
                    jobs_done += 1
                    if target_url and url_queue is None:
                        break
                    continue

                if result == "skipped":
                    release_lock(job["url"])
                    add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                    continue
                elif result == "applied":
                    mark_result(job["url"], "applied", duration_ms=duration_ms)
                    applied += 1
                    update_state(worker_id, jobs_applied=applied,
                                 jobs_done=applied + failed)
                else:
                    reason = result.split(":", 1)[-1] if ":" in result else result
                    mark_result(job["url"], "failed", reason,
                                permanent=_is_permanent_failure(result),
                                duration_ms=duration_ms)
                    if worker_id < icims_mod.LAB_WORKER_ID and (
                        reason.startswith("captcha_unsupported")
                        or reason in {
                            "captcha_provider_timeout",
                            "captcha_provider_unsupported",
                            "captcha_provider_not_loaded",
                        }
                    ):
                        from applypilot.apply import icims as icims_mod
                        from applypilot.apply.captcha import provider_config_id
                        icims_mod.record_host_policy(
                            icims_mod.listing_hostname(
                                job.get("application_url") or job["url"]
                            ),
                            icims_mod.POLICY_BLOCK,
                            reason,
                            provider_config=provider_config_id(),
                        )
                        if reason.startswith("captcha_unsupported"):
                            cached = _negative_cache_captcha_host(
                                job.get("application_url") or job["url"],
                                reason,
                            )
                            if cached:
                                add_event(
                                    f"[W{worker_id}] Negative-cached {cached} "
                                    f"same-host jobs ({reason})"
                                )
                    failed += 1
                    update_state(worker_id, jobs_failed=failed,
                                 jobs_done=applied + failed)
                    recycle_chrome = _should_recycle_chrome(result, worker_id=worker_id)

            except KeyboardInterrupt:
                release_lock(job["url"])
                if _stop_event.is_set():
                    break
                add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
                continue
            except Exception as e:
                logger.exception("Worker %d launcher error", worker_id)
                add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
                release_lock(job["url"])
                failed += 1
                update_state(worker_id, jobs_failed=failed)
                recycle_chrome = worker_id < 50 or worker_id > 89
            finally:
                if recycle_chrome and chrome_proc:
                    cleanup_browser_tabs(port, keep_pages=1, park=is_campaign)
                    cleanup_worker(worker_id, chrome_proc)
                    chrome_proc = None
                elif chrome_proc:
                    cleanup_browser_tabs(port, keep_pages=1, park=is_campaign)

            jobs_done += 1
            if target_url and url_queue is None:
                break
            if not continuous and jobs_done >= limit:
                break
            gap = float(config.DEFAULTS.get("job_gap_seconds") or 0)
            if gap and _stop_event.wait(timeout=gap):
                break
    finally:
        if chrome_proc:
            cleanup_browser_tabs(port)
            cleanup_worker(worker_id, chrome_proc)

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "gpt-5.6-luna",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Codex model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL, _worker_count, _idle_retry_done
    POLL_INTERVAL = poll_interval
    _worker_count = max(1, workers)
    _idle_retry_done = False
    _stop_event.clear()

    config.ensure_dirs()
    init_db()
    console = Console()
    closed = close_ineligible_jobs()
    if closed:
        logger.info("Closed %d leftover ineligible jobs", closed)
    assigned = assign_worker_batches(_worker_count)
    if assigned:
        logger.info("Assigned %d unassigned jobs across %d fixed worker batches",
                    assigned, _worker_count)

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Codex processes to skip current jobs
            with _codex_lock:
                for wid, cproc in list(_codex_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _codex_lock:
                for wid, cproc in list(_codex_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
