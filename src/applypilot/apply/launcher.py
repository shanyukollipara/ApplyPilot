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
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

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
_worker_count = 1

PLAYWRIGHT_MCP_VERSION = "0.0.80"
EXCLUDED_COMPANIES = ("google", "coinbase")
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
        "-C", str(worker_dir),
        "-c", 'mcp_servers.playwright.command="npx"',
        "-c", f"mcp_servers.playwright.args={args_toml}",
        "-c", "mcp_servers.playwright.required=true",
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
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                  AND LOWER(TRIM(site)) NOT IN ('google', 'coinbase')
                  AND LOWER(url) NOT LIKE '%master%'
                  AND LOWER(url) NOT LIKE '%mba%'
                  AND LOWER(url) NOT LIKE '%phd%'
                  AND LOWER(url) NOT LIKE '%ph.d%'
                  AND LOWER(url) NOT LIKE '%doctoral%'
                  AND LOWER(url) NOT LIKE '%doctorate%'
                  AND LOWER(url) NOT LIKE '%graduate-student%'
                  AND LOWER(url) NOT LIKE '%graduate-intern%'
                  AND LOWER(url) NOT LIKE '%graduate-level%'
                  AND LOWER(url) NOT LIKE '%graduate-researcher%'
                  AND LOWER(url) NOT LIKE '%graduate-apprentice%'
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            params: list = [config.DEFAULTS["max_apply_attempts"]]
            worker_clause = ""
            if _worker_count > 1:
                worker_clause = "AND apply_worker = ?"
                params.append(worker_id)
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND LOWER(TRIM(site)) NOT IN ('google', 'coinbase')
                  AND LOWER(title) NOT LIKE '%master%'
                  AND LOWER(title) NOT LIKE '%mba%'
                  AND LOWER(title) NOT LIKE '%phd%'
                  AND LOWER(title) NOT LIKE '%ph.d%'
                  AND LOWER(title) NOT LIKE '%doctoral%'
                  AND LOWER(title) NOT LIKE '%doctorate%'
                  AND LOWER(title) NOT LIKE '%graduate student%'
                  AND LOWER(title) NOT LIKE '%graduate intern%'
                  AND LOWER(title) NOT LIKE '%graduate-level%'
                  AND LOWER(title) NOT LIKE '%graduate level%'
                  AND LOWER(title) NOT LIKE '%graduate researcher%'
                  AND LOWER(title) NOT LIKE '%graduate apprentice%'
                  AND LOWER(url) NOT LIKE '%master%'
                  AND LOWER(url) NOT LIKE '%mba%'
                  AND LOWER(url) NOT LIKE '%phd%'
                  AND LOWER(url) NOT LIKE '%ph.d%'
                  AND LOWER(url) NOT LIKE '%doctoral%'
                  AND LOWER(url) NOT LIKE '%doctorate%'
                  AND LOWER(url) NOT LIKE '%graduate-student%'
                  AND LOWER(url) NOT LIKE '%graduate-intern%'
                  AND LOWER(url) NOT LIKE '%graduate-level%'
                  AND LOWER(url) NOT LIKE '%graduate-researcher%'
                  AND LOWER(url) NOT LIKE '%graduate-apprentice%'
                  {worker_clause}
                  {site_clause}
                  {url_clauses}
                ORDER BY
                  CASE WHEN apply_status IS NULL THEN 0 ELSE 1 END,
                  CASE
                    WHEN COALESCE(application_url, url) LIKE '%myworkdayjobs.com%' THEN 0
                    WHEN COALESCE(application_url, url) LIKE '%workday%' THEN 1
                    ELSE 2
                  END,
                  url
                LIMIT 1
            """, params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        job = dict(row)
        if not reserve:
            conn.rollback()
            # Always use the master resume — enrich/score/tailor stages are removed.
            job["tailored_resume_path"] = str(config.RESUME_PATH)
            return job

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?,
                           apply_error = NULL
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()
        # Remove a previous failed export while this attempt is active.  The
        # CSV is an issues/results export, so it must not retain stale rows.
        _sync_applications_csv(conn)

        # Always use the master resume — enrich/score/tailor stages are removed.
        job["tailored_resume_path"] = str(config.RESUME_PATH)
        return job
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
        for index, row in enumerate(rows):
            conn.execute(
                "UPDATE jobs SET apply_worker = ? WHERE url = ? AND apply_worker IS NULL",
                (index % worker_count, row["url"]),
            )
        conn.commit()
        return len(rows)
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


def _wait_for_captcha_resolution(job: dict, worker_id: int) -> bool:
    """Pause a worker until the user marks its visible CAPTCHA as solved."""
    marker = config.APP_DIR / f"captcha-worker-{worker_id}.resolved"
    _send_notification(
        f"CAPTCHA needs you: {job.get('title', 'job')} at {job.get('site', 'employer')}. "
        "Solve it in the open Chrome window, then reply 'done' in Codex."
    )
    add_event(f"[W{worker_id}] CAPTCHA waiting for user; browser left open")
    update_state(worker_id, status="captcha", last_action="waiting for user")
    while not _stop_event.wait(timeout=2):
        if marker.exists():
            marker.unlink(missing_ok=True)
            add_event(f"[W{worker_id}] CAPTCHA marked solved; resuming")
            return True
    return False


def _resolve_captcha(job: dict, worker_id: int, port: int) -> bool:
    """Try CapSolver first, then fall back to the manual marker wait."""
    from applypilot.apply.capsolver import is_enabled, try_solve_on_cdp

    if is_enabled():
        update_state(worker_id, status="captcha", last_action="CapSolver solving")
        add_event(f"[W{worker_id}] CapSolver attempting to solve CAPTCHA")
        try:
            if try_solve_on_cdp(port):
                add_event(f"[W{worker_id}] CapSolver solved CAPTCHA")
                return True
        except Exception:
            logger.exception("CapSolver auto-solve failed")
        add_event(f"[W{worker_id}] CapSolver could not auto-solve; waiting for user")
    return _wait_for_captcha_resolution(job, worker_id)


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
        )
        with _codex_lock:
            _codex_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
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

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

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
    "not_eligible_location", "not_eligible_salary",
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
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by", "manual_question", "hcaptcha", "cookie_banner")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "gpt-5.6-luna", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Codex model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, reserve=not dry_run)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)

            captcha_rounds = 0
            while result == "captcha" and not dry_run:
                if captcha_rounds >= 2:
                    add_event(f"[W{worker_id}] CAPTCHA loop limit; marking failed")
                    result = "failed:captcha"
                    break
                if not _resolve_captcha(job, worker_id, port):
                    result = "skipped"
                    break
                captcha_rounds += 1
                resumed_result, resumed_ms = run_job(
                    job, port=port, worker_id=worker_id,
                    model=model, dry_run=False, resume_current_page=True,
                )
                result = resumed_result
                duration_ms += resumed_ms

            if dry_run:
                add_event(
                    f"[W{worker_id}] Dry run finished: {job['title'][:40]}"
                )
                jobs_done += 1
                if target_url:
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
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

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
        finally:
            if chrome_proc:
                cleanup_browser_tabs(port)
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

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
    global POLL_INTERVAL, _worker_count
    POLL_INTERVAL = poll_interval
    _worker_count = max(1, workers)
    _stop_event.clear()

    config.ensure_dirs()
    init_db()
    console = Console()
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
