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
from urllib.parse import parse_qs, unquote, urlparse

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
    "recaptcha_v2_enterprise": "ReCaptchaV2EnterpriseTaskProxyLess",
    "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
    "recaptcha_v3_enterprise": "ReCaptchaV3EnterpriseTaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
    "funcaptcha": "FunCaptchaTaskProxyLess",
}

UNSUPPORTED_CAPTCHA_TYPES = frozenset({"hcaptcha"})

DETECT_JS = """() => {
  const websiteURL = location.href;
  const iframeSrc = (needle) => {
    const iframe = document.querySelector('iframe[src*="' + needle + '"]');
    return iframe ? (iframe.getAttribute('src') || iframe.src || '') : '';
  };
  const paramFromSrc = (src, name) => {
    const text = src || '';
    try {
      const u = new URL(text, location.href);
      const fromSearch = u.searchParams.get(name);
      if (fromSearch) return fromSearch;
      const hash = (u.hash || '').replace(/^#/, '');
      const fromHash = new URLSearchParams(hash).get(name);
      if (fromHash) return fromHash;
    } catch (e) {}
    const re = new RegExp('[?#&]' + name + '=([^&]+)', 'i');
    const m = text.match(re);
    return m && m[1] ? decodeURIComponent(m[1]) : '';
  };
  const keyFromSrc = (src) => paramFromSrc(src, 'sitekey') || paramFromSrc(src, 'k');
  const allIframeSrcs = () => [...document.querySelectorAll('iframe')]
    .map((frame) => frame.getAttribute('src') || frame.src || '')
    .filter(Boolean);
  const recaptchaCfgKey = () => {
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
  };
  const recaptchaScriptKey = () => {
    const scripts = [...document.querySelectorAll('script[src*="recaptcha"]')];
    for (const s of scripts) {
      const m = (s.getAttribute('src') || '').match(/[?&]render=([^&]+)/i);
      if (m && m[1] && m[1] !== 'explicit') return decodeURIComponent(m[1]);
    }
    return '';
  };
  const payload = (type, websiteKey, frameURL, responseSelector) => {
    const el = responseSelector ? document.querySelector(responseSelector) : null;
    const value = el && el.value ? String(el.value) : '';
    return {
      type,
      websiteKey: websiteKey || '',
      websiteURL,
      frameURL: frameURL || '',
      responsePresent: !!el,
      responseLength: value.length,
    };
  };

  const hSrc = iframeSrc('hcaptcha.com') || allIframeSrcs().find((src) => /hcaptcha/i.test(src)) || '';
  const hasHcaptcha = !!(
    document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"], textarea[name="h-captcha-response"]')
    || /hcaptcha\\.com/i.test(location.hostname)
    || keyFromSrc(websiteURL)
  );
  if (hasHcaptcha) {
    const sitekeyNode = document.querySelector(
      '.h-captcha[data-sitekey], [data-hcaptcha-sitekey], iframe[data-sitekey], [data-sitekey]'
    );
    let websiteKey = sitekeyNode?.getAttribute('data-sitekey')
      || sitekeyNode?.getAttribute('data-hcaptcha-sitekey')
      || keyFromSrc(hSrc)
      || keyFromSrc(websiteURL);
    if (!websiteKey) {
      for (const src of allIframeSrcs()) {
        websiteKey = keyFromSrc(src);
        if (websiteKey) break;
      }
    }
    const detected = payload('hcaptcha', websiteKey, hSrc || websiteURL, 'textarea[name="h-captcha-response"]');
    const rqAttr = document.querySelector('[data-rqdata], [data-hcaptcha-payload]')?.getAttribute('data-rqdata')
      || document.querySelector('[data-hcaptcha-payload]')?.getAttribute('data-hcaptcha-payload')
      || '';
    let rqdata = rqAttr || paramFromSrc(hSrc, 'rqdata') || paramFromSrc(websiteURL, 'rqdata');
    if (!rqdata) {
      for (const src of allIframeSrcs()) {
        rqdata = paramFromSrc(src, 'rqdata');
        if (rqdata) break;
      }
    }
    detected.rqdata = rqdata || '';
    return detected;
  }

  const hasTurnstile = !!(
    document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]')
    || window.turnstile
  );
  if (hasTurnstile) {
    const tSrc = iframeSrc('challenges.cloudflare.com');
    const websiteKey = document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey')
      || keyFromSrc(tSrc);
    return payload('turnstile', websiteKey, tSrc, '[name="cf-turnstile-response"]');
  }

  const hasFun = !!(
    document.querySelector('#FunCaptcha, iframe[src*="funcaptcha"], iframe[src*="arkoselabs"]')
  );
  if (hasFun) {
    const fSrc = iframeSrc('funcaptcha') || iframeSrc('arkoselabs');
    return payload('funcaptcha', keyFromSrc(fSrc), fSrc, '');
  }

  const rSrcs = allIframeSrcs().filter((src) => /recaptcha/i.test(src));
  const rSrc = rSrcs[0] || iframeSrc('recaptcha');
  const hasRecaptcha = !!(
    document.querySelector('.g-recaptcha, iframe[src*="recaptcha"], textarea[name="g-recaptcha-response"]')
    || window.grecaptcha
  );
  if (hasRecaptcha) {
    const websiteKey = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey]')?.getAttribute('data-sitekey')
      || keyFromSrc(rSrc)
      || recaptchaCfgKey()
      || recaptchaScriptKey();
    const enterprise = rSrcs.some((src) => src.toLowerCase().includes('recaptcha/enterprise'))
      || (rSrc || '').toLowerCase().includes('recaptcha/enterprise')
      || !!(window.grecaptcha && window.grecaptcha.enterprise);
    const size = paramFromSrc(rSrc, 'size');
    const rType = paramFromSrc(rSrc, 'type');
    const isV2 = size === 'normal'
      || rType === 'image'
      || rSrcs.some((src) => src.includes('bframe') || src.includes('recaptcha/api2'))
      || !!document.querySelector('.g-recaptcha, iframe[src*="recaptcha/api2"]');
    let type;
    if (isV2) type = enterprise ? 'recaptcha_v2_enterprise' : 'recaptcha_v2';
    else type = enterprise ? 'recaptcha_v3_enterprise' : 'recaptcha_v3';
    // Indeed Easy Apply uses the v2 enterprise checkbox. A missing bframe is not v3.
    if (/indeed\.com/i.test(websiteURL) || /indeed\.com/i.test(location.hostname)) {
      type = enterprise ? 'recaptcha_v2_enterprise' : 'recaptcha_v2';
    }
    return payload(type, websiteKey, rSrc, 'textarea[name="g-recaptcha-response"]');
  }
  return null;
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
  if (type === 'recaptcha_v2' || type === 'recaptcha_v2_enterprise' || type === 'recaptcha_v3' || type === 'recaptcha_v3_enterprise') {
    document.querySelectorAll('#g-recaptcha-response, textarea[name="g-recaptcha-response"]')
      .forEach((el) => setValue(el, token));
    try {
      if (window.grecaptcha) {
        if (grecaptcha.enterprise) grecaptcha.enterprise.getResponse = () => token;
        grecaptcha.getResponse = () => token;
      }
    } catch (e) {}
    const walk = (obj, depth) => {
      if (!obj || depth > 8) return;
      if (typeof obj.callback === 'function') {
        try { obj.callback(token); } catch (e) {}
      }
      if (typeof obj === 'object') {
        try { Object.values(obj).forEach((v) => walk(v, depth + 1)); } catch (e) {}
      }
    };
    try {
      if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
        walk(window.___grecaptcha_cfg.clients, 0);
      }
    } catch (e) {}
    document.querySelectorAll('iframe[src*="bframe"]').forEach((f) => {
      f.style.display = 'none';
      if (f.parentElement) f.parentElement.style.display = 'none';
    });
  } else if (type === 'hcaptcha') {
    const hasWidget = !!(
      document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"], textarea[name="h-captcha-response"], [data-hcaptcha-widget-id]')
    );
    if (!hasWidget) {
      return false;
    }
    let areas = document.querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]');
    if (!areas.length) {
      const forms = [...document.querySelectorAll('form')];
      const form = forms.find((node) => node.querySelector('.h-captcha, iframe[src*="hcaptcha.com"]'))
        || forms[0];
      if (form) {
        const ta = document.createElement('textarea');
        ta.name = 'h-captcha-response';
        ta.setAttribute('name', 'h-captcha-response');
        ta.setAttribute('aria-hidden', 'true');
        ta.style.display = 'none';
        form.appendChild(ta);
        areas = document.querySelectorAll('textarea[name="h-captcha-response"]');
      }
    }
    areas.forEach((el) => setValue(el, token));
    const widget = document.querySelector('.h-captcha, [data-hcaptcha-widget-id], [data-callback]');
    const callbackName = (widget && widget.getAttribute('data-callback'))
      || document.querySelector('[data-callback]')?.getAttribute('data-callback');
    try {
      if (callbackName && typeof window[callbackName] === 'function') {
        window[callbackName](token);
      }
    } catch (e) {}
    try {
      if (window.hcaptcha && typeof window.hcaptcha.getResponse === 'function') {
        window.hcaptcha.getResponse = function() { return token; };
      }
    } catch (e) {}
  } else if (type === 'turnstile') {
    document.querySelectorAll('[name="cf-turnstile-response"], [name="g-recaptcha-response"]')
      .forEach((el) => setValue(el, token));
  }
  return true;
}"""


