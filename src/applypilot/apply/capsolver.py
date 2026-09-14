"""CapSolver API client and Chrome extension helper for auto-apply CAPTCHAs."""

from __future__ import annotations

import logging
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from applypilot import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.capsolver.com"
EXTENSION_VERSION = "v1.17.1"
EXTENSION_ZIP_NAME = "CapSolver.Browser.Extension-chrome-v1.7.1.zip"
EXTENSION_URL = (
    "https://github.com/capsolver/capsolver-browser-extension/releases/"
    f"download/{EXTENSION_VERSION}/{EXTENSION_ZIP_NAME}"
)

TASK_TYPES = {
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "funcaptcha": "FunCaptchaTaskProxyLess",
}

DETECT_JS = """() => {
  const websiteURL = location.href;
  const iframeKey = (() => {
    const iframes = [...document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"]')];
    for (const iframe of iframes) {
      try {
        const src = iframe.getAttribute('src') || '';
        const m = src.match(/[?&](?:k|sitekey)=([^&]+)/i);
        if (m && m[1]) return decodeURIComponent(m[1]);
      } catch (e) {}
    }
    return '';
  })();
  const cfgKey = (() => {
    try {
      const cfg = window.___grecaptcha_cfg;
      const clients = cfg && cfg.clients ? Object.values(cfg.clients) : [];
      for (const client of clients) {
        const stack = [client];
        while (stack.length) {
          const cur = stack.pop();
          if (!cur || typeof cur !== 'object') continue;
          for (const [k, v] of Object.entries(cur)) {
            if ((k === 'sitekey' || k === 'siteKey') && typeof v === 'string' && v.length > 10) return v;
            if (v && typeof v === 'object') stack.push(v);
          }
        }
      }
    } catch (e) {}
    return '';
  })();
  const scriptKey = (() => {
    const scripts = [...document.querySelectorAll('script[src*="recaptcha"]')];
    for (const s of scripts) {
      const m = (s.getAttribute('src') || '').match(/[?&]render=([^&]+)/i);
      if (m && m[1] && m[1] !== 'explicit') return decodeURIComponent(m[1]);
    }
    return '';
  })();
  const sitekey = document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey')
    || document.querySelector('[data-hcaptcha-sitekey]')?.getAttribute('data-hcaptcha-sitekey')
    || iframeKey
    || cfgKey
    || scriptKey
    || '';
  const hasTurnstile = !!(
    document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]')
    || window.turnstile
  );
  const hasHcaptcha = !!(
    document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"], textarea[name="h-captcha-response"]')
  );
  const hasFun = !!(
    document.querySelector('#FunCaptcha, iframe[src*="funcaptcha"], iframe[src*="arkoselabs"]')
  );
  const hasRecaptcha = !!(
    document.querySelector('.g-recaptcha, iframe[src*="recaptcha"], textarea[name="g-recaptcha-response"]')
    || window.grecaptcha
  );
  let type = '';
  if (hasTurnstile) type = 'turnstile';
  else if (hasHcaptcha) type = 'hcaptcha';
  else if (hasFun) type = 'funcaptcha';
  else if (hasRecaptcha) {
    const checkbox = document.querySelector('.g-recaptcha, iframe[src*="recaptcha/api2"]');
    type = checkbox ? 'recaptcha_v2' : 'recaptcha_v3';
  }
  if (!type) return null;
  return { type, websiteKey: sitekey, websiteURL };
}"""

INJECT_JS = """({ type, token }) => {
  const setValue = (el, value) => {
    if (!el) return;
    el.style.display = 'block';
    el.value = value;
    el.innerHTML = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  if (type === 'recaptcha_v2' || type === 'recaptcha_v3') {
    document.querySelectorAll('#g-recaptcha-response, textarea[name="g-recaptcha-response"]')
      .forEach((el) => setValue(el, token));
  } else if (type === 'hcaptcha') {
    document.querySelectorAll('textarea[name="h-captcha-response"], [name="g-recaptcha-response"]')
      .forEach((el) => setValue(el, token));
  } else if (type === 'turnstile') {
    document.querySelectorAll('[name="cf-turnstile-response"], [name="g-recaptcha-response"]')
      .forEach((el) => setValue(el, token));
  }
  return true;
}"""


