"""iCIMS routing: skip hCaptcha walls, keep vanity/session/social/Apply Network paths.

Classic `*.icims.com` login is blocked by hCaptcha, which CapSolver cannot solve.
This module classifies listings and picks a legitimate apply channel instead of
trying to defeat the challenge.

Production acquire still skips iCIMS. Use `applypilot icims-lab` to plan and
exercise these routes on an isolated Chrome (worker 40 / port 9362) that does
not share CDP ports with the live Workday fleet.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from applypilot import config

logger = logging.getLogger(__name__)

LAB_WORKER_ID = 40
LAB_CDP_PORT = 9322 + LAB_WORKER_ID  # 9362, outside live fleet 9322-9337

CLASSIC = "classic_icims"
VANITY = "vanity_icims"
NOT_ICIMS = "not_icims"

ROUTE_EXISTING_SESSION = "icims_existing_session"
ROUTE_VANITY = "icims_vanity"
ROUTE_SOCIAL = "icims_social_auth"
ROUTE_ACCOUNTLESS = "icims_accountless"
ROUTE_APPLY_NETWORK = "icims_apply_network"
ROUTE_CAPTCHA_PROVIDER = "icims_captcha_provider"
ROUTE_SKIP = "icims_blocked_hcaptcha"

LEGACY_CAPSOLVER_CONFIG = "capsolver:no-hcaptcha"
UNCERTAIN_ERRORS = frozenset({
    "application_outcome_unknown",
    "no_result_line",
    "captcha_submit_checkpoint",
})
SWE_TITLE_MARKERS = (
    "software", "swe ", "swe-", "developer", "engineer intern",
    "engineering intern", "full stack", "frontend", "backend",
    "full-stack", "front-end", "back-end",
)
SIMPLIFY_STRATEGY = "simplify-summer-2027"

POLICY_ALLOW = "allow"
POLICY_BLOCK = "block"
POLICY_TRY = "try"
HOST_POLICY_PATH = config.APP_DIR / "icims_hosts.json"

APPLY_NETWORK_HOSTS = (
    "indeed.com",
    "www.indeed.com",
    "linkedin.com",
    "www.linkedin.com",
    "ziprecruiter.com",
    "www.ziprecruiter.com",
)

NATIVE_APPLY_HOSTS = APPLY_NETWORK_HOSTS + (
    "smartapply.indeed.com",
    "www.ziprecruiter.com",
)

TITLE_STOPWORDS = frozenset({
    "intern", "internship", "internships", "co", "op", "coop", "co-op",
    "summer", "fall", "spring", "winter", "2025", "2026", "2027", "2028",
    "the", "a", "an", "of", "and", "for", "in", "at", "-",
})

COMPANY_STOPWORDS = frozenset({
    "inc", "inc.", "llc", "ltd", "corp", "corporation", "company", "co",
    "the", "group", "holdings", "technologies", "technology",
})

CLICK_APPLY_JS = """() => {
  const label = (node) => (node.innerText || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
  const nodes = [...document.querySelectorAll('a,button,[role="button"],input[type="submit"]')];
  const labels = nodes.map(label).filter((text) => text && text.length < 80).slice(0, 50);
  const score = (node) => {
    const text = label(node).toLowerCase();
    const href = (node.getAttribute('href') || '').toLowerCase();
    if (text === 'apply' || text === 'apply now' || text === 'apply for this job') return 0;
    if (text.startsWith('apply')) return 1;
    if (href.includes('apply')) return 2;
    if (text.includes('apply')) return 3;
    return 9;
  };
  const ranked = nodes.map((node) => ({ node, score: score(node), text: label(node) }))
    .filter((row) => row.score < 9)
    .sort((a, b) => a.score - b.score);
  if (!ranked.length) return { clicked: false, labels };
  ranked[0].node.click();
  return { clicked: true, labels, clickedText: ranked[0].text };
}"""
PAGE_SIGNALS_JS = """() => {
  const text = ((document.body && document.body.innerText) || '').toLowerCase();
  const label = (el) => ((el.innerText || el.getAttribute('aria-label') || el.value || '') + '').trim();
  const clickableMatch = (re) => [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
    .some((el) => re.test(label(el)));
  const social = {
    google: clickableMatch(/continue with google|sign in with google|sign up with google/i),
    microsoft: clickableMatch(/continue with microsoft|sign in with microsoft|sign in with azure/i),
    linkedin: clickableMatch(/continue with linkedin|sign in with linkedin/i),
  };
  return {
    url: location.href,
    hostname: location.hostname,
    hcaptcha: !!(document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"], textarea[name="h-captcha-response"]')),
    recaptcha: !!(document.querySelector('.g-recaptcha, iframe[src*="recaptcha"], textarea[name="g-recaptcha-response"]')),
    socialGoogle: !!social.google,
    socialMicrosoft: !!social.microsoft,
    socialLinkedin: !!social.linkedin,
    skipAccount: clickableMatch(/skip|continue as guest|apply without (an )?account|continue without (an )?account/i),
    authenticated: /(sign out|log out|logout|my profile|my applications|candidate home)/i.test(text)
      && !document.querySelector('input[type="password"]'),
    passwordLogin: !!document.querySelector('input[type="password"]'),
    classicHost: /(^|\\.)icims\\.com$/i.test(location.hostname),
  };
}"""


@dataclass
class Mirror:
    url: str
    title: str
    company: str
    location: str
    source: str
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class IcimsPlan:
    classification: str
    route: str
    apply_url: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    mirrors: list[Mirror] = field(default_factory=list)
    req_id: str = ""
    page_signals: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "route": self.route,
            "apply_url": self.apply_url,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "req_id": self.req_id,
            "mirrors": [
                {
                    "url": m.url,
                    "title": m.title,
                    "company": m.company,
                    "location": m.location,
                    "source": m.source,
                    "confidence": m.confidence,
                    "reasons": list(m.reasons),
                }
                for m in self.mirrors
            ],
            "page_signals": self.page_signals,
        }


def job_apply_url(job: dict) -> str:
    return str(job.get("application_url") or job.get("url") or "")


def classify_listing(url: str) -> str:
    """Return classic_icims, vanity_icims, or not_icims from the listing URL."""
    raw = (url or "").strip()
    if not raw:
        return NOT_ICIMS
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if host == "icims.com" or host.endswith(".icims.com"):
        return CLASSIC
    if "icims" in query or "icims=1" in raw.lower() or "icims=" in raw.lower():
        return VANITY
    return NOT_ICIMS


def is_icims_job(job: dict) -> bool:
    return classify_listing(job_apply_url(job)) != NOT_ICIMS or classify_listing(str(job.get("url") or "")) != NOT_ICIMS


def extract_req_id(url: str) -> str:
    """Pull a requisition/job id out of an iCIMS-style URL when present."""
    text = url or ""
    for pattern in (
        r"/jobs/(\d{4,})",
        r"[?&](?:jobId|jobid|requisitionId|requisition|reqid)=(\d{4,})",
        r"/job/(\d{4,})",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ""


def _tokens(value: str, extra_stop: Iterable[str] = ()) -> set[str]:
    stop = TITLE_STOPWORDS | {item.lower() for item in extra_stop}
    parts = re.findall(r"[a-z0-9]+", (value or "").lower())
    return {part for part in parts if part not in stop and len(part) > 1}


def normalize_title(value: str) -> str:
    return " ".join(sorted(_tokens(value)))


ALIAS_GROUPS = (
    frozenset({"amd", "advanced micro devices"}),
    frozenset({"jhuapl", "jhu apl", "johns hopkins applied physics laboratory", "johns hopkins apl"}),
    frozenset({"principal", "principal financial", "principal financial group"}),
    frozenset({"garmin", "garmin international"}),
    frozenset({"ulta", "ulta beauty"}),
    frozenset({"cvent"}),
    frozenset({"fast enterprises", "fastenterprises"}),
    frozenset({"comed", "exelon", "constellation energy"}),
)


def _ordered_company_tokens(value: str) -> list[str]:
    parts = re.findall(r"[a-z0-9]+", (value or "").lower())
    return [part for part in parts if part not in COMPANY_STOPWORDS and len(part) > 1]


def company_keys(value: str) -> set[str]:
    raw = re.sub(r"\s+", " ", (value or "").lower()).strip()
    keys = {raw, re.sub(r"[^a-z0-9]", "", raw)} if raw else set()
    tokens = _ordered_company_tokens(value)
    keys.update(tokens)
    if len(tokens) >= 2:
        initials = "".join(token[0] for token in tokens)
        if 2 <= len(initials) <= 5:
            keys.add(initials)
    for group in ALIAS_GROUPS:
        if keys & group:
            keys |= set(group)
    keys.discard("")
    return keys


def companies_match(left: str, right: str) -> bool:
    return bool(company_keys(left) & company_keys(right))


def titles_match(left: str, right: str) -> bool:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return False
    if a == b or a <= b or b <= a:
        return True
    overlap = a & b
    needed = min(len(a), len(b))
    return needed >= 2 and len(overlap) == needed


def locations_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return True
    if "remote" in a or "remote" in b:
        return True
    return bool(a & b)


def is_native_apply_url(url: str) -> bool:
    """True when the URL looks like a board-native apply, not an iCIMS bounce."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host == "icims.com" or host.endswith(".icims.com") or "icims=" in (url or "").lower():
        return False
    return any(host == item or host.endswith("." + item) for item in NATIVE_APPLY_HOSTS)


def score_mirror(job: dict, listing: dict) -> Mirror | None:
    """Score one board listing against an iCIMS job. None if it is not usable."""
    url = str(listing.get("url") or listing.get("job_url") or listing.get("job_url_direct") or "")
    if not is_native_apply_url(url):
        return None
    title = str(listing.get("title") or "")
    company = str(listing.get("company") or listing.get("site") or "")
    location = str(listing.get("location") or "")
    source = str(listing.get("source") or listing.get("site") or urlparse(url).hostname or "")
    job_title = str(job.get("title") or "")
    job_company = str(job.get("site") or job.get("company") or "")
    job_location = str(job.get("location") or "")
    job_url = job_apply_url(job)
    req = extract_req_id(job_url) or extract_req_id(str(job.get("url") or ""))
    listing_req = extract_req_id(url)
    blob = " ".join(
        str(listing.get(key) or "")
        for key in ("url", "title", "description", "job_url", "job_url_direct")
    )
    reasons: list[str] = []
    confidence = 0.0

    company_ok = companies_match(job_company, company)
    title_ok = titles_match(job_title, title)
    location_ok = locations_compatible(job_location, location)
    req_ok = bool(req) and (req == listing_req or req in blob)

    if not company_ok:
        return None
    reasons.append("company")
    confidence += 0.33
    if title_ok:
        reasons.append("title")
        confidence += 0.33
    if location_ok:
        reasons.append("location")
        confidence += 0.07
    if req_ok:
        reasons.append(f"req:{req}")
        confidence += 0.32
    if not title_ok:
        return None
    # Wrong requisition is worse than skipping.
    if req and listing_req and req != listing_req:
        return None
    confidence = min(confidence, 1.0)
    if confidence < 0.98 and not req_ok:
        return None
    if confidence < 0.98:
        return None
    return Mirror(
        url=url,
        title=title,
        company=company,
        location=location,
        source=source,
        confidence=round(confidence, 3),
        reasons=reasons,
    )


def select_best_mirror(job: dict, listings: Iterable[dict]) -> Mirror | None:
    scored = [item for item in (score_mirror(job, listing) for listing in listings) if item]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item.confidence, item.url))
    return scored[0]


def listings_from_jobspy_frame(df) -> list[dict]:
    """Normalize a JobSpy DataFrame (or list of row mappings) into listing dicts."""
    if df is None:
        return []
    if isinstance(df, list):
        return list(df)
    rows: list[dict] = []
    for _, row in df.iterrows():
        job_url = str(row.get("job_url") or "")
        direct = str(row.get("job_url_direct") or "")
        url = direct if is_native_apply_url(direct) else job_url
        rows.append({
            "url": url,
            "job_url": job_url,
            "job_url_direct": direct,
            "title": "" if str(row.get("title", "")) == "nan" else str(row.get("title") or ""),
            "company": "" if str(row.get("company", "")) == "nan" else str(row.get("company") or ""),
            "location": "" if str(row.get("location", "")) == "nan" else str(row.get("location") or ""),
            "description": "" if str(row.get("description", "")) == "nan" else str(row.get("description") or ""),
            "source": "" if str(row.get("site", "")) == "nan" else str(row.get("site") or ""),
            "site": "" if str(row.get("site", "")) == "nan" else str(row.get("site") or ""),
        })
    return rows


def find_db_mirrors(job: dict, conn) -> list[Mirror]:
    """Find already-imported Indeed/LinkedIn/ZipRecruiter rows that match this job."""
    rows = conn.execute(
        """
        SELECT url, title, site, location, application_url, full_description, description
          FROM jobs
         WHERE url != ?
           AND (
                LOWER(url) LIKE '%indeed.com%'
             OR LOWER(url) LIKE '%linkedin.com%'
             OR LOWER(url) LIKE '%ziprecruiter.com%'
             OR LOWER(COALESCE(application_url, '')) LIKE '%indeed.com%'
             OR LOWER(COALESCE(application_url, '')) LIKE '%linkedin.com%'
             OR LOWER(COALESCE(application_url, '')) LIKE '%ziprecruiter.com%'
           )
        """,
        (job.get("url") or "",),
    ).fetchall()
    listings = []
    for row in rows:
        mapping = dict(row)
        listings.append({
            "url": mapping.get("application_url") or mapping.get("url"),
            "title": mapping.get("title") or "",
            "company": mapping.get("site") or "",
            "location": mapping.get("location") or "",
            "description": mapping.get("full_description") or mapping.get("description") or "",
            "source": mapping.get("site") or "",
        })
    scored = [item for item in (score_mirror(job, listing) for listing in listings) if item]
    scored.sort(key=lambda item: (-item.confidence, item.url))
    return scored


def search_apply_network(
    job: dict,
    scrape: Callable[..., Any] | None = None,
    results_wanted: int = 15,
) -> list[Mirror]:
    """Search Indeed/ZipRecruiter/LinkedIn for a native-apply mirror of this job."""
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    company = str(job.get("site") or "")
    title = str(job.get("title") or "")
    location = str(job.get("location") or "United States")
    req = extract_req_id(job_apply_url(job)) or extract_req_id(str(job.get("url") or ""))
    query = " ".join(part for part in (company, req, title) if part).strip()
    if not query:
        return []
    kwargs = {
        "site_name": ["indeed", "zip_recruiter", "linkedin"],
        "search_term": query,
        "location": location or "United States",
        "results_wanted": results_wanted,
        "hours_old": 24 * 90,
        "country_indeed": "usa",
        "verbose": 0,
    }
    try:
        frame = scrape(**kwargs)
    except Exception:
        logger.exception("Apply Network search failed for %s", query[:80])
        return []
    scored = [
        item for item in (
            score_mirror(job, listing) for listing in listings_from_jobspy_frame(frame)
        ) if item
    ]
    scored.sort(key=lambda item: (-item.confidence, item.url))
    return scored


def plan_from_signals(
    job: dict,
    *,
    signals: dict[str, Any] | None = None,
    mirrors: list[Mirror] | None = None,
    captcha_provider: str | None = None,
) -> IcimsPlan:
    """Choose a route from listing type, optional live page signals, and mirrors.

    Order: existing session → guest → social → vanity → official mirror →
    experimental CAPTCHA provider → skip.
    """
    url = job_apply_url(job)
    classification = classify_listing(url)
    if classification == NOT_ICIMS:
        classification = classify_listing(str(job.get("url") or ""))
        url = str(job.get("url") or url)
    req = extract_req_id(url) or extract_req_id(str(job.get("url") or ""))
    mirrors = list(mirrors or [])
    best = mirrors[0] if mirrors else None
    reasons: list[str] = [classification]

    if classification == NOT_ICIMS:
        return IcimsPlan(
            classification=classification,
            route="direct",
            apply_url=url,
            confidence=1.0,
            reasons=["not_icims"],
            req_id=req,
            page_signals=signals,
        )

    if signals:
        if signals.get("authenticated") and not signals.get("hcaptcha"):
            return IcimsPlan(
                CLASSIC if signals.get("classicHost") else classification,
                ROUTE_EXISTING_SESSION,
                str(signals.get("url") or url),
                0.9,
                reasons + ["authenticated_session"],
                mirrors,
                req,
                signals,
            )
        if signals.get("skipAccount") and not signals.get("hcaptcha"):
            return IcimsPlan(
                classification,
                ROUTE_ACCOUNTLESS,
                str(signals.get("url") or url),
                0.8,
                reasons + ["skip_or_guest_control"],
                mirrors,
                req,
                signals,
            )
        social = any(signals.get(key) for key in ("socialGoogle", "socialMicrosoft", "socialLinkedin"))
        if social and not signals.get("hcaptcha"):
            return IcimsPlan(
                classification,
                ROUTE_SOCIAL,
                str(signals.get("url") or url),
                0.75,
                reasons + ["social_login_available"],
                mirrors,
                req,
                signals,
            )
        if classification == VANITY and not signals.get("hcaptcha") and not signals.get("classicHost"):
            return IcimsPlan(
                classification,
                ROUTE_VANITY,
                url,
                0.7,
                reasons + ["vanity_no_hcaptcha"],
                mirrors,
                req,
                signals,
            )
        if signals.get("hcaptcha") and best and best.confidence >= 0.98:
            return IcimsPlan(
                classification,
                ROUTE_APPLY_NETWORK,
                best.url,
                best.confidence,
                reasons + ["hcaptcha"] + best.reasons,
                mirrors,
                req,
                signals,
            )
        if signals.get("hcaptcha") and captcha_provider == "nopecha":
            return IcimsPlan(
                classification,
                ROUTE_CAPTCHA_PROVIDER,
                str(signals.get("url") or url),
                0.4,
                reasons + ["experimental_hcaptcha_provider"],
                mirrors,
                req,
                signals,
            )
        if signals.get("hcaptcha"):
            return IcimsPlan(
                classification,
                ROUTE_SKIP,
                url,
                1.0,
                reasons + ["hcaptcha_unsupported"],
                mirrors,
                req,
                signals,
            )

    if classification == VANITY:
        return IcimsPlan(
            classification,
            ROUTE_VANITY,
            url,
            0.55,
            reasons + ["unprobed_vanity"],
            mirrors,
            req,
            signals,
        )
    if best and best.confidence >= 0.98:
        return IcimsPlan(
            classification,
            ROUTE_APPLY_NETWORK,
            best.url,
            best.confidence,
            reasons + best.reasons,
            mirrors,
            req,
            signals,
        )
    if captcha_provider == "nopecha":
        return IcimsPlan(
            classification,
            ROUTE_CAPTCHA_PROVIDER,
            url,
            0.35,
            reasons + ["experimental_hcaptcha_provider_unprobed"],
            mirrors,
            req,
            signals,
        )
    return IcimsPlan(
        classification,
        ROUTE_SKIP,
        url,
        1.0,
        reasons + ["classic_unprobed_no_mirror"],
        mirrors,
        req,
        signals,
    )


def plan_job(
    job: dict,
    *,
    conn=None,
    listings: Iterable[dict] | None = None,
    signals: dict[str, Any] | None = None,
    search: bool = False,
    scrape: Callable[..., Any] | None = None,
    captcha_provider: str | None = None,
) -> IcimsPlan:
    """Build a route plan. Network search is opt-in so unit tests stay offline."""
    mirrors: list[Mirror] = []
    if listings is not None:
        best = select_best_mirror(job, listings)
        if best:
            mirrors.append(best)
    if conn is not None:
        mirrors.extend(find_db_mirrors(job, conn))
    if search:
        mirrors.extend(search_apply_network(job, scrape=scrape))
    # Deduplicate by URL, keep highest confidence.
    by_url: dict[str, Mirror] = {}
    for mirror in mirrors:
        current = by_url.get(mirror.url)
        if current is None or mirror.confidence > current.confidence:
            by_url[mirror.url] = mirror
    ordered = sorted(by_url.values(), key=lambda item: (-item.confidence, item.url))
    return plan_from_signals(
        job, signals=signals, mirrors=ordered, captcha_provider=captcha_provider,
    )


def inspect_page_signals(port: int) -> dict[str, Any] | None:
    """Read login/captcha/social controls from a live Chrome via CDP."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages = [page for context in browser.contexts for page in context.pages]
            for page in pages:
                try:
                    return page.evaluate(PAGE_SIGNALS_JS)
                except Exception:
                    continue
    except Exception:
        logger.debug("iCIMS page-signal inspect failed on port %d", port, exc_info=True)
    return None


def icims_prompt_addon(job: dict, plan: IcimsPlan | None = None) -> str:
    """Extra Luna instructions for iCIMS / Apply Network applications."""
    route = (plan.route if plan else "") or ""
    lines = [
        "iCIMS / APPLY CHANNEL RULES",
        "- Never try to solve hCaptcha. If hCaptcha is visible, output RESULT:CAPTCHA immediately.",
        "- Prefer an already-signed-in session: if the application form is reachable without creating a password, stay on that path.",
        "- If you see Continue with Google, Microsoft, or LinkedIn, use that before creating a local username/password. Do not start a new identity-provider account; only continue if the browser is already signed into that provider.",
        "- If the portal offers Skip, Continue as guest, or Apply without an account, take that path.",
        "- Do not treat a vanity career-site URL as classic iCIMS login. Stay on the current host unless the page itself navigates.",
        "- If the current URL is Indeed, LinkedIn, or ZipRecruiter, apply natively there. If that site redirects back to icims.com login, stop with RESULT:FAILED:icims_redirect.",
    ]
    if route == ROUTE_SOCIAL:
        lines.append("- This job was routed to social login. Prefer Continue with Microsoft, then Google, then LinkedIn.")
    elif route == ROUTE_APPLY_NETWORK:
        lines.append("- This job was routed to an official Apply Network mirror. Apply on the current board; do not navigate to icims.com yourself.")
    elif route == ROUTE_VANITY:
        lines.append("- This is a vanity/direct career site. Continue the on-page application. If it dumps you onto icims.com login with hCaptcha, RESULT:CAPTCHA immediately.")
    elif route == ROUTE_EXISTING_SESSION:
        lines.append("- A previous session for this employer may already be signed in. Do not log out. Continue the application.")
    elif route == ROUTE_CAPTCHA_PROVIDER:
        lines.append("- This job may hit hCaptcha on iCIMS login. Do not click or reason through it. Output RESULT:CAPTCHA immediately so the launcher can hand the same browser to the configured provider.")
    return "\n".join(lines)


def persist_plan(conn, job_url: str, plan: IcimsPlan) -> None:
    """Store the chosen route on the job row when those columns exist."""
    columns = {row[0] for row in conn.execute("SELECT name FROM pragma_table_info('jobs')")}
    assignments = []
    values: list[Any] = []
    if "apply_route" in columns:
        assignments.append("apply_route = ?")
        values.append(plan.route)
    if "icims_mirror_url" in columns:
        assignments.append("icims_mirror_url = ?")
        values.append(plan.apply_url if plan.route == ROUTE_APPLY_NETWORK else None)
    if not assignments:
        return
    values.append(job_url)
    conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE url = ?", values)
    conn.commit()


_network_lock = threading.Lock()


def listing_hostname(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def load_host_policy() -> dict[str, dict[str, Any]]:
    path = HOST_POLICY_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("hosts", data) if isinstance(data, dict) else {}


def save_host_policy(hosts: dict[str, dict[str, Any]]) -> None:
    HOST_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOST_POLICY_PATH.write_text(
        json.dumps({"hosts": hosts}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def record_host_policy(host: str, decision: str, reason: str, **extra: Any) -> None:
    if not host:
        return
    hosts = load_host_policy()
    entry = hosts.get(host) or {}
    entry.update({"decision": decision, "reason": reason, **extra})
    hosts[host] = entry
    save_host_policy(hosts)


def host_decision(url: str) -> str | None:
    host = listing_hostname(url)
    if not host:
        return None
    if host == "icims.com" or host.endswith(".icims.com"):
        return POLICY_BLOCK
    entry = load_host_policy().get(host) or {}
    decision = entry.get("decision")
    return str(decision) if decision else None


def blocked_hostnames() -> list[str]:
    blocked = []
    for host, entry in load_host_policy().items():
        if (entry or {}).get("decision") == POLICY_BLOCK:
            blocked.append(host)
    return blocked


def stored_provider_config(host: str) -> str:
    entry = load_host_policy().get(host) or {}
    return str(entry.get("provider_config") or LEGACY_CAPSOLVER_CONFIG)


def experimental_retry_allowed(
    url: str,
    *,
    retry_hosts: Iterable[str] | None,
    provider_config: str,
) -> bool:
    """True when a blocked host may be retried under a different solver config."""
    host = listing_hostname(url)
    if not host:
        return False
    allowed = {item.lower() for item in (retry_hosts or []) if item}
    if host not in allowed:
        return False
    entry = load_host_policy().get(host) or {}
    if (entry or {}).get("decision") != POLICY_BLOCK:
        return True
    return stored_provider_config(host) != provider_config


def is_swe_intern(job: dict) -> bool:
    title = str(job.get("title") or "").lower()
    return any(marker in title for marker in SWE_TITLE_MARKERS)


def duplicate_block_reason(conn, job: dict) -> str | None:
    """Return a reason if this candidate+requisition must not be submitted again."""
    url = str(job.get("url") or "")
    apply_url = job_apply_url(job)
    req = extract_req_id(apply_url) or extract_req_id(url)
    company = str(job.get("site") or job.get("company") or "").strip().lower()
    row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if row:
        status = str(row["apply_status"] or "")
        error = str(row["apply_error"] or "")
        if status == "applied":
            return "already_applied"
        if status == "in_progress":
            return "already_running"
        if error in UNCERTAIN_ERRORS or error.endswith("application_outcome_unknown"):
            return "uncertain_outcome"
    if not req or not company:
        return None
    like = f"%/{req}%"
    siblings = conn.execute(
        """
        SELECT url, apply_status, apply_error FROM jobs
         WHERE url != ?
           AND LOWER(TRIM(COALESCE(site, ''))) = ?
           AND (url LIKE ? OR COALESCE(application_url, '') LIKE ?)
        """,
        (url, company, like, like),
    ).fetchall()
    for sibling in siblings:
        status = str(sibling["apply_status"] or "")
        error = str(sibling["apply_error"] or "")
        if status == "applied":
            return "already_applied_req"
        if status == "in_progress":
            return "already_running_req"
        if error in UNCERTAIN_ERRORS:
            return "uncertain_outcome"
    return None


def experiment_jobs(
    conn,
    *,
    limit: int = 3,
    retry_hosts: Iterable[str] | None = None,
    provider_config: str = "",
    url: str | None = None,
) -> list[dict]:
    """Small Simplify SWE iCIMS batch for the isolated lab worker."""
    if url:
        return lab_jobs(conn, url=url, limit=1)
    retry = {item.lower() for item in (retry_hosts or []) if item}
    rows = lab_jobs(conn, limit=max(limit * 8, 24))
    selected: list[dict] = []
    for job in rows:
        if not is_swe_intern(job):
            continue
        strategy = str(job.get("strategy") or "")
        if strategy and strategy != SIMPLIFY_STRATEGY:
            continue
        if duplicate_block_reason(conn, job):
            continue
        host = listing_hostname(job_apply_url(job))
        if host_decision(job_apply_url(job)) == POLICY_BLOCK:
            if not experimental_retry_allowed(
                job_apply_url(job),
                retry_hosts=retry,
                provider_config=provider_config,
            ):
                continue
        selected.append(job)
        if len(selected) >= limit:
            break
    return selected


def classify_probe_signals(signals: dict[str, Any] | None) -> str:
    """allow / block / try from a live page probe."""
    if not signals:
        return POLICY_TRY
    if signals.get("hcaptcha") or signals.get("classicHost"):
        return POLICY_BLOCK
    host = str(signals.get("hostname") or "")
    if host == "icims.com" or host.endswith(".icims.com"):
        return POLICY_BLOCK
    if signals.get("followedApply") and not signals.get("hcaptcha"):
        return POLICY_ALLOW
    return POLICY_TRY


def seed_policy_from_jobs(conn) -> None:
    """Mark hosts with a successful vanity apply as allow."""
    hosts = load_host_policy()
    changed = False
    rows = conn.execute(
        """
        SELECT url, application_url FROM jobs
         WHERE apply_status = 'applied'
           AND (
                LOWER(COALESCE(application_url, url)) LIKE '%icims=%'
             OR LOWER(url) LIKE '%icims=%'
           )
           AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims.com%'
           AND LOWER(url) NOT LIKE '%icims.com%'
        """
    ).fetchall()
    for row in rows:
        host = listing_hostname(row["application_url"] or row["url"])
        if host and (hosts.get(host) or {}).get("decision") != POLICY_BLOCK:
            hosts[host] = {
                "decision": POLICY_ALLOW,
                "reason": "prior_applied",
            }
            changed = True
    if changed:
        save_host_policy(hosts)


def sync_jobs_to_host_policy(conn) -> dict[str, int]:
    """Park blocked vanity hosts; unhold allow/try vanity held for lab."""
    hosts = load_host_policy()
    blocked = [host for host, entry in hosts.items() if (entry or {}).get("decision") == POLICY_BLOCK]
    allowed = [
        host for host, entry in hosts.items()
        if (entry or {}).get("decision") in {POLICY_ALLOW, POLICY_TRY}
    ]
    parked = 0
    unheld = 0
    for host in blocked:
        like = f"%{host}%"
        cursor = conn.execute(
            """
            UPDATE jobs
               SET apply_status = 'failed',
                   apply_error = 'icims_blocked_hcaptcha',
                   apply_attempts = 99,
                   agent_id = NULL
             WHERE COALESCE(apply_status, '') NOT IN ('applied', 'in_progress')
               AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims.com%'
               AND LOWER(url) NOT LIKE '%icims.com%'
               AND (url LIKE ? OR COALESCE(application_url, '') LIKE ?)
            """,
            (like, like),
        )
        parked += cursor.rowcount
    for host in allowed:
        like = f"%{host}%"
        cursor = conn.execute(
            """
            UPDATE jobs
               SET apply_status = NULL,
                   apply_error = NULL,
                   apply_attempts = 0,
                   agent_id = NULL
             WHERE COALESCE(apply_status, '') NOT IN ('applied', 'in_progress')
               AND COALESCE(apply_error, '') IN (
                    'icims_lab_hold', 'icims_unsupported', 'icims_blocked_hcaptcha'
               )
               AND LOWER(COALESCE(application_url, url)) NOT LIKE '%icims.com%'
               AND LOWER(url) NOT LIKE '%icims.com%'
               AND (url LIKE ? OR COALESCE(application_url, '') LIKE ?)
            """,
            (like, like),
        )
        unheld += cursor.rowcount
    conn.commit()
    return {"parked": parked, "unheld": unheld}


def maybe_apply_network_url(job: dict) -> str | None:
    """Search Apply Network once; return a native-apply URL or None."""
    with _network_lock:
        plan = plan_job(job, search=True)
    if plan.route == ROUTE_APPLY_NETWORK and is_native_apply_url(plan.apply_url):
        return plan.apply_url
    return None


def lab_jobs(conn, *, url: str | None = None, limit: int = 8) -> list[dict]:
    """Return iCIMS rows the live fleet is holding for lab work."""
    if url:
        row = conn.execute(
            """
        SELECT url, title, site, location, application_url, apply_status, apply_error, apply_route, strategy
          FROM jobs
         WHERE url = ? OR application_url = ?
             LIMIT 1
            """,
            (url, url),
        ).fetchone()
        return [dict(row)] if row else [{
            "url": url,
            "application_url": url,
            "title": "",
            "site": "",
            "location": "",
        }]
    rows = conn.execute(
        """
        SELECT url, title, site, location, application_url, apply_status, apply_error, apply_route, strategy
          FROM jobs
         WHERE COALESCE(apply_status, '') != 'applied'
           AND (
                LOWER(COALESCE(application_url, url)) LIKE '%icims.com%'
             OR LOWER(url) LIKE '%icims.com%'
             OR LOWER(COALESCE(application_url, url)) LIKE '%icims=%'
             OR LOWER(url) LIKE '%icims=%'
             OR COALESCE(apply_error, '') IN ('icims_lab_hold', 'icims_unsupported')
           )
         ORDER BY
           CASE WHEN LOWER(COALESCE(application_url, url)) LIKE '%icims.com%' THEN 1 ELSE 0 END,
           url
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _probe_open_page(page, url: str) -> dict[str, Any]:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)
    click = page.evaluate(CLICK_APPLY_JS)
    if isinstance(click, dict) and click.get("clicked"):
        page.wait_for_timeout(7000)
        pages = page.context.pages
        if pages:
            page = pages[-1]
    signals = page.evaluate(PAGE_SIGNALS_JS) or {}
    if isinstance(click, dict):
        signals["followedApply"] = bool(click.get("clicked"))
        signals["clickedText"] = click.get("clickedText")
        signals["applyLabels"] = (click.get("labels") or [])[:12]
    else:
        signals["followedApply"] = bool(click)
    signals["listingUrl"] = url
    return signals