def hcaptcha_params_from_url(url: str) -> dict[str, str]:
    """Pull sitekey/rqdata out of an hCaptcha iframe URL, including hash params."""
    parsed = urlparse(url or "")
    found: dict[str, str] = {}
    for blob in (parsed.query, parsed.fragment):
        qs = parse_qs(blob, keep_blank_values=False)
        if qs.get("sitekey") and "sitekey" not in found:
            found["sitekey"] = unquote(qs["sitekey"][0])
        elif qs.get("k") and "sitekey" not in found:
            found["sitekey"] = unquote(qs["k"][0])
        if qs.get("rqdata") and "rqdata" not in found:
            found["rqdata"] = unquote(qs["rqdata"][0])
    return found


def host_is_hcaptcha(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == "hcaptcha.com" or host.endswith(".hcaptcha.com")


def merge_hcaptcha_detection(
    detections: list[dict[str, Any] | None],
    *,
    page_url: str,
    frame_urls: list[str],
) -> dict[str, Any] | None:
    """Combine DOM detections with Playwright frame URLs so hash sitekeys are not missed."""
    merged: dict[str, Any] = {}
    for detected in detections:
        if not detected:
            continue
        kind = str(detected.get("type") or "")
        if kind and kind != "hcaptcha":
            continue
        if kind:
            merged["type"] = "hcaptcha"
        for key in ("websiteKey", "websiteURL", "frameURL", "rqdata", "responsePresent", "responseLength"):
            value = detected.get(key)
            if value not in (None, ""):
                merged.setdefault(key, value)
    for url in [*(frame_urls or []), page_url]:
        extra = hcaptcha_params_from_url(str(url or ""))
        if extra.get("sitekey"):
            merged["type"] = "hcaptcha"
            merged.setdefault("websiteKey", extra["sitekey"])
        if extra.get("rqdata"):
            merged.setdefault("rqdata", extra["rqdata"])
        if host_is_hcaptcha(str(url or "")):
            merged["type"] = "hcaptcha"
            merged.setdefault("frameURL", url)
    if merged.get("type") != "hcaptcha" and any("hcaptcha" in str(url or "").lower() for url in frame_urls):
        merged["type"] = "hcaptcha"
    if merged.get("type") != "hcaptcha":
        return None
    web = str(merged.get("websiteURL") or "")
    if (not web) or host_is_hcaptcha(web):
        if page_url and not host_is_hcaptcha(page_url):
            merged["websiteURL"] = page_url
    return merged


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
    kind = str(detected.get("type") or "")
    website_url = (detected.get("websiteURL") or "").strip()
    # Indeed Easy Apply checkbox is v2 enterprise. Coerce even if URL missing.
    if kind in {"recaptcha_v3", "recaptcha_v3_enterprise"} and (
        "indeed.com" in website_url.lower() or not website_url
    ):
        kind = "recaptcha_v2_enterprise"
        detected["type"] = kind
    if kind in UNSUPPORTED_CAPTCHA_TYPES:
        return None
    task_type = TASK_TYPES.get(kind)
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
    if kind in {"recaptcha_v3", "recaptcha_v3_enterprise"}:
        page_action = (detected.get("pageAction") or "").strip() or "submit"
        task["pageAction"] = page_action
    if kind == "recaptcha_v3_enterprise" and detected.get("enterprisePayload"):
        task["enterprisePayload"] = detected["enterprisePayload"]
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


def inspect_live_captcha(port: int) -> dict[str, Any] | None:
    """Return the first CAPTCHA detection from a live Chrome worker, if any."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages = [page for context in browser.contexts for page in context.pages]
            fallback = None
            for page in pages:
                frames = list(page.frames) or [page.main_frame]
                frame_urls = [frame.url for frame in frames]
                detections: list[dict[str, Any] | None] = []
                for frame in frames:
                    try:
                        detections.append(frame.evaluate(DETECT_JS))
                    except Exception:
                        detections.append(None)
                for detected in detections:
                    if not detected:
                        continue
                    kind = str(detected.get("type") or "")
                    if kind and kind != "hcaptcha" and detected.get("websiteKey"):
                        logger.info(
                            "CAPTCHA detected type=%s key_prefix=%s response_len=%s url=%s frame=%s",
                            detected.get("type"),
                            str(detected.get("websiteKey") or "")[:8],
                            detected.get("responseLength"),
                            str(detected.get("websiteURL") or "")[:120],
                            str(detected.get("frameURL") or "")[:80],
                        )
                        return detected
                merged = merge_hcaptcha_detection(
                    detections, page_url=page.url, frame_urls=frame_urls,
                )
                if merged:
                    logger.info(
                        "CAPTCHA detected type=%s key_prefix=%s response_len=%s url=%s frame=%s",
                        merged.get("type"),
                        str(merged.get("websiteKey") or "")[:8],
                        merged.get("responseLength"),
                        str(merged.get("websiteURL") or "")[:120],
                        str(merged.get("frameURL") or "")[:80],
                    )
                    if merged.get("websiteKey"):
                        return merged
                    fallback = fallback or merged
                for detected in detections:
                    if detected:
                        fallback = fallback or detected
            if fallback:
                return fallback
            logger.info("CAPTCHA inspect found no widget on port %d pages=%d", port, len(pages))
    except Exception:
        logger.warning("CAPTCHA inspect failed on port %d", port, exc_info=True)
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
                kind = str(detected.get("type") or "")
                if kind in UNSUPPORTED_CAPTCHA_TYPES:
                    logger.warning(
                        "CapSolver does not support %s on %s",
                        kind,
                        str(detected.get("websiteURL") or "")[:120],
                    )
                    return False
                task = task_from_detection(detected)
                if not task:
                    logger.info("CapSolver detected %s but is missing a site key", detected.get("type"))
                    continue
                logger.info(
                    "CapSolver solving %s on %s",
                    task.get("type"),
                    (detected.get("websiteURL") or "")[:120],
                )
                try:
                    solution = solve(task)
                except CapSolverError as exc:
                    if "don't support this service" in str(exc).lower():
                        logger.warning(
                            "CapSolver does not support %s on %s",
                            detected.get("type"),
                            (detected.get("websiteURL") or "")[:120],
                        )
                        continue
                    raise
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
