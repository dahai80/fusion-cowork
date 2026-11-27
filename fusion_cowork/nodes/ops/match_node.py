"""issue #422: historical_match — historical project match node (extend vs new verdict).

Runs automatically after requirement structuring and before scaffolding. Scores deterministic
capability overlap against candidate projects (the customer's own + other customers') with a
three-value verdict:
- extend: matched the customer's own project (score >= threshold) — reuse the existing repo, extend incrementally
- base: matched another customer's project — distill shared capabilities into the base product for the
  new repo to inherit (never write to another customer's repo, never leak project names to another customer)
- new: no hit — current behavior, fresh scaffold

Scoring v1 is deterministic lexical overlap (zero LLM, zero cost, reproducible in synthetic E2E);
gaps = required capabilities minus capabilities the candidate already has. Candidates come from
params.candidates (inline) or candidates_file (JSON).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

# issue #439: CJK ranges kept as unicode escapes — tokenization logic data, not prose
_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
# Chinese stopwords (token values, intentionally CJK — written as unicode escapes)
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "with",
        "in",
        "on",
        "by",
        "app",
        "application",
        "system",
        "platform",
        "project",
        "support",
        "managed",
        "\u9700\u8981",
        "\u652f\u6301",
        "\u4e00\u4e2a",
        "\u7cfb\u7edf",
        "\u5e73\u53f0",
        "\u9879\u76ee",
        "\u7ba1\u7406",
    }
)


def _tokenize(text: str) -> set:
    # issue #439: CJK runs have no separators — a contiguous run tokenizes as one
    # token and never equals a capability token. Segment CJK into character
    # bigrams (single chars kept as-is) so substring overlap scores deterministically;
    # latin/digit words unchanged.
    lower = str(text or "").lower()
    tokens = set()
    for w in _WORD_RE.findall(lower):
        if _CJK_RE.fullmatch(w):
            if len(w) == 1:
                tokens.add(w)
            else:
                tokens.update(w[i : i + 2] for i in range(len(w) - 1))
        elif len(w) >= 2:
            tokens.add(w)
    return {t for t in tokens if t not in _STOPWORDS}


def score_overlap(requirement: str, capabilities: List[str], stack: str = "") -> float:
    """Deterministic capability-overlap score in [0, 1]: containment — how much
    of the candidate's capability/stack vocabulary the requirement covers.
    Containment (not Jaccard) so a short capability list against a long
    requirement can still reach 1.0."""
    req = _tokenize(requirement)
    cand = _tokenize(" ".join(capabilities) + " " + str(stack or ""))
    if not req or not cand:
        return 0.0
    return len(req & cand) / len(cand)


def _load_candidates(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = [c for c in (params.get("candidates") or []) if isinstance(c, dict)]
    file_path = str(params.get("candidates_file", "") or "")
    if file_path:
        p = Path(file_path).expanduser()
        data = json.loads(p.read_text())
        items = data if isinstance(data, list) else (data.get("candidates") or [])
        candidates.extend(c for c in items if isinstance(c, dict))
    return candidates


@register_node
class HistoricalMatchNode(BaseNode):
    """Historical project match — requirement vs candidate capability overlap, three-value verdict extend/base/new."""

    name = "historical_match"
    display_name = "历史项目比对"  # i18n: user-facing
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "需求与历史项目能力重叠比对 — extend (客户自己的项目) / base (提炼公共能力) / new (全新)"
    icon = "🧭"
    default_label = "历史项目比对"  # i18n: user-facing

    inputs = [
        {"key": "requirement", "label": "需求文本", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "verdict", "label": "判定", "type": "string"},  # i18n: user-facing
        {"key": "score", "label": "匹配分", "type": "number"},  # i18n: user-facing
        {"key": "matched", "label": "命中项目", "type": "object"},  # i18n: user-facing
        {"key": "gaps", "label": "能力缺口", "type": "list"},  # i18n: user-facing
        # i18n: user-facing
        {"key": "extension_label", "label": "扩展标签 (extend 判定为 extension, 其余空)", "type": "string"},
        # i18n: user-facing
        {"key": "extension_contract", "label": "扩展交付契约段落 (extend 判定非空, 其余空)", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "requirement": {"type": "string", "description": "需求文本 (缺省取上游输入 requirement)"},
                "candidates": {
                    "type": "array",
                    "description": (
                        # i18n: user-facing
                        "候选项目列表: {slug, repo, capabilities: [], stack: '', customer: '', health: ''} — "
                        # i18n: user-facing
                        "customer 为空串表示本客户自有项目"
                    ),
                },
                # i18n: user-facing
                "candidates_file": {"type": "string", "description": "候选 JSON 文件路径 (与 candidates 合并)"},
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    # i18n: user-facing
                    "description": "需求要求的能力清单 — gaps = 要求 - 候选已具备",
                },
                "own_customer": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "本客户标识 (candidate.customer 与之相等才算 extend; 缺省空 = 全部自有)",
                },
                "threshold": {
                    "type": "number",
                    "default": 0.6,
                    "description": "extend/base 判定分数阈值 (缺省 0.6)",  # i18n: user-facing
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        requirement = str(inputs.get("requirement") or params.get("requirement") or "")
        if not requirement.strip():
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="缺少需求文本 (requirement)")
        threshold = float(params.get("threshold") or 0.6)
        own_customer = str(params.get("own_customer", "") or "")
        required = [str(x) for x in (params.get("required_capabilities") or [])]

        candidates = _load_candidates(params)
        logger.info(f"historical_match start: {len(candidates)} candidates threshold={threshold}")
        best: Dict[str, Any] = {}
        best_score = -1.0
        for cand in candidates:
            score = score_overlap(
                requirement, [str(x) for x in (cand.get("capabilities") or [])], str(cand.get("stack", "") or "")
            )
            logger.info(f"historical_match candidate {cand.get('slug', '?')} score={score:.3f}")
            if score > best_score:
                best, best_score = cand, score

        if best_score < 0 or best_score < threshold:
            verdict = "new"
            matched, gaps = {}, list(required)
            logger.info(f"historical_match verdict=new (best={max(best_score, 0.0):.3f} < {threshold})")
        else:
            matched = {
                "slug": str(best.get("slug", "") or ""),
                "repo": str(best.get("repo", "") or ""),
                "capabilities": [str(x) for x in (best.get("capabilities") or [])],
                "stack": str(best.get("stack", "") or ""),
                "health": str(best.get("health", "") or ""),
                "customer": str(best.get("customer", "") or ""),
            }
            gaps = [cap for cap in required if cap not in matched["capabilities"]]
            # issue #422: match on the customer's own project = extend; match on another customer's project = base
            # (distill shared capabilities; never write to another customer's repo, never leak project names to them)
            verdict = "extend" if matched["customer"] == own_customer else "base"
            logger.info(
                f"historical_match verdict={verdict} matched={matched['slug']} score={best_score:.3f} gaps={gaps}"
            )

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "verdict": verdict,
                "score": round(max(best_score, 0.0), 4),
                "threshold": threshold,
                # issue #444: base verdict blanks the raw matched too — step output_data is exposed verbatim via
                # GET /api/runs/{id}, and raw fields carrying another customer's slug/repo violate the #422 red line;
                # same policy as matched_visible. Template-interpolation safe: matched_repo is always "" for base
                "matched": {} if verdict == "base" else matched,
                "gaps": gaps,
                "candidates_scanned": len(candidates),
                # issue #422: flat keys for template ${step.<id>.result.matched_repo} interpolation —
                # only extend carries a value (missing-path interpolation in new/base errors per #343)
                "matched_repo": matched.get("repo", "") if verdict == "extend" else "",
                # base-verdict privacy red line: the new customer's visible surface never carries another customer's project name
                "matched_visible": {} if verdict == "base" else matched,
                # issue #427: extension traceability — extend verdict emits the extension label (github_api filters empty strings)
                "extension_label": "extension" if verdict == "extend" else "",
                # issue #427: delivery contract for extend runs — the PR must carry an Extension Log (CHANGELOG)
                # and mark this run as an extension of the existing repo; empty string on non-extend runs (prompt byte-identical)
                "extension_contract": (
                    "\n\n## Extension contract (match verdict: extend)"
                    "\nThis run extends the existing repo (see scaffold source). Deliverables must include:"
                    "\n1. A PR label `extension`."
                    "\n2. A CHANGELOG entry under a top-level `## Extension Log` heading listing the capabilities added by this run."
                    "\n3. The PR body states it extends the existing product rather than a new project."
                )
                if verdict == "extend"
                else "",
            },
            # i18n: user-facing
            summary=f"判定 {verdict} (score={max(best_score, 0.0):.2f}, {len(candidates)} 候选)",
        )