def lab_probe(url: str, *, headless: bool = True) -> dict[str, Any] | None:
    """Open an isolated Chrome (port 9362) and read iCIMS page signals."""
    from playwright.sync_api import sync_playwright

    from applypilot.apply.chrome import cleanup_worker, launch_chrome

    proc = launch_chrome(LAB_WORKER_ID, port=LAB_CDP_PORT, headless=headless)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{LAB_CDP_PORT}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            return _probe_open_page(page, url)
    except Exception:
        logger.exception("iCIMS lab probe failed for %s", url[:120])
        return None
    finally:
        cleanup_worker(LAB_WORKER_ID, proc)


def survey_vanity_hosts(jobs: list[dict], *, headless: bool = True) -> dict[str, dict[str, Any]]:
    """Probe one listing per hostname and write allow/block/try policy."""
    from playwright.sync_api import sync_playwright

    from applypilot.apply.chrome import cleanup_worker, launch_chrome

    by_host: dict[str, dict] = {}
    for job in jobs:
        url = job_apply_url(job)
        host = listing_hostname(url)
        if not host or host.endswith("icims.com"):
            continue
        by_host.setdefault(host, job)

    results: dict[str, dict[str, Any]] = {}
    proc = launch_chrome(LAB_WORKER_ID, port=LAB_CDP_PORT, headless=headless)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{LAB_CDP_PORT}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            for host, job in by_host.items():
                url = job_apply_url(job)
                try:
                    signals = _probe_open_page(page, url)
                except Exception:
                    logger.exception("Survey failed for %s", host)
                    signals = {"hostname": host, "hcaptcha": False, "followedApply": False}
                decision = classify_probe_signals(signals)
                record_host_policy(
                    host,
                    decision,
                    "survey",
                    url=signals.get("url") or url,
                    hcaptcha=bool(signals.get("hcaptcha")),
                    followedApply=bool(signals.get("followedApply")),
                    classicHost=bool(signals.get("classicHost")),
                )
                results[host] = {"decision": decision, "signals": signals, "title": job.get("title")}
                logger.info("iCIMS survey %s -> %s hcaptcha=%s apply=%s",
                            host, decision, signals.get("hcaptcha"), signals.get("followedApply"))
    finally:
        cleanup_worker(LAB_WORKER_ID, proc)
    return results


