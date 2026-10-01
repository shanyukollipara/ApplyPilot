"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
# Keep ApplyPilot isolated from the user's ticket watcher, which owns 9222.
BASE_CDP_PORT = 9322

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(worker_id: int) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    if (profile_dir / "Default").exists():
        return profile_dir  # Already initialized

    # Lab workers (id >= 40 / port 9362+) must not clone a live fleet
    # profile while those Chromes are running — that can corrupt cookie DBs.
    lab_min_worker = 40
    source: Path | None = None
    if worker_id < lab_min_worker:
        for wid in range(lab_min_worker):
            if wid == worker_id:
                continue
            candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
            if (candidate / "Default").exists():
                source = candidate
                break
    if source is None and worker_id < lab_min_worker:
        source = config.get_chrome_user_data()

    if source is None:
        logger.info("[worker-%d] Creating a fresh Chrome profile at %s", worker_id, profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        return profile_dir

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


def _seed_lab_session_cookies(dest_dir: Path, source_dir: Path) -> None:
    """Copy cookies/logins into a new lab profile without Preferences that block extensions."""
    dest_default = dest_dir / "Default"
    src_default = source_dir / "Default"
    if (dest_default / "Cookies").exists() or not src_default.exists():
        return
    dest_default.mkdir(parents=True, exist_ok=True)
    for name in ("Cookies", "Cookies-journal", "Login Data", "Login Data-journal"):
        src = src_default / name
        if not src.exists():
            continue
        try:
            shutil.copy2(src, dest_default / name)
        except OSError:
            pass


def _ensure_lab_extension_prefs(profile_dir: Path) -> None:
    """Mark this as a real headed profile with developer mode on."""
    prefs_file = profile_dir / "Default" / "Preferences"
    prefs: dict = {}
    if prefs_file.exists():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}
    prefs.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
    prefs.setdefault("profile", {})["exit_type"] = "Normal"
    prefs.setdefault("session", {})["restore_on_startup"] = 4
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

_campaign_watchdog_started = False
# Tiny chip in the top-right corner. A 1200x900 window gets clamped on-screen
# by macOS/Chrome and covers the desktop; a 32px window can sit on the edge.
CAMPAIGN_SLIVER_PX = 4
CAMPAIGN_WINDOW_W = 32
CAMPAIGN_WINDOW_H = 32
_sliver_origin: tuple[int, int] | None = None


