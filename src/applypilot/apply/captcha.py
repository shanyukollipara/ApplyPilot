"""CAPTCHA handoff: Luna pauses, a provider attempts on the same Chrome session.

hCaptcha is routed to NopeCHA. reCAPTCHA, Turnstile, and FunCaptcha stay on CapSolver.
Luna itself never clicks or reasons through a challenge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from applypilot.apply import capsolver, nopecha

logger = logging.getLogger(__name__)

PROVIDER_CAPSOLVER = "capsolver"
PROVIDER_NOPECHA = "nopecha"
PROVIDER_NONE = "none"

CHECKPOINT_NOT_LOADED = "captcha_provider_not_loaded"
CHECKPOINT_UNSUPPORTED = "captcha_provider_unsupported"
CHECKPOINT_TIMEOUT = "captcha_provider_timeout"
CHECKPOINT_ATTEMPT_COMPLETED = "captcha_attempt_completed"
CHECKPOINT_LOGIN_REJECTED = "captcha_completed_but_login_rejected"
CHECKPOINT_LOGIN_ACCEPTED = "captcha_login_accepted"
CHECKPOINT_PROFILE = "captcha_profile_checkpoint"
CHECKPOINT_SUBMIT = "captcha_submit_checkpoint"
CHECKPOINT_CONFIRMED = "application_confirmed"
CHECKPOINT_UNKNOWN = "application_outcome_unknown"

HCAPTCHA_TYPES = frozenset({"hcaptcha"})
CAPSOLVER_TYPES = frozenset({"recaptcha_v2", "recaptcha_v2_enterprise", "recaptcha_v3", "recaptcha_v3_enterprise", "turnstile", "funcaptcha"})


@dataclass
class CaptchaResolution:
    """Outcome of one provider attempt. Unpackable as (solved, unsupported)."""

    solved: bool
    unsupported: str | None = None
    provider: str = PROVIDER_NONE
    checkpoint: str = CHECKPOINT_NOT_LOADED
    captcha_type: str | None = None
    error: str | None = None
    elapsed_ms: int = 0

    def __iter__(self):
        yield self.solved
        yield self.unsupported

    def __eq__(self, other):
        if isinstance(other, CaptchaResolution):
            return (self.solved, self.unsupported) == (other.solved, other.unsupported)
        if isinstance(other, tuple):
            return (self.solved, self.unsupported) == other
        return NotImplemented


def provider_for(captcha_type: str | None) -> str:
    kind = str(captcha_type or "")
    if kind in HCAPTCHA_TYPES:
        return PROVIDER_NOPECHA if nopecha.is_enabled() else PROVIDER_NONE
    if kind in CAPSOLVER_TYPES:
        return PROVIDER_CAPSOLVER if capsolver.is_enabled() else PROVIDER_NONE
    return PROVIDER_NONE


def provider_config_id() -> str:
    """Fingerprint of the current solver configuration for host-policy retries."""
    parts = []
    if nopecha.use_chrome_extension():
        parts.append("nopecha:extension")
    elif nopecha.is_enabled():
        parts.append("nopecha:hcaptcha")
    else:
        parts.append("nopecha:off")
    if capsolver.is_enabled():
        parts.append("capsolver:other")
    else:
        parts.append("capsolver:off")
    return "+".join(parts)


def inspect_live_captcha(port: int) -> dict[str, Any] | None:
    return capsolver.inspect_live_captcha(port)


def resolve_live_captcha(
    port: int,
    *,
    worker_id: int = 0,
    allow_manual_wait: bool = False,
    wait_fn: Callable[[], bool] | None = None,
    add_event: Callable[[str], None] | None = None,
    update_state: Callable[..., None] | None = None,
) -> CaptchaResolution:
    """Attempt the configured provider on the already-open Chrome session."""
    started = time.time()

    def _event(message: str) -> None:
        if add_event:
            add_event(message)

    def _state(**kwargs: Any) -> None:
        if update_state:
            update_state(worker_id, **kwargs)

    def _finish(**kwargs: Any) -> CaptchaResolution:
        kwargs.setdefault("elapsed_ms", int((time.time() - started) * 1000))
        return CaptchaResolution(**kwargs)

    detected = inspect_live_captcha(port)
    if detected:
        kind0 = str(detected.get("type") or "")
        web = str(detected.get("websiteURL") or "").lower()
        # Indeed Easy Apply uses the v2 enterprise checkbox, not v3.
        if kind0 in {"recaptcha_v3", "recaptcha_v3_enterprise"} and (
            "indeed.com" in web or web == ""
        ):
            detected["type"] = "recaptcha_v2_enterprise"
    kind = str((detected or {}).get("type") or "") or None
    # Luna already emitted RESULT:CAPTCHA. iCIMS challenges are hCaptcha and are
    # often inside iframes, so try NopeCHA even when the top-level snapshot missed it.
    if not kind and nopecha.is_enabled() and worker_id >= 40:
        kind = "hcaptcha"
    # Regular 16-pack workers never call NopeCHA, even if a key is loaded.
    if kind in HCAPTCHA_TYPES and worker_id < 40:
        _event(f"[W{worker_id}] hCaptcha present; regular workers do not use NopeCHA")
        return _finish(
            solved=False,
            unsupported=kind,
            provider=PROVIDER_NONE,
            checkpoint=CHECKPOINT_NOT_LOADED,
            captcha_type=kind,
            error="nopecha_fleet_skipped",
        )
    provider = provider_for(kind)

    if kind in HCAPTCHA_TYPES and provider != PROVIDER_NOPECHA:
        _event(f"[W{worker_id}] hCaptcha present but NopeCHA is not loaded")
        return _finish(
            solved=False,
            unsupported=kind,
            provider=PROVIDER_NONE,
            checkpoint=CHECKPOINT_NOT_LOADED,
            captcha_type=kind,
            error="nopecha_not_configured",
        )

    if kind in HCAPTCHA_TYPES:
        _state(status="captcha", last_action="NopeCHA solving hCaptcha")
        using_extension = nopecha.use_chrome_extension()
        _event(
            f"[W{worker_id}] NopeCHA {'extension waiting in-page' if using_extension else 'attempting hCaptcha'}"
        )
        try:
            solved = (
                nopecha.wait_for_extension_solve(port)
                if using_extension
                else nopecha.try_solve_on_cdp(port)
            )
            if solved:
                _event(f"[W{worker_id}] NopeCHA completed an hCaptcha attempt")
                return _finish(
                    solved=True,
                    provider=PROVIDER_NOPECHA,
                    checkpoint=CHECKPOINT_ATTEMPT_COMPLETED,
                    captcha_type=kind,
                )
        except nopecha.NopechaError as exc:
            if exc.code == "timeout":
                _event(f"[W{worker_id}] NopeCHA timed out")
                return _finish(
                    solved=False,
                    provider=PROVIDER_NOPECHA,
                    checkpoint=CHECKPOINT_TIMEOUT,
                    captcha_type=kind,
                    error="timeout",
                )
            logger.exception("NopeCHA auto-solve failed")
            return _finish(
                solved=False,
                provider=PROVIDER_NOPECHA,
                checkpoint=CHECKPOINT_UNSUPPORTED if "support" in str(exc).lower() else CHECKPOINT_NOT_LOADED,
                captcha_type=kind,
                error=str(exc.code or "nopecha_error"),
            )
        except Exception:
            logger.exception("NopeCHA auto-solve failed")
        _event(f"[W{worker_id}] NopeCHA could not auto-solve hCaptcha")
        return _finish(
            solved=False,
            provider=PROVIDER_NOPECHA,
            checkpoint=CHECKPOINT_UNSUPPORTED,
            captcha_type=kind,
            error="nopecha_unsolved",
        )

    if capsolver.is_enabled():
        _state(status="captcha", last_action="CapSolver solving")
        _event(f"[W{worker_id}] CapSolver attempting to solve CAPTCHA")
        try:
            if capsolver.try_solve_on_cdp(port):
                _event(f"[W{worker_id}] CapSolver solved CAPTCHA")
                return _finish(
                    solved=True,
                    provider=PROVIDER_CAPSOLVER,
                    checkpoint=CHECKPOINT_ATTEMPT_COMPLETED,
                    captcha_type=kind,
                )
        except Exception:
            logger.exception("CapSolver auto-solve failed")
        if not allow_manual_wait or wait_fn is None:
            _event(f"[W{worker_id}] CapSolver could not auto-solve; not waiting")
            return _finish(
                solved=False,
                provider=PROVIDER_CAPSOLVER,
                checkpoint=CHECKPOINT_TIMEOUT if kind else CHECKPOINT_NOT_LOADED,
                captcha_type=kind,
                error="capsolver_unsolved",
            )
        _event(f"[W{worker_id}] CapSolver could not auto-solve; waiting for extension/user")
        solved = bool(wait_fn())
        return _finish(
            solved=solved,
            provider=PROVIDER_CAPSOLVER,
            checkpoint=CHECKPOINT_ATTEMPT_COMPLETED if solved else CHECKPOINT_TIMEOUT,
            captcha_type=kind,
        )

    if not allow_manual_wait or wait_fn is None:
        _event(f"[W{worker_id}] CAPTCHA cannot be solved in unattended mode")
        return _finish(
            solved=False,
            unsupported=kind,
            provider=PROVIDER_NONE,
            checkpoint=CHECKPOINT_NOT_LOADED,
            captcha_type=kind,
            error="no_provider",
        )
    solved = bool(wait_fn())
    return _finish(
        solved=solved,
        provider=PROVIDER_NONE,
        checkpoint=CHECKPOINT_ATTEMPT_COMPLETED if solved else CHECKPOINT_TIMEOUT,
        captcha_type=kind,
    )


def infer_funnel_checkpoint(result: str, signals: dict[str, Any] | None, *, provider_solved: bool) -> str:
    """Map a Luna result + live page signals onto a research checkpoint."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    url = str((signals or {}).get("url") or "").lower()
    if result == "applied" or reason == "applied":
        return CHECKPOINT_CONFIRMED
    if result == "login_issue" or reason == "login_issue":
        return CHECKPOINT_LOGIN_REJECTED if provider_solved else "login_issue"
    if reason in {CHECKPOINT_NOT_LOADED, CHECKPOINT_UNSUPPORTED, CHECKPOINT_TIMEOUT, CHECKPOINT_ATTEMPT_COMPLETED}:
        return reason
    if reason in {CHECKPOINT_UNKNOWN, "no_result_line"}:
        return CHECKPOINT_UNKNOWN
    if signals:
        if signals.get("authenticated") and not signals.get("passwordLogin"):
            if any(token in url for token in ("submit", "review", "confirm")):
                return CHECKPOINT_SUBMIT
            if any(token in url for token in ("profile", "application", "apply")):
                return CHECKPOINT_PROFILE
            return CHECKPOINT_LOGIN_ACCEPTED
        if any(token in url for token in ("submit", "review")):
            return CHECKPOINT_SUBMIT
        if any(token in url for token in ("profile", "personal")):
            return CHECKPOINT_PROFILE
    if result == "captcha":
        return CHECKPOINT_SUBMIT if provider_solved else CHECKPOINT_ATTEMPT_COMPLETED
    return reason
