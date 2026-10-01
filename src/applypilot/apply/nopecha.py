"""NopeCHA token client and Chrome-extension helper for hCaptcha.

Never logs tokens, cookies, or API keys.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from applypilot import config

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_BASE = "https://api.nopecha.com"
TOKEN_PATH = "/token/"
STATUS_PATH = "/status/"
INCOMPLETE_JOB = 14
PROVIDER_NAME = "nopecha"
EXTENSION_VERSION = "0.6.1"
EXTENSION_ZIP_NAME = "chromium_automation.zip"
EXTENSION_URL = (
    "https://github.com/NopeCHALLC/nopecha-extension/releases/latest/download/"
    + EXTENSION_ZIP_NAME
)
RESPONSE_JS = """() => {
  const names = ['h-captcha-response', 'g-recaptcha-response'];
  const nodes = names.flatMap((name) => [...document.querySelectorAll('textarea[name="' + name + '"]')]);
  return Math.max(0, ...nodes.map((el) => String(el && el.value || '').length), 0);
}"""

# Set only after a live Chrome reports the NopeCHA service worker.
# prepare_extension() is not enough: branded Chrome 137+ ignores --load-extension.
_extension_active = False


class NopechaError(Exception):
    """Raised when the NopeCHA API returns an error or times out."""

    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(description if not code else f"{code}: {description}")


def get_api_key() -> str | None:
    key = (os.environ.get("NOPECHA_API_KEY") or "").strip()
    return key or None


def is_enabled() -> bool:
    return get_api_key() is not None


def _extension_flag() -> str:
    return os.environ.get("NOPECHA_EXTENSION", "").strip().lower()


def use_chrome_extension() -> bool:
    """True when lab Chrome should wait for the in-page NopeCHA extension."""
    flag = _extension_flag()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return is_enabled()
    return bool(_extension_active) and is_enabled()


def extension_dir() -> Path:
    return config.APP_DIR / "nopecha-extension" / EXTENSION_VERSION


def extension_is_prepared() -> bool:
    marker = extension_dir() / ".ready"
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8").strip() == EXTENSION_VERSION
    except OSError:
        return False


def _find_unpacked_root(extract_dir: Path) -> Path:
    if (extract_dir / "manifest.json").exists():
        return extract_dir
    for child in sorted(extract_dir.iterdir()):
        if child.is_dir() and (child / "manifest.json").exists():
            return child
    raise FileNotFoundError(f"NopeCHA extension manifest not found in {extract_dir}")


def write_extension_api_key(unpacked_dir: Path, api_key: str) -> Path:
    """Write the subscription key into an automation-build manifest.json.

    The official graphical build has no `nopecha` block; that build is
    configured by visiting https://nopecha.com/setup#KEY after Chrome starts.
    """
    manifest_path = unpacked_dir / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if "nopecha" not in data:
        return manifest_path
    settings = dict(data.get("nopecha") or {})
    settings["key"] = api_key
    settings["enabled"] = True
    settings["hcaptcha_auto_open"] = True
    settings["hcaptcha_auto_solve"] = True
    settings["mouse_visualization"] = False
    data["nopecha"] = settings
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return manifest_path


def _download_extension(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / EXTENSION_ZIP_NAME
    logger.info("Downloading NopeCHA browser extension %s", EXTENSION_VERSION)
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        with client.stream("GET", EXTENSION_URL) as response:
            response.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
    extract_dir = dest_dir / "unpacked"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    root = _find_unpacked_root(extract_dir)
    (dest_dir / ".ready").write_text(EXTENSION_VERSION, encoding="utf-8")
    return root


def prepare_extension() -> Path | None:
    """Download/cache the automation build and write the API key into manifest.json."""
    key = get_api_key()
    if not key:
        return None
    dest_dir = extension_dir()
    try:
        if extension_is_prepared():
            root = _find_unpacked_root(dest_dir / "unpacked")
        else:
            root = _download_extension(dest_dir)
        write_extension_api_key(root, key)
        logger.info("NopeCHA Chrome extension files ready")
        return root
    except Exception:
        logger.exception("Could not prepare NopeCHA Chrome extension")
        return None


def mark_extension_running(running: bool) -> None:
    """Record whether the live Chrome actually attached the NopeCHA service worker."""
    global _extension_active
    _extension_active = bool(running)


def extension_service_worker_present(port: int) -> bool:
    """True when CDP lists the unpacked NopeCHA service worker on this Chrome."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            items = json.load(response)
    except Exception:
        return False
    if not isinstance(items, list):
        return False
    script = "4ncg2v.js"
    try:
        root = _find_unpacked_root(extension_dir() / "unpacked")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        script = Path(str((manifest.get("background") or {}).get("service_worker") or script)).name
    except Exception:
        pass
    needle = script.lower()
    for item in items:
        if str(item.get("type") or "") != "service_worker":
            continue
        blob = json.dumps(item).lower()
        if needle in blob or "nopecha" in blob:
            return True
    return False


