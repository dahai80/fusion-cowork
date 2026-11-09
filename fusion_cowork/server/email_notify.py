"""HITL email notification (issue #404).

Every HITL touchpoint in the delivery pipeline (approval gates A/B/C and any
agent-node permission prompt) notifies the responsible user by email using the
address registered at fusion-identity signup — no new email storage.

- Config: SMTP_HOST/PORT/USER/PASS/FROM/TLS env-injected only; SMTP_HOST
  absent → notifier disabled, zero behavior change.
- Fire-and-forget: send runs on a daemon thread, 3 attempts, failure logs a
  WARN and never blocks the pipeline.
- Recipient: FUSION_COWORK_NOTIFY_EMAIL override, else the fusion-identity
  registration record (user profile lookup keyed by the run's identity token).
- Mail: plain text English v1 — run_id, node name, deep link
  #/pipeline/<run_id>, one-line summary of what awaits / what was decided.
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
import time
from email.message import EmailMessage
from typing import Any, Dict, Optional

logger = logging.getLogger("fusion_cowork.email_notify")

_DEFAULT_BASE_URL = "http://127.0.0.1:11461"
_MAX_ATTEMPTS = 3
_RETRY_SLEEP = 2.0
_SMTP_TIMEOUT = 10.0

# test seam — replaces the SMTP send entirely
SEND_SEAM: Any = None


def smtp_config_from_env() -> Optional[Dict[str, Any]]:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "from_addr": os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", ""),
        "tls": os.environ.get("SMTP_TLS", "1") not in ("0", "false", "off", "no"),
    }


def resolve_recipient() -> str:
    """Recipient for the responsible user: env override first, then the
    fusion-identity registration record of the run's identity token."""
    override = os.environ.get("FUSION_COWORK_NOTIFY_EMAIL", "").strip()
    if override:
        return override
    from ..auth.identity_propagation import get_current_identity_token

    token = get_current_identity_token()
    if not token:
        logger.warning("[hitl-email] no identity token on the run — recipient unknown, skipped")
        return ""
    try:
        import jwt as pyjwt

        claims = pyjwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except Exception as e:
        logger.warning("[hitl-email] identity token undecodable (%s) — skipped", e)
        return ""
    uid = str(claims.get("uid") or claims.get("sub") or claims.get("user_id") or "")
    if not uid:
        logger.warning("[hitl-email] identity token carries no uid claim — skipped")
        return ""
    from ..auth.identity import get_identity_client

    client = get_identity_client()
    if client is None:
        logger.warning("[hitl-email] identity disabled — cannot resolve email for uid=%s, skipped", uid)
        return ""
    email = client.get_user_email(uid)
    if not email:
        logger.warning("[hitl-email] identity user %s has no registered email — skipped", uid)
    return email or ""


def _default_send(cfg: Dict[str, Any], msg: EmailMessage) -> None:
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=_SMTP_TIMEOUT) as smtp:
        if cfg["tls"]:
            smtp.starttls()
        if cfg["user"] and cfg["password"]:
            smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


class HitlEmailNotifier:
    """Callable wired onto WorkflowEngine.hitl_notifier — never raises."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_url: str = "",
        send_fn: Any = None,
    ):
        self._config = config or smtp_config_from_env()
        self._base_url = (base_url or os.environ.get("FUSION_COWORK_WEB_BASE_URL", "") or _DEFAULT_BASE_URL).rstrip("/")
        self._send_fn = send_fn or (SEND_SEAM if SEND_SEAM is not None else _default_send)

    @property
    def enabled(self) -> bool:
        return bool(self._config and self._config.get("host"))

    def __call__(
        self, execution_id: str, node_name: str, check_input: Dict[str, Any], phase: str, outcome: str
    ) -> None:
        if not self.enabled:
            return
        if phase == "resolved" and os.environ.get("SMTP_NOTIFY_RESOLVED", "") not in ("1", "true", "yes"):
            return
        to_addr = resolve_recipient()
        if not to_addr:
            return
        subject, body = self._render(execution_id, node_name, phase, outcome)
        thread = threading.Thread(
            target=self._deliver,
            args=(dict(self._config or {}), to_addr, subject, body, execution_id),
            daemon=True,
            name=f"hitl-email-{execution_id}",
        )
        thread.start()
        logger.info("[hitl-email] %s phase=%s node=%s -> %s (async)", execution_id, phase, node_name, to_addr)

    def _render(self, execution_id: str, node_name: str, phase: str, outcome: str) -> tuple[str, str]:
        link = f"{self._base_url}/#/pipeline/{execution_id}"
        if phase == "pending":
            subject = f"[fusion-cowork] Approval required — run {execution_id}"
            summary = f"Node '{node_name}' is paused waiting for your approval."
        else:
            subject = f"[fusion-cowork] Approval resolved — run {execution_id}"
            summary = f"Node '{node_name}' gate resolved: {outcome or 'decided'}."
        body = "\n".join(
            [
                summary,
                "",
                f"run_id: {execution_id}",
                f"node: {node_name}",
                f"phase: {phase}" + (f" ({outcome})" if outcome else ""),
                f"deep link: {link}",
                "",
                "-- fusion-cowork delivery pipeline",
            ]
        )
        return subject, body

    def _deliver(self, cfg: Dict[str, Any], to_addr: str, subject: str, body: str, execution_id: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("from_addr") or cfg.get("user") or "fusion-cowork@localhost"
        msg["To"] = to_addr
        msg.set_content(body)
        last_err: Optional[Exception] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                self._send_fn(cfg, msg)
                logger.info("[hitl-email] %s delivered to %s (attempt %s)", execution_id, to_addr, attempt)
                return
            except Exception as e:
                last_err = e
                logger.warning("[hitl-email] %s send attempt %s/%s failed: %s", execution_id, attempt, _MAX_ATTEMPTS, e)
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_SLEEP)
        logger.warning(
            "[hitl-email] %s notification NOT delivered to %s after %s attempts: %s — pipeline continues",
            execution_id,
            to_addr,
            _MAX_ATTEMPTS,
            last_err,
        )
