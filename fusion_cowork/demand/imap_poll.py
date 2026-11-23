# issue #585: QQ mailbox IMAP poller — read-only, UID-cursor, Message-ID dedupe.
# Mail content is untrusted data: body text is passed downstream as opaque
# payload; links are never fetched; attachments are never opened.
# The password comes from env only and is never logged.

from __future__ import annotations

import asyncio
import email
import email.header
import html as html_mod
import logging
import os
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from imaplib import IMAP4_SSL
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOST = "imap.qq.com"
DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"


@dataclass
class ImapConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    user: str = ""
    password: str = ""
    folder: str = DEFAULT_FOLDER

    @classmethod
    def from_env(cls) -> ImapConfig:
        # issue #595: operator env file (~/.fusion-cowork/demand_imap.env) is
        # loaded first; process env vars keep priority (setdefault semantics)
        try:
            from .settings import load_env_file

            load_env_file()
        except Exception:
            pass
        return cls(
            host=os.environ.get("FUSION_QQ_IMAP_HOST", DEFAULT_HOST),
            port=int(os.environ.get("FUSION_QQ_IMAP_PORT", str(DEFAULT_PORT))),
            user=os.environ.get("FUSION_QQ_IMAP_USER", ""),
            password=os.environ.get("FUSION_QQ_IMAP_PASSWORD", ""),
            folder=os.environ.get("FUSION_QQ_IMAP_FOLDER", DEFAULT_FOLDER),
        )


@dataclass
class RawMail:
    message_id: str = ""
    uid: int = 0
    folder: str = DEFAULT_FOLDER
    subject: str = ""
    from_addr: str = ""
    body_text: str = ""
    arrived_at: float = 0.0
    parts: Dict[str, str] = field(default_factory=dict)


def decode_header_value(raw: str) -> str:
    try:
        chunks = email.header.decode_header(raw or "")
        out = []
        for text, charset in chunks:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception as e:
        logger.warning(f"header decode failed (ignored): {e}")
        return raw or ""


_STRIP_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_STRIP_TAGS = re.compile(r"<[^>]+>")
_COLLAPSE_WS = re.compile(r"[ \t]*\n[ \t\n]*")


def strip_html(html_text: str) -> str:
    """HTML to plain text — regex-based, no new deps, never fetches anything."""
    if not html_text:
        return ""
    text = _STRIP_BLOCK.sub(" ", html_text)
    text = re.sub(r"<(br|/p|/div|/tr|/li)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _STRIP_TAGS.sub(" ", text)
    text = html_mod.unescape(text)
    text = _COLLAPSE_WS.sub("\n", text)
    return text.strip()


def _extract_body(msg: email.message.Message) -> str:
    """Walk multipart tree; prefer text/plain, fall back to stripped html.
    Attachments are NEVER decoded to disk or opened — text parts only."""
    plain_parts: List[str] = []
    html_parts: List[str] = []
    try:
        for part in msg.walk():
            if part.get_filename():
                continue  # attachment — skip entirely
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
    except Exception as e:
        logger.warning(f"body walk failed (ignored): {e}")
    if plain_parts:
        return "\n".join(plain_parts).strip()
    return strip_html("\n".join(html_parts))


def parse_message(raw_bytes: bytes, uid: int, folder: str) -> RawMail:
    msg = email.message_from_bytes(raw_bytes)
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    if not message_id:
        # dedupe requires a stable key — synthesize from headers when missing
        message_id = f"synthetic-{uid}-{msg.get('Date', '')}-{hash(msg.get('Subject', ''))}"
    arrived_at = 0.0
    try:
        if msg.get("Date"):
            arrived_at = parsedate_to_datetime(msg.get("Date")).timestamp()
    except Exception:
        arrived_at = 0.0
    return RawMail(
        message_id=message_id,
        uid=uid,
        folder=folder,
        subject=decode_header_value(msg.get("Subject", "")),
        from_addr=decode_header_value(msg.get("From", "")),
        body_text=_extract_body(msg),
        arrived_at=arrived_at,
    )


def poll_once_sync(cfg: ImapConfig, last_uid: int, batch_limit: int = 50) -> Dict[str, Any]:
    """One read-only poll cycle: fetch messages with UID > last_uid.
    Returns {mails, max_uid, error}. Never mutates the mailbox (EXAMINE only)."""
    conn: Optional[IMAP4_SSL] = None
    try:
        conn = IMAP4_SSL(cfg.host, cfg.port)
        conn.login(cfg.user, cfg.password)
        conn.select(cfg.folder, readonly=True)
        typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            return {"mails": [], "max_uid": last_uid, "error": f"UID search failed: {typ}"}
        uids = sorted(int(u) for u in (data[0] or b"").split() if u)
        uids = [u for u in uids if u > last_uid][:batch_limit]
        mails: List[RawMail] = []
        max_uid = last_uid
        for uid in uids:
            typ, fetch_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not fetch_data or fetch_data[0] is None:
                logger.warning(f"UID fetch failed uid={uid} (skipped)")
                continue
            raw_bytes = fetch_data[0][1]
            mails.append(parse_message(raw_bytes, uid, cfg.folder))
            max_uid = max(max_uid, uid)
        logger.info(f"imap poll: {len(mails)} new mails, cursor {last_uid} -> {max_uid}")
        return {"mails": mails, "max_uid": max_uid, "error": ""}
    except Exception as e:
        # never include the password — imaplib errors do not embed credentials
        logger.error(f"imap poll failed: {type(e).__name__}: {e}")
        return {"mails": [], "max_uid": last_uid, "error": f"{type(e).__name__}: {e}"}
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


async def poll_once(cfg: ImapConfig, last_uid: int, batch_limit: int = 50) -> Dict[str, Any]:
    return await asyncio.to_thread(poll_once_sync, cfg, last_uid, batch_limit)


def raw_mail_to_record(mail: RawMail) -> Dict[str, Any]:
    return {
        "message_id": mail.message_id,
        "uid": mail.uid,
        "folder": mail.folder,
        "subject": mail.subject,
        "from_addr": mail.from_addr,
        "body_text": mail.body_text,
        "arrived_at": mail.arrived_at or time.time(),
    }
