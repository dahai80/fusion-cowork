"""Daily digest builder for the support mailbox agent (issue #546, P0).

Renders the last-24h feedback rows into a plain-text digest and emails it
to the owner via the shared SMTP config (email_notify.py helpers).
P0 red line: the digest is the ONLY outbound mail the agent ever sends —
zero per-message replies until P1 (and even then template-only).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List

from ..server.email_notify import resolve_recipient, smtp_config_from_env

logger = logging.getLogger(__name__)

_CATEGORY_LABELS = {
    "bug": "Bug 报告",
    "feature": "功能需求",
    "business": "商务询价",
    "data_rights": "数据权利",
    "account": "账号问题",
    "consult": "使用咨询",
    "spam": "垃圾/退信",
    "unclassified": "未分类",
}


class DigestBuilder:
    def __init__(self, smtp_send=None):
        # smtp_send seam: tests replace the SMTP send; None = default smtplib path
        self._smtp_send = smtp_send

    def render(self, rows: List[Dict[str, Any]], window_start: datetime) -> str:
        counts: Dict[str, int] = {}
        human_queue_rows = []
        highlight_rows = []
        for r in rows:
            counts[r.get("category", "unclassified")] = counts.get(r.get("category", "unclassified"), 0) + 1
            if r.get("human_queue"):
                human_queue_rows.append(r)
            if r.get("category") in ("bug", "business", "data_rights"):
                highlight_rows.append(r)
        lines = [
            f"Support mailbox daily digest — {window_start.strftime('%Y-%m-%d %H:%M')} UTC ~ now (P0 observe-only)",
            "",
            f"Total inbound: {len(rows)}",
        ]
        for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {_CATEGORY_LABELS.get(cat, cat)}: {n}")
        lines.append("")
        if highlight_rows:
            lines.append(f"Needs attention ({len(highlight_rows)}):")
            for r in highlight_rows[:20]:
                lines.append(f"  [{r.get('category')}] {r.get('subject', '')[:60]} — {r.get('from_addr', '')}")
            lines.append("")
        if human_queue_rows:
            lines.append(f"Human queue (low confidence / sensitive): {len(human_queue_rows)}")
            for r in human_queue_rows[:20]:
                lines.append(f"  [{r.get('category')}] {r.get('subject', '')[:60]} (conf={r.get('confidence', 0):.2f})")
            lines.append("")
        lines.append("P0 observe-only: no outbound actions taken on any message.")
        return "\n".join(lines)

    async def send_digest(self, digest_text: str) -> bool:
        cfg = smtp_config_from_env()
        if cfg is None:
            logger.warning("digest not sent: SMTP not configured (SMTP_HOST unset)")
            return False
        to_addr = resolve_recipient()
        return await self._deliver(cfg, to_addr, digest_text)

    async def _deliver(self, cfg: Dict[str, Any], to_addr: str, digest_text: str) -> bool:
        import asyncio
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = "[fusion-cowork mailbox] daily digest (P0)"
        msg["From"] = cfg.get("from_addr", "")
        msg["To"] = to_addr
        msg.set_content(digest_text)
        send = self._smtp_send or self._default_send
        try:
            await asyncio.to_thread(send, cfg, msg)
            logger.info(f"digest sent to {to_addr}")
            return True
        except Exception as e:
            logger.warning(f"digest send failed: {e}")
            return False

    @staticmethod
    def _default_send(cfg: Dict[str, Any], msg: Any) -> None:
        import smtplib

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10.0) as smtp:
            if cfg.get("tls", True):
                smtp.starttls()
            if cfg.get("user"):
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)

    async def build_and_send(self, store, window_hours: int = 24) -> bool:
        window_start = datetime.now(UTC) - timedelta(hours=window_hours)
        rows = await store.list_since(window_start.isoformat())
        digest_text = self.render(rows, window_start)
        return await self.send_digest(digest_text)
