"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(0, "--min-score", help="Unused (scoring removed)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery scrapers."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help="Unused (tailoring/cover letter stages removed).",
    ),
) -> None:
    """Run pipeline stages (discover jobs)."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(0, "--min-score", help="Unused (scoring removed)."),
    model: str = typer.Option("gpt-5.6-luna", "--model", "-m", help="Codex model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Codex needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Codex CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Jobs exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No jobs ready.[/red]\n"
                "Run [bold]applypilot run discover[/bold] first."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually (the normal apply command configures Playwright automatically):[/bold]")
        console.print(
            f"  codex exec --ignore-user-config --ignore-rules --ephemeral "
            f"--sandbox read-only --model {model} - < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command("sync-simplify")
def sync_simplify() -> None:
    """Import unseen Summer 2027 internships from the Simplify job board."""
    _bootstrap()
    from applypilot.apply.launcher import assign_worker_batches
    from applypilot.discovery.simplify import sync_listings

    result = sync_listings()
    assigned = assign_worker_batches(10)
    console.print(
        "[green]Simplify sync[/green] "
        f"received={result['received']} inserted={result['inserted']} "
        f"skipped={result['skipped']} excluded={result['excluded']} "
        f"assigned={assigned}"
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Codex CLI
    codex_bin = shutil.which("codex")
    if codex_bin:
        results.append(("Codex CLI", ok_mark, codex_bin))
    else:
        results.append(("Codex CLI", fail_mark,
                        "Install Codex CLI (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional CAPTCHA solving)
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if capsolver_key:
        try:
            from applypilot.apply.capsolver import get_balance
            balance = get_balance()
            results.append(("CapSolver API key", ok_mark, f"balance ${balance:.2f}"))
        except Exception as exc:
            results.append(("CapSolver API key", warn_mark, f"set, but unverified ({exc})"))
    else:
        results.append(("CapSolver API key", warn_mark,
                        "Optional — set CAPSOLVER_API_KEY in ~/.applypilot/.env for reCAPTCHA/Turnstile"))

    nopecha_key = os.environ.get("NOPECHA_API_KEY", "").strip()
    if nopecha_key:
        try:
            from applypilot.apply.nopecha import get_status
            status = get_status()
            credit = status.get("credit")
            plan = status.get("plan") or "active"
            results.append(("NopeCHA API key", ok_mark, f"{plan}, {credit} credits"))
        except Exception as exc:
            results.append(("NopeCHA API key", warn_mark, f"set, but unverified ({exc})"))
    else:
        results.append(("NopeCHA API key", warn_mark,
                        "Optional — set NOPECHA_API_KEY in ~/.applypilot/.env for hCaptcha"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Codex CLI + Chrome + Node.js)[/dim]")

    console.print()


@app.command("icims-lab")
def icims_lab(
    url: Optional[str] = typer.Option(None, "--url", help="Plan/probe/apply one job URL."),
    limit: int = typer.Option(8, "--limit", "-l", help="How many held iCIMS jobs to plan."),
    search_boards: bool = typer.Option(
        False, "--search-boards",
        help="Also search Indeed/LinkedIn/ZipRecruiter (slow, network).",
    ),
    probe: bool = typer.Option(
        False, "--probe",
        help="Open isolated Chrome on port 9362 and read login/captcha signals.",
    ),
    apply: bool = typer.Option(
        False, "--apply",
        help="Run one isolated Luna apply on worker 40. Default is dry-run.",
    ),
    submit: bool = typer.Option(
        False, "--submit",
        help="With --apply, actually submit instead of dry-run.",
    ),
    headed: bool = typer.Option(
        False, "--headed",
        help="Show the lab Chrome window (port 9362). Live fleet stays headless.",
    ),
    model: str = typer.Option("gpt-5.6-luna", "--model", "-m"),
    csv_path: Optional[str] = typer.Option(
        None, "--csv", help="Apply only jobs from this CSV. Isolated from the live fleet.",
    ),
    workers: int = typer.Option(
        1, "--workers", "-w", help="Initial isolated iCIMS workers (IDs 40+). Default 1.",
    ),
    max_workers: int = typer.Option(
        4, "--max-workers", help="Cap when scaling up as live fleet Chromes free.",
    ),
    watch_fleet: bool = typer.Option(
        True, "--watch-fleet/--no-watch-fleet",
        help="Add isolated workers only as live fleet ports 9322-9337 free.",
    ),
) -> None:
    """Plan and test iCIMS routes without touching the live Workday fleet."""
    _bootstrap()
    from applypilot.apply import icims
    from applypilot.database import get_connection, init_db

    init_db()
    conn = get_connection()

    if csv_path:
        from applypilot.apply import nopecha as nopecha_mod
        if not nopecha_mod.is_enabled():
            console.print("[red]NOPECHA_API_KEY is not set. hCaptcha cannot be handed off.[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"Isolated iCIMS CSV run workers={workers} max={max_workers} "
            f"submit={submit} headed={headed} (live fleet untouched)"
        )
        applied, failed = icims.run_csv_queue(
            csv_path,
            workers=workers,
            max_workers=max_workers,
            watch_fleet=watch_fleet,
            submit=submit,
            headed=headed,
            model=model,
        )
        console.print(f"CSV result applied={applied} failed={failed}")
        raise typer.Exit(code=0 if failed == 0 or applied else 1)

    jobs = icims.lab_jobs(conn, url=url, limit=1 if (url or apply or probe) else limit)
    if not jobs:
        console.print("[yellow]No iCIMS jobs found to plan.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title="iCIMS lab routes (live fleet unchanged)", show_header=True, header_style="bold cyan")
    table.add_column("Company", max_width=22)
    table.add_column("Title", max_width=28)
    table.add_column("Class")
    table.add_column("Route")
    table.add_column("Conf", justify="right")
    table.add_column("Apply URL", max_width=42)
    table.add_column("Why", max_width=28)

    plans: list[tuple[dict, object]] = []
    for job in jobs:
        plan = icims.plan_job(job, conn=conn, search=search_boards, captcha_provider="nopecha")
        icims.persist_plan(conn, job["url"], plan)
        plans.append((job, plan))
        table.add_row(
            (job.get("site") or "")[:22],
            (job.get("title") or "")[:28],
            plan.classification,
            plan.route,
            f"{plan.confidence:.2f}",
            (plan.apply_url or "")[:42],
            ", ".join(plan.reasons)[:28],
        )
    console.print(table)

    if probe:
        target = url or icims.job_apply_url(jobs[0])
        console.print(f"\nProbing [bold]{target}[/bold] on port {icims.LAB_CDP_PORT}...")
        signals = icims.lab_probe(target, headless=not headed)
        console.print(signals or "[red]probe failed[/red]")
        job = jobs[0]
        plan = icims.plan_job(job, conn=conn, signals=signals, search=search_boards)
        icims.persist_plan(conn, job["url"], plan)
        console.print(f"Updated route: [bold]{plan.route}[/bold] ({plan.confidence:.2f}) {plan.reasons}")
        if not apply:
            return

    if apply:
        job, plan = plans[0]
        if plan.route == icims.ROUTE_SKIP:
            console.print("[yellow]Route is skip (hCaptcha/classic with no mirror). Not launching Chrome.[/yellow]")
            raise typer.Exit(code=2)
        console.print(
            f"\nLab apply worker={icims.LAB_WORKER_ID} port={icims.LAB_CDP_PORT} "
            f"route={plan.route} dry_run={not submit}"
        )
        applied, failed = icims.lab_apply_one(
            job["url"],
            apply_url=plan.apply_url,
            headless=not headed,
            dry_run=not submit,
            model=model,
        )
        console.print(f"Lab result applied={applied} failed={failed}")
        raise typer.Exit(code=0 if applied else 1)


if __name__ == "__main__":
    app()
