"""Apply-trial instrumentation. Never stores tokens, passwords, cookies, or API keys."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from applypilot import config

logger = logging.getLogger(__name__)

SECRET_KEY_RE = re.compile(
    r"(token|password|cookie|authorization|api[_-]?key|secret|g-recaptcha-response|"
    r"h-captcha-response|captcha-response)",
    re.I,
)


def classify_ats(url: str) -> str:
    text = (url or "").lower()
    if "myworkdayjobs.com" in text or "workday" in text:
        return "workday"
    if "greenhouse.io" in text or "greenhouse" in text:
        return "greenhouse"
    if "lever.co" in text:
        return "lever"
    if "icims.com" in text:
        return "icims_classic"
    if "icims=" in text:
        return "icims_vanity"
    if "indeed.com" in text:
        return "indeed"
    if "linkedin.com" in text:
        return "linkedin"
    if "ziprecruiter.com" in text:
        return "ziprecruiter"
    return "other"


def listing_host(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Drop credential-like keys and values before persistence."""
    cleaned: dict[str, Any] = {}
    for key, value in record.items():
        if SECRET_KEY_RE.search(str(key)):
            continue
        if isinstance(value, str) and SECRET_KEY_RE.search(value) and len(value) > 40:
            continue
        cleaned[key] = value
    return cleaned


@dataclass
class ApplyTrial:
    job_id: str = ""
    company: str = ""
    title: str = ""
    ats: str = ""
    entry_host: str = ""
    final_host: str = ""
    route_used: str = ""
    captcha_provider: str = ""
    captcha_checkpoint: str = ""
    time_to_checkpoint: int | None = None
    provider_result: str = ""
    login_result: str = ""
    profile_result: str = ""
    submit_result: str = ""
    confirmation_detected: bool = False
    total_runtime: int | None = None
    job_url: str = ""
    worker_id: int | None = None
    captcha_type: str = ""
    apply_status: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def as_log_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confirmation_detected"] = bool(self.confirmation_detected)
        return sanitize_record(payload)


def jsonl_path() -> Path:
    config.ensure_dirs()
    return config.LOG_DIR / "icims_experiment.jsonl"


def append_jsonl(trial: ApplyTrial) -> None:
    path = jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(trial.as_log_dict(), ensure_ascii=False) + "\n")


def persist_trial(conn: sqlite3.Connection | None, trial: ApplyTrial) -> None:
    append_jsonl(trial)
    if conn is None:
        return
    conn.execute(
        """
        INSERT INTO apply_trials (
            started_at, finished_at, job_url, job_id, company, title, ats,
            entry_host, final_host, route_used, captcha_provider, captcha_checkpoint,
            time_to_checkpoint_ms, provider_result, login_result, profile_result,
            submit_result, confirmation_detected, total_runtime_ms, worker_id,
            captcha_type, apply_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trial.started_at,
            trial.finished_at or _now(),
            trial.job_url,
            trial.job_id,
            trial.company,
            trial.title,
            trial.ats,
            trial.entry_host,
            trial.final_host,
            trial.route_used,
            trial.captcha_provider,
            trial.captcha_checkpoint,
            trial.time_to_checkpoint,
            trial.provider_result,
            trial.login_result,
            trial.profile_result,
            trial.submit_result,
            1 if trial.confirmation_detected else 0,
            trial.total_runtime,
            trial.worker_id,
            trial.captcha_type,
            trial.apply_status,
        ),
    )
    conn.commit()


def trial_from_job(job: dict, *, worker_id: int | None = None) -> ApplyTrial:
    url = str(job.get("application_url") or job.get("url") or "")
    from applypilot.apply.icims import extract_req_id

    return ApplyTrial(
        job_id=extract_req_id(url) or extract_req_id(str(job.get("url") or "")) or "",
        company=str(job.get("site") or job.get("company") or ""),
        title=str(job.get("title") or ""),
        ats=classify_ats(url),
        entry_host=listing_host(url),
        route_used=str(job.get("apply_route") or ""),
        job_url=str(job.get("url") or url),
        worker_id=worker_id,
    )
