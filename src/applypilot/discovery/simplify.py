"""Synchronize Summer 2027 internship listings from SimplifyJobs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from applypilot import config
from applypilot.database import get_connection, init_db

logger = logging.getLogger(__name__)

SOURCE_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)
STRATEGY = "simplify-summer-2027"
GRAD_DEGREES = {"master's", "masters", "mba", "phd", "ph.d", "doctoral", "doctorate"}
USER_AGENT = "ApplyPilot/1.0 (local internship sync)"

_JOB_ID_RE = re.compile(r"/jobs/(\d+)", re.IGNORECASE)


def is_icims_listing_url(url: str | None) -> bool:
    """True for classic *.icims.com or vanity ?icims= career URLs."""
    text = (url or "").lower()
    return "icims.com" in text or "icims=" in text


def canonical_job_key(url: str | None) -> str | None:
    """Stable key for matching Simplify URLs against existing rows."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    job_id = _JOB_ID_RE.search(path)
    if job_id:
        return f"job:{job_id.group(1)}"
    return f"path:{parsed.netloc.lower()}{path.lower()}"


def _location_text(value) -> str | None:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    if value:
        return str(value)
    return None


def _has_undergrad_path(record: dict) -> bool:
    degrees = [str(item).strip().lower() for item in (record.get("degrees") or []) if item]
    if not degrees:
        return True
    return any(degree == "bachelor's" or degree == "bachelors" for degree in degrees)


def _is_grad_only(record: dict) -> bool:
    degrees = [str(item).strip().lower() for item in (record.get("degrees") or []) if item]
    if not degrees:
        return False
    return all(any(marker in degree for marker in GRAD_DEGREES) for degree in degrees)


def accepted_listing(record: dict) -> bool:
    """Return True when a Simplify record should enter the apply queue."""
    if not isinstance(record, dict):
        return False
    company = str(record.get("company_name") or "").strip()
    title = str(record.get("title") or "").strip()
    url = str(record.get("url") or "").strip()
    if not company or not title or not url:
        return False
    if not record.get("active") or not record.get("is_visible"):
        return False
    terms = record.get("terms") or []
    is_robinhood = company.lower() == "robinhood"
    if not is_robinhood and "Summer 2027" not in terms:
        return False
    if _is_grad_only(record) and not _has_undergrad_path(record):
        return False
    return True


def normalize_listing(record: dict) -> dict:
    url = str(record.get("url") or "").strip()
    locations = _location_text(record.get("locations"))
    terms = record.get("terms") or []
    description = " | ".join(
        part for part in (
            record.get("category"),
            locations,
            ", ".join(str(term) for term in terms),
        ) if part
    )
    return {
        "url": url,
        "title": str(record.get("title") or "").strip(),
        "site": str(record.get("company_name") or "").strip(),
        "location": locations,
        "description": description or None,
        "application_url": url,
        "strategy": STRATEGY,
    }


def fetch_listings() -> list[dict]:
    with httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(SOURCE_URL)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, list) or len(data) < 50:
        raise ValueError("Simplify snapshot was empty or malformed")
    return data


def _existing_keys(conn) -> set[str]:
    keys: set[str] = set()
    for row in conn.execute("SELECT url, application_url FROM jobs"):
        for value in (row["url"], row["application_url"]):
            key = canonical_job_key(value)
            if key:
                keys.add(key)
    return keys


def sync_listings(records: list[dict] | None = None) -> dict:
    """Insert unseen Simplify internships without touching application history."""
    init_db()
    snapshot = records if records is not None else fetch_listings()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    existing = _existing_keys(conn)
    inserted = 0
    skipped = 0
    excluded = 0
    try:
        conn.execute("BEGIN")
        for record in snapshot:
            if not accepted_listing(record):
                excluded += 1
                continue
            job = normalize_listing(record)
            key = canonical_job_key(job["url"])
            if not key or key in existing:
                skipped += 1
                continue
            parked = is_icims_listing_url(job["url"]) or is_icims_listing_url(
                job["application_url"]
            )
            conn.execute(
                """
                INSERT INTO jobs (
                    url, title, description, location, site, strategy,
                    discovered_at, application_url, apply_attempts,
                    apply_status, apply_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["url"], job["title"], job["description"], job["location"],
                    job["site"], job["strategy"], now, job["application_url"],
                    99 if parked else 0,
                    "failed" if parked else None,
                    "icims_unsupported" if parked else None,
                ),
            )
            existing.add(key)
            inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info(
        "Simplify sync inserted=%d skipped=%d excluded=%d received=%d",
        inserted, skipped, excluded, len(snapshot),
    )
    return {
        "received": len(snapshot),
        "inserted": inserted,
        "skipped": skipped,
        "excluded": excluded,
    }