def campaign_sliver_origin() -> tuple[int, int]:
    """Top-right corner of the main display — only a few pixels should show."""
    global _sliver_origin
    if _sliver_origin is not None:
        return _sliver_origin
    left, top = 1916, 0
    try:
        import ctypes
        import ctypes.util

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        class CGSize(ctypes.Structure):
            _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

        class CGRect(ctypes.Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]

        lib = ctypes.util.find_library("CoreGraphics") or (
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        cg = ctypes.CDLL(lib)
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGDisplayBounds.restype = CGRect
        rect = cg.CGDisplayBounds(cg.CGMainDisplayID())
        width = int(rect.size.width)
        left = max(0, int(rect.origin.x) + width - CAMPAIGN_SLIVER_PX)
        top = int(rect.origin.y)
    except Exception:
        logger.debug("Could not read display bounds for sliver origin", exc_info=True)
    _sliver_origin = (left, top)
    return _sliver_origin


def campaign_hidden_headed(worker_id: int, headless: bool) -> bool:
    """Campaign workers cannot use --headless=new; Indeed Cloudflare 403s it.

    Workers 50-89 keep branded headed Chrome as a tiny top-right corner
    sliver so Playwright raises do not cover the desktop.
    """
    return headless and 50 <= worker_id <= 89


def _campaign_demote_script(pids: list[int]) -> str:
    """AppleScript: pin campaign Chrome to a right-edge sliver. Do not minimize.

    Minimizing makes macOS restore a full window on screen the next time
    Playwright touches the page. A hanging-off-the-edge window stays a sliver.
    """
    ids = ", ".join(str(int(pid)) for pid in pids if pid and pid > 0)
    if not ids:
        return ""
    left, top = campaign_sliver_origin()
    return f"""
tell application "System Events"
  repeat with pidValue in {{{ids}}}
    try
      set pidNum to pidValue as integer
      repeat with procRef in (every process whose unix id is pidNum)
        try
          set miniaturized of every window of procRef to false
        end try
        try
          set zoomed of every window of procRef to false
        end try
        try
          set size of every window of procRef to {{{CAMPAIGN_WINDOW_W}, {CAMPAIGN_WINDOW_H}}}
        end try
        try
          set position of every window of procRef to {{{left}, {top}}}
        end try
        try
          set frontmost of procRef to false
        end try
      end repeat
    end try
  end repeat
end tell
"""


def _demote_campaign_chrome(pids: list[int]) -> None:
    """Pin campaign Chrome to a right-edge sliver. Never touch personal Chrome."""
    if platform.system() != "Darwin":
        return
    script = _campaign_demote_script(pids)
    if not script:
        return
    try:
        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        logger.debug("Could not demote campaign Chrome pids %s", pids, exc_info=True)


def _campaign_chrome_watchdog() -> None:
    """Keep campaign Chrome on the right-edge sliver when Playwright raises it."""
    while True:
        time.sleep(0.05)
        pids: list[int] = []
        with _chrome_lock:
            for wid, proc in _chrome_procs.items():
                if 50 <= wid <= 89 and proc is not None and proc.poll() is None:
                    pids.append(proc.pid)
        for i in range(0, len(pids), 6):
            _demote_campaign_chrome(pids[i : i + 6])


def _ensure_campaign_watchdog() -> None:
    global _campaign_watchdog_started
    with _chrome_lock:
        if _campaign_watchdog_started:
            return
        _campaign_watchdog_started = True
    threading.Thread(
        target=_campaign_chrome_watchdog,
        name="campaign-chrome-bg",
        daemon=True,
    ).start()


def _schedule_demote(pid: int) -> None:
    """Pin the window as soon as it exists, then a few more times."""
    def _run() -> None:
        for delay in (0.15, 0.4, 0.9, 1.8, 3.5):
            time.sleep(delay)
            _demote_campaign_chrome([pid])
    threading.Thread(target=_run, name=f"chrome-demote-{pid}", daemon=True).start()


def _wait_indeed_warmup(port: int, timeout: int = 25) -> bool:
    """Poll CDP tab titles until Indeed is past Cloudflare, or time out."""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                tabs = json.load(response)
            for tab in tabs:
                title = (tab.get("title") or "").lower()
                url = tab.get("url") or ""
                if "indeed.com" not in url:
                    continue
                blocked = any(
                    marker in title
                    for marker in ("security check", "just a moment", "blocked", "attention required")
                )
                if not blocked:
                    logger.info("[cdp-%d] Indeed warmup ok title=%s", port, tab.get("title"))
                    return True
        except Exception:
            pass
        time.sleep(1)
    logger.warning("[cdp-%d] Indeed warmup still on Cloudflare after %ss", port, timeout)
    return False


def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()
    using_cft = False

    disable_features = [
        "InfiniteSessionRestore",
        "PasswordManagerOnboarding",
    ]
    extension_dir = None
    extension_label = None
    # Indeed campaign workers 50-89 keep the copied UT login in headed
    # branded Chrome (primaries 50-59 + clones 60+). Chrome for Testing
    # cannot decrypt those cookies. --headless=new is Cloudflare-blocked.
    campaign_branded = 50 <= worker_id <= 89
    hidden_headed = campaign_hidden_headed(worker_id, headless)
    if hidden_headed:
        disable_features.extend([
            "CalculateNativeWinOcclusion",
            "IntensiveWakeUpThrottling",
        ])
    # Lab Chromes (worker 40-49) load NopeCHA in Chrome for Testing. Branded
    # Chrome 137+ silently ignores --load-extension, which is why the last
    # headed run timed out with an empty widget.
    if worker_id >= 40 and not headless and not campaign_branded:
        from applypilot.apply.nopecha import prepare_extension as prepare_nopecha
        extension_dir = prepare_nopecha()
        if extension_dir is not None:
            cft = config.get_chrome_for_testing_path()
            if cft:
                chrome_exe = cft
                using_cft = True
                disable_features.append("DisableLoadExtensionCommandLineSwitch")
                extension_label = "NopeCHA"
                # Persistent CFT profile. Do not reuse the branded Chrome 152
                # worker dir — those Preferences block unpacked extensions.
                legacy = profile_dir
                profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}-cft"
                profile_dir.mkdir(parents=True, exist_ok=True)
                (profile_dir / "Default").mkdir(exist_ok=True)
                _seed_lab_session_cookies(profile_dir, legacy)
                _ensure_lab_extension_prefs(profile_dir)
                _suppress_restore_nag(profile_dir)
            else:
                logger.error(
                    "[worker-%d] Chrome for Testing not found; NopeCHA extension cannot load "
                    "in branded Chrome. Install Playwright Chromium or set CHROME_FOR_TESTING_PATH",
                    worker_id,
                )
                extension_dir = None
    # CapSolver's Chrome extension can inject overlays that make Workday
    # controls appear "unstable" to Playwright clicks. Prefer the API/CDP
    # path unless CAPSOLVER_EXTENSION=1 is set explicitly.
    elif not headless and os.environ.get("CAPSOLVER_EXTENSION", "").strip() in {"1", "true", "yes"}:
        from applypilot.apply.capsolver import prepare_extension
        extension_dir = prepare_extension()
        if extension_dir is not None:
            disable_features.append("DisableLoadExtensionCommandLineSwitch")
            extension_label = "CapSolver"

    sliver_left, sliver_top = campaign_sliver_origin() if hidden_headed else (80, 40)
    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={CAMPAIGN_WINDOW_W},{CAMPAIGN_WINDOW_H}" if campaign_branded else "--window-size=1024,768",
        (
            f"--window-position={sliver_left},{sliver_top}"
            if hidden_headed
            else (
                f"--window-position={80 + ((worker_id - 50) % 20) * 36},{(48 if worker_id < 60 else 72)}"
                if campaign_branded
                else f"--window-position={80 * worker_id},{40 * (worker_id % 5)}"
            )
        ),
        "--disable-session-crashed-bubble",
        f"--disable-features={','.join(disable_features)}",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if not campaign_branded:
        cmd.append("--disable-popup-blocking")
    if worker_id >= 40 and extension_dir is not None:
        cmd.extend([
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ])
    elif campaign_branded:
        cmd.extend([
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ])
    if extension_dir is None:
        # Profile may still contain unpacked/marketplace extensions from prior
        # runs; force them off so overlays cannot block ATS clicks.
        cmd.append("--disable-extensions")
    if extension_dir is not None:
        from applypilot.apply.capsolver import extension_chrome_args
        cmd.extend(extension_chrome_args(extension_dir))
        logger.info("[worker-%d] %s extension loaded", worker_id, extension_label or "CAPTCHA")
    if hidden_headed:
        # Keep Chromium scheduled while the window is off-screen / occluded.
        cmd.extend([
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ])
    elif headless:
        cmd.append("--headless=new")
    if campaign_branded:
        # Load Indeed before any Playwright CDP client attaches so Cloudflare
        # sees a normal first paint instead of an automated navigate.
        cmd.append("https://www.indeed.com/")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc
    if hidden_headed:
        _ensure_campaign_watchdog()
        _schedule_demote(proc.pid)
        _demote_campaign_chrome([proc.pid])

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    if hidden_headed:
        _demote_campaign_chrome([proc.pid])
    if campaign_branded:
        _wait_indeed_warmup(port, timeout=25)
    if hidden_headed:
        _demote_campaign_chrome([proc.pid])
    if worker_id >= 40 and extension_dir is not None:
        from applypilot.apply.nopecha import configure_loaded_extension
        attached = configure_loaded_extension(port)
        if not attached:
            time.sleep(2)
            attached = configure_loaded_extension(port)
        if not attached:
            logger.error(
                "[worker-%d] NopeCHA extension flags were passed but the service worker never appeared",
                worker_id,
            )
    logger.info("[worker-%d] Chrome started on port %d (pid %d)%s",
                worker_id, port, proc.pid,
                " chrome-for-testing" if using_cft else (
                    " hidden-headed" if hidden_headed else ""
                ))
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def cleanup_browser_tabs(port: int, keep_pages: int = 1, *, park: bool = False) -> None:
    """Close extra tabs/windows in a worker browser without closing Chrome.

    Codex is instructed to close tabs, but ATS flows leave redirects behind.
    Campaign workers pass park=True so the leftover window stays a right-edge
    sliver and is reused for the next job.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=3000
            )
            pages = [page for context in browser.contexts for page in context.pages]
            for page in pages[keep_pages:]:
                try:
                    page.close(run_before_unload=False)
                except Exception:
                    logger.debug("Could not close extra worker tab", exc_info=True)
            kept = [page for context in browser.contexts for page in context.pages]
            if park and kept:
                _park_worker_window(kept[0])
                try:
                    url = (kept[0].url or "").lower()
                    if "indeed.com" not in url:
                        kept[0].goto("https://www.indeed.com/", timeout=8000, wait_until="domcontentloaded")
                except Exception:
                    logger.debug("Could not reset worker tab to Indeed", exc_info=True)
    except Exception:
        # Tab cleanup is defensive and must not change the application result.
        logger.debug("Worker tab cleanup unavailable on port %d", port, exc_info=True)


def _park_worker_window(page) -> None:
    """Pin the CDP window to the right-edge sliver (not minimized)."""
    try:
        left, top = campaign_sliver_origin()
        session = page.context.new_cdp_session(page)
        info = session.send("Browser.getWindowForTarget")
        window_id = info.get("windowId")
        if window_id is None:
            return
        session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "left": left,
                    "top": top,
                    "width": CAMPAIGN_WINDOW_W,
                    "height": CAMPAIGN_WINDOW_H,
                    "windowState": "normal",
                },
            },
        )
    except Exception:
        logger.debug("Could not park worker Chrome window", exc_info=True)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)
    # Never sweep BASE_CDP_PORT unless this process actually launched worker 0.
    # A lab/iCIMS process must not kill the live fleet on 9322.


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)