def lab_apply_one(
    job_url: str,
    *,
    apply_url: str | None = None,
    headless: bool = True,
    dry_run: bool = True,
    model: str = "gpt-5.6-luna",
) -> tuple[int, int]:
    """Run one isolated apply on worker 40. Does not start the live fleet."""
    from applypilot.apply.launcher import worker_loop
    from applypilot.database import get_connection

    if apply_url:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET application_url = ? WHERE url = ?",
            (apply_url, job_url),
        )
        conn.commit()
    return worker_loop(
        worker_id=LAB_WORKER_ID,
        limit=1,
        target_url=job_url,
        min_score=0,
        headless=headless,
        model=model,
        dry_run=dry_run,
    )


FLEET_CDP_FIRST = 9322
FLEET_CDP_LAST = 9337  # workers 0-15


def count_fleet_cdp_ports() -> int:
    """How many live-fleet Chrome debug ports are listening. Does not touch them."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{FLEET_CDP_FIRST}-{FLEET_CDP_LAST}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return 0
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        if "LISTEN" not in line:
            continue
        for part in line.split():
            if ":" not in part:
                continue
            maybe = part.rsplit(":", 1)[-1]
            if maybe.isdigit():
                port = int(maybe)
                if FLEET_CDP_FIRST <= port <= FLEET_CDP_LAST:
                    ports.add(port)
    return len(ports)


def load_jobs_csv(path: str | Path) -> list[dict]:
    """Load the authorized iCIMS job list. Does not read the live apply queue."""
    import csv
    from pathlib import Path as _Path

    rows: list[dict] = []
    with _Path(path).expanduser().open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            rows.append({
                "url": url,
                "application_url": (row.get("application_url") or url).strip(),
                "title": (row.get("title") or "").strip(),
                "site": (row.get("company") or row.get("site") or "").strip(),
                "location": (row.get("location") or "").strip(),
            })
    return rows


def ensure_csv_job(conn, job: dict) -> None:
    """Insert a CSV job if missing. Never reset an already-applied row."""
    existing = conn.execute(
        "SELECT url, apply_status FROM jobs WHERE url = ? OR application_url = ?",
        (job["url"], job["url"]),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, location, application_url, strategy,
            apply_status, apply_error, apply_attempts
        ) VALUES (?, ?, ?, ?, ?, ?, 'failed', 'icims_lab_hold', 0)
        """,
        (
            job["url"],
            job.get("title"),
            job.get("site"),
            job.get("location"),
            job.get("application_url") or job["url"],
            SIMPLIFY_STRATEGY,
        ),
    )
    conn.commit()


