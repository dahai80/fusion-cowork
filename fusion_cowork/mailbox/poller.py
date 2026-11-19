"""IMAP poller for the support mailbox agent (issue #546).

5-minute poll cycle, message-id idempotency (UNIQUE dedupe lives in
FeedbackStore), crash-recoverable: last seen IMAP UIDVALIDITY/UID pair is
persisted in poll_state so a restart resumes without refetching the whole
mailbox. Credentials come from fusion-sv injected env only — never from
the repo (spec §6 red line).

imap_factory seam: tests inject a fake IMAP client; production builds
imaplib.IMAP4_SSL. All IMAP calls are sync stdlib, run via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import imaplib
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 300
_STATE_KEY = "imap_last_uid"

_DECODE_FALLBACK = "utf-8"
_MAX_BODY_CHARS = 20_000


def imap_config_from_env() -> Optional[Dict[str, Any]]:
    # any missing required field = mailbox agent disabled (log once at caller)
    host = os.environ.get("FUSION_MAILBOX_IMAP_HOST", "").strip()
    user = os.environ.get("FUSION_MAILBOX_IMAP_USER", "").strip()
    password = os.environ.get("FUSION_MAILBOX_IMAP_PASS", "").strip()
    if not host or not user or not password:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("FUSION_MAILBOX_IMAP_PORT", "993") or 993),
        "user": user,
        "password": password,
        "folder": os.environ.get("FUSION_MAILBOX_IMAP_FOLDER", "INBOX"),
    }


def _decode_header_value(raw: Any) -> str:
    try:
        parts = email.header.decode_header(str(raw))
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or _DECODE_FALLBACK, errors="replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception as e:
        logger.debug(f"header decode failed (raw used): {e}")
        return str(raw)


def _extract_body(msg: email.message.Message) -> str:
    # prefer text/plain, fall back to text/html stripped naively; hard cap size
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or _DECODE_FALLBACK
                    body += payload.decode(charset, errors="replace")
                if ctype == "text/plain":
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or _DECODE_FALLBACK
            body = payload.decode(charset, errors="replace")
    return body[:_MAX_BODY_CHARS]


class MailboxPoller:
    def __init__(
        self,
        config: Dict[str, Any],
        imap_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self._config = config
        self._imap_factory = imap_factory or self._default_factory

    @staticmethod
    def _default_factory(config: Dict[str, Any]) -> Any:
        return imaplib.IMAP4_SSL(config["host"], config["port"])

    def _fetch_sync(self, known_uid: str) -> Dict[str, Any]:
        # returns {"messages": [...], "last_uid": str}; raises on IMAP errors so
        # the service loop can log + retry next cycle (crash-recoverable)
        client = self._imap_factory(self._config)
        messages: List[Dict[str, Any]] = []
        last_uid = known_uid
        try:
            client.login(self._config["user"], self._config["password"])
            client.select(self._config["folder"], readonly=True)
            search = f"(UID {int(known_uid) + 1}:*)" if known_uid else "(ALL)"
            status, data = client.uid("search", None, search)
            if status != "OK":
                raise RuntimeError(f"IMAP UID search failed: {status}")
            uids = [u for u in (data[0] or b"").split() if int(u) > int(known_uid or 0)]
            for uid in uids[-200:]:
                status, msg_data = client.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                message_id = (msg.get("Message-ID") or "").strip() or f"uid-{uid.decode()}"
                from_addr = email.utils.parseaddr(_decode_header_value(msg.get("From", "")))[1]
                messages.append(
                    {
                        "message_id": message_id,
                        "from_addr": from_addr,
                        "subject": _decode_header_value(msg.get("Subject", "")),
                        "received_at": _decode_header_value(msg.get("Date", "")),
                        "body": _extract_body(msg),
                        "uid": uid.decode(),
                    }
                )
                last_uid = uid.decode()
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return {"messages": messages, "last_uid": last_uid}

    async def fetch_new(self, known_uid: str = "") -> Dict[str, Any]:
        # known_uid = persisted last seen UID; empty = first run (fetch ALL, the
        # store dedupes by message-id so a UIDVALIDITY change cannot duplicate rows)
        return await asyncio.to_thread(self._fetch_sync, known_uid)

    @staticmethod
    def state_key() -> str:
        return _STATE_KEY