class CapSolverError(Exception):
    """Raised when the CapSolver API returns an error."""

    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(description if not code else f"{code}: {description}")


def get_api_key() -> str | None:
    """Return the configured CapSolver key, or None if unset."""
    key = (os.environ.get("CAPSOLVER_API_KEY") or "").strip()
    return key or None


def is_enabled() -> bool:
    """True when a CapSolver API key is available."""
    return get_api_key() is not None


def extension_dir() -> Path:
    """On-disk cache for the unpacked CapSolver Chrome extension."""
    return config.APP_DIR / "capsolver-extension" / EXTENSION_VERSION


def extension_chrome_args(unpacked_dir: Path) -> list[str]:
    """Chrome flags that load the unpacked CapSolver extension."""
    path = str(unpacked_dir)
    return [
        f"--load-extension={path}",
        f"--disable-extensions-except={path}",
    ]


def task_from_detection(detected: dict[str, Any]) -> dict[str, Any] | None:
    """Map a page detection result to a CapSolver createTask payload."""
    kind = detected.get("type")
    task_type = TASK_TYPES.get(str(kind or ""))
    website_url = (detected.get("websiteURL") or "").strip()
    website_key = (detected.get("websiteKey") or "").strip()
    if not task_type or not website_url or not website_key:
        return None
    task: dict[str, Any] = {
        "type": task_type,
        "websiteURL": website_url,
    }
    if kind == "funcaptcha":
        task["websitePublicKey"] = website_key
    else:
        task["websiteKey"] = website_key
    if kind == "recaptcha_v3":
        page_action = (detected.get("pageAction") or "").strip()
        if page_action:
            task["pageAction"] = page_action
    return task


def token_from_solution(solution: dict[str, Any]) -> str:
    """Extract the token string from a CapSolver solution object."""
    return str(
        solution.get("gRecaptchaResponse")
        or solution.get("token")
        or solution.get("text")
        or ""
    )


