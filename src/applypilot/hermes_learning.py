"""Local outcome journal used by the Hermes ApplyPilot operator."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _learning_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "memories" / "applypilot-outcomes.jsonl"


def record_outcome(
    *,
    job: dict,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one sanitized result for Hermes learning.

    This is deliberately best-effort: a memory write must never block or fail an
    application result. Hermes can inspect the live project separately, while
    this durable journal remains free of credentials and applicant documents.
    """
    url = str(job.get("application_url") or job.get("url") or "")
    host = urlsplit(url).hostname or ""
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "site": str(job.get("site") or ""),
        "title": str(job.get("title") or ""),
        "host": host,
        "status": status,
        "error": error or "",
        "duration_ms": duration_ms,
    }
    path = _learning_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