def run_csv_queue(
    csv_path: str | Path,
    *,
    workers: int = 1,
    max_workers: int = 4,
    watch_fleet: bool = True,
    submit: bool = False,
    headed: bool = True,
    model: str = "gpt-5.6-luna",
) -> tuple[int, int]:
    """Apply only CSV iCIMS jobs on worker 40+. Never launches the live fleet."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from queue import Queue

    from applypilot.apply import launcher
    from applypilot.apply.dashboard import init_worker
    from applypilot.database import get_connection, init_db

    init_db()
    conn = get_connection()
    pending: Queue[str] = Queue()
    queued = 0
    skipped = 0
    for row in load_jobs_csv(csv_path):
        ensure_csv_job(conn, row)
        job_row = conn.execute(
            "SELECT url, title, site, location, application_url, apply_status, apply_error FROM jobs WHERE url = ? OR application_url = ?",
            (row["url"], row["url"]),
        ).fetchone()
        job = dict(job_row) if job_row else row
        reason = duplicate_block_reason(conn, job)
        if reason:
            skipped += 1
            logger.info("CSV skip %s (%s)", job.get("url", "")[:90], reason)
            continue
        plan = plan_job(job, conn=conn, captcha_provider="nopecha")
        persist_plan(conn, job["url"], plan)
        pending.put(job["url"])
        queued += 1
    logger.info("iCIMS CSV queued=%d skipped=%d", queued, skipped)
    if queued == 0:
        return 0, 0

    launcher._stop_event.clear()
    start_workers = max(1, workers)
    cap = max(start_workers, max_workers)
    started = 0
    applied_total = 0
    failed_total = 0

    def _run(wid: int, headless: bool) -> tuple[int, int]:
        init_worker(wid)
        return launcher.worker_loop(
            worker_id=wid,
            limit=0,
            min_score=0,
            headless=headless,
            model=model,
            dry_run=not submit,
            url_queue=pending,
        )

    def _start(pool: ThreadPoolExecutor):
        nonlocal started
        wid = LAB_WORKER_ID + started
        # Extension solve needs a real window; keep every lab worker headed.
        headless = not headed
        started += 1
        launcher._worker_count = started
        return pool.submit(_run, wid, headless)

    with ThreadPoolExecutor(max_workers=cap) as pool:
        futures = {_start(pool)}
        while futures:
            if watch_fleet and started < cap and not pending.empty():
                fleet = count_fleet_cdp_ports()
                desired = min(cap, max(start_workers, 16 - fleet))
                while started < desired and not pending.empty():
                    logger.info(
                        "Scaling iCIMS workers to %d (live fleet chrome=%d)",
                        started + 1, fleet,
                    )
                    futures.add(_start(pool))
            done, remaining = wait(futures, timeout=20, return_when=FIRST_COMPLETED)
            futures = remaining
            for fut in done:
                try:
                    applied, failed = fut.result()
                    applied_total += applied
                    failed_total += failed
                except Exception:
                    logger.exception("Isolated iCIMS worker crashed")
                    failed_total += 1
            if not futures and not pending.empty() and started < cap:
                logger.info("Restarting an isolated iCIMS worker; queue still has jobs")
                futures.add(_start(pool))
            if not futures and pending.empty():
                break
    return applied_total, failed_total