def _api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to a CapSolver API path and return the parsed object."""
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ApplyPilot/0.3.0",
            },
        )
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise CapSolverError("", "CapSolver returned a non-JSON response")
        if response.is_error:
            if isinstance(data, dict):
                raise CapSolverError(
                    str(data.get("errorCode") or response.status_code),
                    str(data.get("errorDescription") or data.get("message")
                        or f"CapSolver HTTP {response.status_code}"),
                )
            response.raise_for_status()
    if not isinstance(data, dict):
        raise CapSolverError("", "Unexpected CapSolver response")
    if data.get("errorId"):
        raise CapSolverError(
            str(data.get("errorCode") or ""),
            str(data.get("errorDescription") or "CapSolver request failed"),
        )
    return data


def get_balance() -> float:
    """Return the CapSolver account balance in USD."""
    key = get_api_key()
    if not key:
        raise CapSolverError("", "CAPSOLVER_API_KEY is not set")
    data = _api_post("/getBalance", {"clientKey": key})
    return float(data.get("balance") or 0)


def solve(task: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    """Create a CapSolver task and poll until the solution is ready."""
    key = get_api_key()
    if not key:
        raise CapSolverError("", "CAPSOLVER_API_KEY is not set")

    created = _api_post("/createTask", {"clientKey": key, "task": task})
    if created.get("status") == "ready" and created.get("solution"):
        return created["solution"]

    task_id = created.get("taskId")
    if not task_id:
        raise CapSolverError("", "CapSolver createTask did not return a taskId")

    deadline = time.time() + (timeout or config.DEFAULTS["capsolver_timeout"])
    interval = config.DEFAULTS["capsolver_poll_interval"]
    while time.time() < deadline:
        result = _api_post("/getTaskResult", {"clientKey": key, "taskId": task_id})
        status = result.get("status")
        if status == "ready" and result.get("solution"):
            return result["solution"]
        if status in {"idle", "processing", None}:
            time.sleep(interval)
            continue
        raise CapSolverError(str(result.get("errorCode") or ""), f"Unexpected status {status}")
    raise CapSolverError("", f"Timed out waiting for CapSolver task {task_id}")


def write_extension_api_key(unpacked_dir: Path, api_key: str) -> Path:
    """Write the API key into the official extension config.js."""
    config_js = unpacked_dir / "assets" / "config.js"
    if not config_js.exists():
        config_js.parent.mkdir(parents=True, exist_ok=True)
        config_js.write_text(
            "export const defaultConfig = {\n"
            f"  apiKey: '{api_key}',\n"
            "  useCapsolver: true,\n"
            "  manualSolving: false,\n"
            "  enabledForRecaptcha: true,\n"
            "  enabledForRecaptchaV3: true,\n"
            "  enabledForHCaptcha: true,\n"
            "  enabledForFunCaptcha: true,\n"
            "  enabledForImageToText: true,\n"
            "  enabledForAwsCaptcha: true,\n"
            "};\n",
            encoding="utf-8",
        )
        return config_js

    text = config_js.read_text(encoding="utf-8")
    if re.search(r"apiKey:\s*['\"][^'\"]*['\"]", text):
        text = re.sub(r"apiKey:\s*['\"][^'\"]*['\"]", f"apiKey: '{api_key}'", text, count=1)
    else:
        text = text.replace("{", "{\n  apiKey: '" + api_key + "',", 1)
    text = re.sub(r"useCapsolver:\s*false", "useCapsolver: true", text)
    config_js.write_text(text, encoding="utf-8")
    return config_js


def _find_unpacked_root(extract_dir: Path) -> Path:
    if (extract_dir / "manifest.json").exists():
        return extract_dir
    for child in sorted(extract_dir.iterdir()):
        if child.is_dir() and (child / "manifest.json").exists():
            return child
    raise FileNotFoundError(f"CapSolver extension manifest not found in {extract_dir}")


def _download_extension(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / EXTENSION_ZIP_NAME
    logger.info("Downloading CapSolver browser extension %s", EXTENSION_VERSION)
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        with client.stream("GET", EXTENSION_URL) as response:
            response.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
    extract_dir = dest_dir / "unpacked"
    if extract_dir.exists():
        import shutil
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    root = _find_unpacked_root(extract_dir)
    (dest_dir / ".ready").write_text(EXTENSION_VERSION, encoding="utf-8")
    return root


def prepare_extension() -> Path | None:
    """Download/cache the CapSolver extension and write the API key into it."""
    key = get_api_key()
    if not key:
        return None
    dest_dir = extension_dir()
    ready_marker = dest_dir / ".ready"
    try:
        if ready_marker.exists() and ready_marker.read_text(encoding="utf-8").strip() == EXTENSION_VERSION:
            root = _find_unpacked_root(dest_dir / "unpacked")
        else:
            root = _download_extension(dest_dir)
        write_extension_api_key(root, key)
        return root
    except Exception:
        logger.exception("Could not prepare CapSolver Chrome extension")
        return None


def try_solve_on_cdp(port: int) -> bool:
    """Detect a CAPTCHA in the live Chrome worker and inject a CapSolver token."""
    if not is_enabled():
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright is not available; cannot inject a CapSolver token")
        return False

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages = [page for context in browser.contexts for page in context.pages]
            for page in pages:
                detected = page.evaluate(DETECT_JS)
                if not detected:
                    continue
                task = task_from_detection(detected)
                if not task:
                    logger.info("CapSolver detected %s but is missing a site key", detected.get("type"))
                    continue
                solution = solve(task)
                token = token_from_solution(solution)
                if not token:
                    continue
                page.evaluate(INJECT_JS, {"type": detected["type"], "token": token})
                logger.info("CapSolver injected a %s token", detected["type"])
                return True
    except CapSolverError:
        logger.exception("CapSolver API could not solve the CAPTCHA")
        return False
    except Exception:
        logger.exception("CapSolver CDP solve failed")
        return False
    return False
