"""Decision gate node (issue #517): typed-decision gating via fusion-mlx.

Short-state binary/categorical gating decisions (escalate-or-continue,
retry-vs-fail, route selection) call the fusion-mlx decision lane (Laya
decision head) instead of burning a full LLM turn: <15ms P50 vs
multi-second baseline, zero generation tokens. `noul` / low-confidence /
timeout / transport errors fall back to a single LLM turn (conservative
default preserved). Deterministic routing stays in plain code — only
model-uncertain decisions belong here.

Constraints honored: decision state must fit the decision model's short
context (max_len 1024) — the state is compacted and hard-truncated; the
decision URL defaults to fusion-mlx :11434 (env FUSION_MLX_DECISION_URL
override) since the gateway may not proxy the decision lane.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

import httpx

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

# decision head max_len=1024 — keep the serialized state well under it
_MAX_STATE_CHARS = 900
_DEFAULT_DECISION_URL = "http://127.0.0.1:11434"
# below this confidence the answer counts as model-uncertain -> LLM fallback
_DEFAULT_CONFIDENCE_THRESHOLD = 0.5


def _resolve_decision_url() -> str:
    return os.environ.get("FUSION_MLX_DECISION_URL", "").strip() or _DEFAULT_DECISION_URL


def _auth_headers() -> Dict[str, str]:
    # fusion-mlx backend key — canonical resolution via fusion-core (issue #204
    # key model); unresolvable = no header (server decides, fallback covers 401)
    try:
        from fusion_core import config as core_config

        key = str(core_config.resolve_api_key() or "").strip()
    except Exception as e:
        logger.warning(f"decision_gate: backend key resolution failed: {e}")
        key = ""
    if not key:
        logger.warning("decision_gate: no backend key resolved — decision API calls will 401")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _new_client(**kwargs: Any) -> httpx.AsyncClient:
    # trust_env=False: decision lane is local — never inherit ambient proxies
    return httpx.AsyncClient(trust_env=False, **kwargs)


def _settings_key() -> str:
    # direct settings.json read — bypasses env-var precedence so a stale
    # legacy env key cannot permanently poison the decision lane
    try:
        from fusion_core.config import load_settings

        auth = (load_settings() or {}).get("auth") or {}
        return str(auth.get("api_key") or "").strip()
    except Exception as e:
        logger.debug(f"decision_gate: settings key unavailable: {e}")
        return ""


def _compact_state(state: Any) -> str:
    # issue #517 constraint: compact state summary, not full node history
    if isinstance(state, str):
        text = state
    else:
        try:
            text = json.dumps(state, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(state)
    if len(text) > _MAX_STATE_CHARS:
        text = text[:_MAX_STATE_CHARS]
        logger.warning(f"decision state truncated to {_MAX_STATE_CHARS} chars (max_len 1024 constraint)")
    return text


def _low_confidence(answer: Dict[str, Any], threshold: float) -> bool:
    try:
        return float(answer.get("confidence") or 0.0) < threshold
    except (TypeError, ValueError):
        return True


@register_node
class DecisionGateNode(BaseNode):
    """fusion-mlx decision gate — typed short-state gating via the Laya decision head, no full LLM turn burned."""

    name = "decision_gate"
    # i18n: user-facing
    display_name = "决策门"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Call the fusion-mlx decision lane (/v1/decisions or /v1/laya/decide) for short-state gating; noul/low-confidence falls back to an LLM turn"
    icon = "🚦"
    # i18n: user-facing
    default_label = "决策门"

    inputs = [
        # i18n: user-facing
        {"key": "state", "label": "决策状态", "type": "object"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "answers", "label": "决策答案", "type": "object"},
        # i18n: user-facing
        {"key": "decision_meta", "label": "决策元信息", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "state": {
                    "type": ["object", "string"],
                    "description": "Compact decision state (string or JSON object); truncated to 900 chars",
                },
                "questions": {
                    "type": "object",
                    "description": "Questions for /v1/decisions: {qid: {type: choice|score|noul, instructions, criteria}} — instructions required, choice needs criteria list",
                },
                "preset": {
                    "type": "string",
                    "description": "Preset for /v1/laya/decide (e.g. 'router'); uses the laya endpoint instead",
                    "default": "",
                },
                "confidence_threshold": {
                    "type": "number",
                    "description": "Answers below this confidence fall back to the LLM",
                    "default": 0.5,
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Decision API timeout in seconds",
                    "default": 5.0,
                },
                "fallback_llm": {
                    "type": "boolean",
                    "description": "Fall back to a single LLM turn on noul/low-confidence/error",
                    "default": True,
                },
                "model": {
                    "type": "string",
                    "description": "Chat model id for the LLM fallback (or FUSION_MLX_MODEL env)",
                    "default": "",
                },
            },
            "required": ["state"],
        }

    def _decide_url(self, preset: str) -> str:
        base = _resolve_decision_url().rstrip("/")
        if preset:
            return f"{base}/v1/laya/decide"
        return f"{base}/v1/decisions"

    @staticmethod
    def _decide_payload(state: str, questions: Dict[str, Dict[str, Any]], preset: str) -> Dict[str, Any]:
        if preset:
            return {"prompt": state, "preset": str(preset)}
        return {"state": state, "questions": questions}

    async def _ask_llm(self, model: str, state: str, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        # issue #517: conservative fallback — one LLM turn reproducing the
        # typed-decision contract in JSON; usage recorded for the savings ledger
        from ...ai.mlx_client import FusionMLXClient

        client = FusionMLXClient()
        qnames = ", ".join(map(str, questions)) or "decision"
        prompt = (
            "You are a workflow gate. Decide strictly from the state below.\n"
            f"Questions: {qnames}\n"
            'Reply with JSON only: {"answers": {"<question_id>": {"choice": "...", "confidence": 0.0}}}\n'
            f"State: {state}"
        )
        resp = await client.chat(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=256)
        usage = dict(resp.usage or {})
        answers: Dict[str, Any] = {}
        try:
            parsed = json.loads(resp.content[resp.content.find("{") : resp.content.rfind("}") + 1])
            answers = parsed.get("answers") if isinstance(parsed.get("answers"), dict) else parsed
        except (ValueError, AttributeError) as e:
            logger.warning(f"LLM fallback answer parse failed: {e}")
        return {"answers": answers, "usage": usage}

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        state_raw = inputs.get("state", params.get("state"))
        if state_raw is None or (isinstance(state_raw, str) and not state_raw.strip()):
            return NodeResult(status=NodeStatus.FAILED, error="missing required param state")
        questions = params.get("questions") or {}
        if not isinstance(questions, dict):
            return NodeResult(
                status=NodeStatus.FAILED, error="questions must be an object {qid: {type, instructions, criteria}}"
            )
        preset = str(params.get("preset") or "")
        if not preset and not questions:
            return NodeResult(status=NodeStatus.FAILED, error="fill at least one of questions or preset")
        threshold = 0.5
        try:
            threshold = float(params.get("confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD))
        except (TypeError, ValueError):
            pass
        timeout_s = 5.0
        try:
            timeout_s = float(params.get("timeout_s", 5.0) or 5.0)
        except (TypeError, ValueError):
            pass
        fallback_llm = params.get("fallback_llm", True) is not False
        model = str(params.get("model") or os.environ.get("FUSION_MLX_MODEL") or "")

        state = _compact_state(state_raw)
        url = self._decide_url(preset)
        payload = self._decide_payload(state, questions, preset)
        started = time.perf_counter()
        engine = "laya"
        usage: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0}
        answers: Dict[str, Any] = {}
        fallback_reason = ""

        try:
            async with _new_client(timeout=timeout_s, headers=_auth_headers()) as http:
                resp = await http.post(url, json=payload)
                # self-heal: a stale legacy env key wins fusion-core precedence
                # but 401s against the backend — retry once with the
                # settings.json key (issue #204 two-key model)
                if resp.status_code in (401, 403):
                    sk = _settings_key()
                    if sk and sk != _auth_headers().get("Authorization", "").removeprefix("Bearer "):
                        logger.info("decision_gate: 401 on resolved key — retrying with settings.json key")
                        resp = await http.post(url, json=payload, headers={"Authorization": f"Bearer {sk}"})
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            fallback_reason = f"decision api error: {e}"
            logger.warning(f"decision_gate: {fallback_reason}")
        else:
            raw_answers = body.get("answers") if isinstance(body.get("answers"), dict) else body
            answers = raw_answers if isinstance(raw_answers, dict) else {}
            uncertain = [
                qid
                for qid, ans in answers.items()
                if not isinstance(ans, dict) or str(ans.get("type") or "") == "noul" or _low_confidence(ans, threshold)
            ]
            if uncertain:
                fallback_reason = f"noul/low-confidence at: {', '.join(map(str, uncertain))}"
                logger.info(f"decision_gate: {fallback_reason}")

        if fallback_reason:
            if not fallback_llm:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"decision gate uncertain and fallback disabled: {fallback_reason}",
                    data={"answers": answers, "decision_meta": {"engine": "laya", "fallback_reason": fallback_reason}},
                )
            if not model:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"LLM fallback requested but no model set (param model or FUSION_MLX_MODEL): {fallback_reason}",
                )
            engine = "llm-fallback"
            fb = await self._ask_llm(model, state, questions)
            answers, usage = fb["answers"], fb["usage"]

        latency_ms = int((time.perf_counter() - started) * 1000)
        meta = {
            "engine": engine,
            "route": "laya/decide" if preset else "decisions",
            "latency_ms": latency_ms,
            "url": url,
            "state_chars": len(state),
            "confidence_threshold": threshold,
            # issue #517 acceptance: measured token savings per gated decision —
            # the laya lane burns zero generation tokens; fallback usage recorded
            "tokens": usage,
            "tokens_saved_vs_llm_turn": usage.get("prompt_tokens", 0) == 0 and usage.get("completion_tokens", 0) == 0,
        }
        if fallback_reason:
            meta["fallback_reason"] = fallback_reason
        logger.info(
            f"decision_gate: engine={engine} route={meta['route']} latency_ms={latency_ms} "
            f"answers={len(answers)} tokens={usage.get('prompt_tokens', 0)}+{usage.get('completion_tokens', 0)}"
        )
        return NodeResult(status=NodeStatus.SUCCESS, data={"answers": answers, "decision_meta": meta})
