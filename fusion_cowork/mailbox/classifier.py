"""Mail classifier for the support mailbox agent (issue #546).

Stage 1 — deterministic rule pre-filter (no LLM): bounces, no-reply
senders, known spam signals are classified directly and cheap.
Stage 2 — LLM classify (local mlx via FusionMLXClient) into
bug/feature/business/data_rights/account/consult/spam + sentiment +
urgency, with confidence. Low confidence (below threshold) = human queue
(fail-open to human, never to automation — spec §6 red line).

Routes (P0 observe-only): every route only records + feeds the digest;
outbound sends are disabled until P1. ROUTE_TABLE documents the P1
mapping so the classification contract is already stable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CATEGORIES = ("bug", "feature", "business", "data_rights", "account", "consult", "spam")
SENTIMENTS = ("positive", "neutral", "negative")
URGENCIES = ("low", "medium", "high")

_DEFAULT_CONFIDENCE_THRESHOLD = 0.6

# spec §6 routing; P0 observe-only = routed_to recorded, no send
ROUTE_TABLE = {
    "bug": "mined_issues+auto_ack",
    "feature": "requirement_pool",
    "business": "owner_notify+sla",
    "data_rights": "owner_notify+sla",
    "consult": "faq_draft",
    "spam": "drop",
}

# deterministic pre-filter signals -> class without LLM
_NO_REPLY_PATTERNS = ("no-reply", "noreply", "no_reply", "donotreply", "mailer-daemon", "postmaster")
# i18n: user-facing
_BOUNCE_SUBJECT_PATTERNS = ("undeliver", "delivery status", "delivery failure", "returned mail", "退信")
_HUMAN_QUEUE_CLASSES = ("business", "data_rights")
_ANGRY_SENTIMENT = "negative"

_SYSTEM_PROMPT = (
    "You classify inbound support emails for a local-first AI product. "
    "Reply with ONE JSON object only, no markdown fence, exactly these keys: "
    '{"category": one of ["bug","feature","business","data_rights","account","consult","spam"], '
    '"sentiment": one of ["positive","neutral","negative"], '
    '"urgency": one of ["low","medium","high"], '
    '"confidence": number 0-1, "reason": one short sentence}. '
    "data_rights covers data export/deletion/privacy/compliance demands. "
    "business covers pricing/purchasing/partnership/invoicing. "
    "Mark spam for unsolicited marketing or phishing."
)


def _rule_prefilter(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # returns a full classification dict when a deterministic rule fires
    from_addr = (msg.get("from_addr") or "").lower()
    subject = (msg.get("subject") or "").lower()
    local_part = from_addr.split("@")[0] if "@" in from_addr else from_addr
    if any(p in local_part for p in _NO_REPLY_PATTERNS) or "mailer-daemon" in from_addr:
        return {
            "category": "spam",
            "sentiment": "neutral",
            "urgency": "low",
            "confidence": 0.99,
            "reason": "deterministic rule: no-reply/mailer-daemon sender",
        }
    if any(p in subject for p in _BOUNCE_SUBJECT_PATTERNS):
        return {
            "category": "spam",
            "sentiment": "neutral",
            "urgency": "low",
            "confidence": 0.99,
            "reason": "deterministic rule: bounce subject",
        }
    if "unsubscribe" in subject or ("unsolicited" in (msg.get("body") or "").lower()[:500] and "offer" in subject):
        return {
            "category": "spam",
            "sentiment": "neutral",
            "urgency": "low",
            "confidence": 0.9,
            "reason": "deterministic rule: marketing subject",
        }
    return None


def parse_classification_json(text: str) -> Optional[Dict[str, Any]]:
    # robust JSON extraction from LLM output (fence stripping, object search)
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("category") not in CATEGORIES:
        return None
    obj["sentiment"] = obj.get("sentiment") if obj.get("sentiment") in SENTIMENTS else "neutral"
    obj["urgency"] = obj.get("urgency") if obj.get("urgency") in URGENCIES else "medium"
    try:
        obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
    except (TypeError, ValueError):
        obj["confidence"] = 0.0
    return obj


class MailClassifier:
    def __init__(
        self, mlx: Any, model: str = "qwen3.5-9b", confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD
    ):
        self._mlx = mlx
        self._model = model
        self._threshold = confidence_threshold

    async def classify(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        # full pipeline for one message: rule pre-filter -> LLM -> route
        rule = _rule_prefilter(msg)
        if rule is not None:
            rule["rule_prefilter"] = rule["reason"]
            rule["human_queue"] = False
            rule["routed_to"] = ROUTE_TABLE[rule["category"]]
            rule["model_raw"] = ""
            return rule

        classification = await self._llm_classify(msg)
        if classification is None:
            # unparseable LLM output = fail-open to human queue, never to automation
            classification = {
                "category": "consult",
                "sentiment": "neutral",
                "urgency": "medium",
                "confidence": 0.0,
                "reason": "LLM output unparseable",
            }
            human_queue = True
            model_raw = "unparseable"
        else:
            human_queue = (
                classification["confidence"] < self._threshold
                or classification["category"] in _HUMAN_QUEUE_CLASSES
                or (
                    classification["category"] == "bug"
                    and classification["sentiment"] == _ANGRY_SENTIMENT
                    and classification["urgency"] == "high"
                )
            )
            model_raw = json.dumps(classification, ensure_ascii=False)
        classification["human_queue"] = human_queue
        classification["routed_to"] = ROUTE_TABLE.get(classification["category"], "human_queue")
        if human_queue and classification["category"] not in ("spam",):
            classification["routed_to"] = "human_queue"
        classification["rule_prefilter"] = ""
        classification["model_raw"] = model_raw
        return classification

    async def _llm_classify(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        user_content = (
            f"From: {msg.get('from_addr', '')}\nSubject: {msg.get('subject', '')}\n\n{(msg.get('body') or '')[:6000]}"
        )
        try:
            response = await self._mlx.chat(
                self._model,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                max_tokens=512,
            )
        except Exception as e:
            logger.warning(f"LLM classify failed (fail-open to human queue): {e}")
            return None
        content = getattr(response, "content", None) or (response.get("content") if isinstance(response, dict) else "")
        return parse_classification_json(content or "")