def configure_loaded_extension(port: int) -> bool:
    """Verify the unpacked extension is actually running. Never logs keys or URLs."""
    running = extension_service_worker_present(port)
    mark_extension_running(running)
    if running:
        logger.info("NopeCHA extension service worker attached on port %d", port)
    else:
        logger.error(
            "NopeCHA extension did not attach a service worker on port %d "
            "(branded Chrome ignores --load-extension; lab needs Chrome for Testing)",
            port,
        )
    return running


def wait_for_extension_solve(port: int, timeout: float | None = None) -> bool:
    """Wait for the loaded extension to fill h-captcha-response in this Chrome."""
    timeout = timeout or max(float(config.DEFAULTS.get("nopecha_timeout", 90)), 120)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright is not available; cannot wait for NopeCHA extension")
        return False

    deadline = time.time() + timeout
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            seen_widget = False
            started = time.time()
            while time.time() <= deadline:
                longest = 0
                left_login = False
                for context in browser.contexts:
                    for page in context.pages:
                        url = (page.url or "").lower()
                        if url and "login" not in url and any(
                            token in url
                            for token in ("/candidate/", "/profile/", "preregister")
                        ):
                            left_login = True
                        targets = list(page.frames) + [page]
                        for target in targets:
                            try:
                                length = int(target.evaluate(RESPONSE_JS) or 0)
                            except Exception:
                                continue
                            if length > longest:
                                longest = length
                            if length > 0:
                                seen_widget = True
                if longest > 20:
                    logger.info("NopeCHA extension filled an hCaptcha response")
                    return True
                if left_login and seen_widget:
                    logger.info("NopeCHA extension wait: Chrome left the login wall")
                    return True
                if longest == 0:
                    _page, detected = _detect_hcaptcha_on_browser(browser)
                    if detected:
                        seen_widget = True
                        if int(detected.get("responseLength") or 0) > 20:
                            logger.info("NopeCHA extension filled an hCaptcha response")
                            return True
                if not seen_widget and time.time() > started + 20:
                    logger.info("NopeCHA extension saw no captcha widget on port %d", port)
                    return False
                time.sleep(2)
            if seen_widget:
                raise NopechaError("timeout", "Timed out waiting for NopeCHA extension")
            logger.info("NopeCHA extension saw no hCaptcha widget on port %d", port)
            return False
    except NopechaError:
        raise
    except Exception:
        logger.exception("NopeCHA extension wait failed")
        return False


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": "ApplyPilot/0.3.0",
    }


def _api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.post(url, json=payload, headers=_headers())
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise NopechaError("", "NopeCHA returned a non-JSON response")
    if not isinstance(data, dict):
        raise NopechaError("", "Unexpected NopeCHA response")
    if data.get("error"):
        raise NopechaError(str(data.get("error")), str(data.get("message") or "NopeCHA request failed"))
    return data


def _api_get(path: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(url, params=params, headers=_headers())
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise NopechaError("", "NopeCHA returned a non-JSON response")
    if not isinstance(data, dict):
        raise NopechaError("", "Unexpected NopeCHA response")
    return data


def get_status() -> dict[str, Any]:
    """Return subscription status. Does not include the API key."""
    key = get_api_key()
    if not key:
        raise NopechaError("", "NOPECHA_API_KEY is not set")
    data = _api_get(STATUS_PATH, {"key": key})
    if data.get("error"):
        raise NopechaError(str(data.get("error")), str(data.get("message") or "NopeCHA status failed"))
    return {
        "plan": data.get("plan"),
        "credit": data.get("credit"),
        "quota": data.get("quota"),
        "status": data.get("status") or "Active",
    }


def solve_hcaptcha(
    website_url: str,
    website_key: str,
    *,
    timeout: float | None = None,
    rqdata: str | None = None,
    useragent: str | None = None,
) -> str:
    """Create an hCaptcha token job and poll until ready. Returns the token string."""
    key = get_api_key()
    if not key:
        raise NopechaError("", "NOPECHA_API_KEY is not set")
    website_url = (website_url or "").strip()
    website_key = (website_key or "").strip()
    if not website_url or not website_key:
        raise NopechaError("", "hCaptcha sitekey or URL missing")

    payload: dict[str, Any] = {
        "key": key,
        "type": "hcaptcha",
        "sitekey": website_key,
        "url": website_url,
    }
    if rqdata:
        payload["data"] = {"rqdata": rqdata}
    if useragent:
        payload["useragent"] = useragent

    created = _api_post(TOKEN_PATH, payload)
    job_id = str(created.get("data") or created.get("id") or "")
    if not job_id:
        raise NopechaError("", "NopeCHA create did not return a job id")
    logger.info("NopeCHA hCaptcha job submitted prefix=%s", job_id[:8])

    deadline = time.time() + (timeout or config.DEFAULTS["nopecha_timeout"])
    interval = config.DEFAULTS["nopecha_poll_interval"]
    while time.time() < deadline:
        result = _api_get(TOKEN_PATH, {"key": key, "id": job_id})
        error = result.get("error")
        if error in {None, 0, "0"}:
            token = str(result.get("data") or result.get("token") or "")
            if token:
                logger.info("NopeCHA hCaptcha job completed")
                return token
        if error == INCOMPLETE_JOB or str(result.get("message") or "").lower() == "incomplete job":
            time.sleep(interval)
            continue
        if error:
            raise NopechaError(str(error), str(result.get("message") or "NopeCHA poll failed"))
        time.sleep(interval)
    raise NopechaError("timeout", f"Timed out waiting for NopeCHA hCaptcha job {job_id[:8]}")


def _detect_hcaptcha_on_browser(browser) -> tuple[Any, dict[str, Any]] | tuple[None, None]:
    from applypilot.apply.capsolver import DETECT_JS, merge_hcaptcha_detection

    pages = [page for context in browser.contexts for page in context.pages]
    fallback: tuple[Any, dict[str, Any]] | tuple[None, None] = (None, None)
    for page in pages:
        frames = list(page.frames) or [page.main_frame]
        detections: list[dict[str, Any] | None] = []
        for frame in frames:
            try:
                detections.append(frame.evaluate(DETECT_JS))
            except Exception:
                detections.append(None)
        merged = merge_hcaptcha_detection(
            detections, page_url=page.url, frame_urls=[frame.url for frame in frames],
        )
        if merged and merged.get("websiteKey"):
            return page, merged
        if merged:
            fallback = (page, merged)
    return fallback


def try_solve_on_cdp(port: int, timeout: float | None = None) -> bool:
    """Detect hCaptcha in the live Chrome worker and inject a NopeCHA token."""
    if not is_enabled():
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright is not available; cannot inject a NopeCHA token")
        return False

    from applypilot.apply.capsolver import INJECT_JS

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            deadline = time.time() + 20
            page = None
            detected: dict[str, Any] | None = None
            while time.time() <= deadline:
                page, detected = _detect_hcaptcha_on_browser(browser)
                if detected and detected.get("websiteKey"):
                    break
                logger.info(
                    "NopeCHA waiting for hCaptcha sitekey on port %d url=%s",
                    port,
                    str((detected or {}).get("websiteURL") or "")[:120],
                )
                time.sleep(1.5)
            if not detected or not page:
                logger.info("NopeCHA found no hCaptcha widget on port %d", port)
                return False
            website_url = str(detected.get("websiteURL") or page.url or "")
            website_key = str(detected.get("websiteKey") or "")
            rqdata = str(detected.get("rqdata") or "") or None
            if not website_key:
                logger.info("NopeCHA saw hCaptcha without a sitekey on %s", website_url[:120])
                return False
            try:
                useragent = page.evaluate("() => navigator.userAgent")
            except Exception:
                useragent = None
            logger.info(
                "NopeCHA solving hCaptcha on %s key_prefix=%s rqdata=%s",
                website_url[:120],
                website_key[:8],
                "yes" if rqdata else "no",
            )
            token = solve_hcaptcha(
                website_url,
                website_key,
                timeout=timeout,
                rqdata=rqdata,
                useragent=useragent if isinstance(useragent, str) else None,
            )
            if not token:
                return False
            frames = list(page.frames) or [page.main_frame]
            injected = False
            for target in (*frames, page):
                try:
                    target.evaluate(INJECT_JS, {"type": "hcaptcha", "token": token})
                    injected = True
                except Exception:
                    continue
            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass
            if injected:
                logger.info("NopeCHA injected an hCaptcha token")
            return injected
    except NopechaError as exc:
        if exc.code == "timeout":
            raise
        logger.exception("NopeCHA API could not solve hCaptcha")
        return False
    except Exception:
        logger.exception("NopeCHA CDP solve failed")
        return False
    return False
