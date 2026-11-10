"""issue #422: historical_match — 历史项目比对节点 (extend vs new 判定)。

需求结构化后、脚手架前自动运行。对候选项目 (customer 自己的 + 其他客户的)
做确定性能力重叠打分, 三值判定:
- extend: 命中客户自己的项目 (score >= threshold) — 复用已有仓, 增量扩展
- base: 命中其他客户的项目 — 提炼公共能力进 base 产品后新仓继承 (绝不写他客户仓,
  绝不向他客户泄露项目名)
- new: 无命中 — 现行为, 全新脚手架

打分 v1 是确定性词面重叠 (零 LLM 零成本, 合成 E2E 可复现); gaps = 要求能力
减去候选已具备能力。候选来自 params.candidates (inline) 或 candidates_file (JSON)。
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

_WORD_RE = re.compile(r"[a-z0-9一-鿿]+")
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
        "需要",
        "支持",
        "一个",
        "系统",
        "平台",
        "项目",
        "管理",
    }
)


def _tokenize(text: str) -> set:
    words = _WORD_RE.findall(str(text or "").lower())
    return {w for w in words if len(w) >= 2 and w not in _STOPWORDS}


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
    """历史项目比对 — 需求 vs 候选项目能力重叠, 三值判定 extend/base/new。"""

    name = "historical_match"
    display_name = "历史项目比对"
    category = NodeCategory.TOOL
    description = "需求与历史项目能力重叠比对 — extend (客户自己的项目) / base (提炼公共能力) / new (全新)"
    icon = "🧭"
    default_label = "历史项目比对"

    inputs = [
        {"key": "requirement", "label": "需求文本", "type": "string"},
    ]
    outputs = [
        {"key": "verdict", "label": "判定", "type": "string"},
        {"key": "score", "label": "匹配分", "type": "number"},
        {"key": "matched", "label": "命中项目", "type": "object"},
        {"key": "gaps", "label": "能力缺口", "type": "list"},
        {"key": "extension_label", "label": "扩展标签 (extend 判定为 extension, 其余空)", "type": "string"},
        {"key": "extension_contract", "label": "扩展交付契约段落 (extend 判定非空, 其余空)", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "requirement": {"type": "string", "description": "需求文本 (缺省取上游输入 requirement)"},
                "candidates": {
                    "type": "array",
                    "description": (
                        "候选项目列表: {slug, repo, capabilities: [], stack: '', customer: '', health: ''} — "
                        "customer 为空串表示本客户自有项目"
                    ),
                },
                "candidates_file": {"type": "string", "description": "候选 JSON 文件路径 (与 candidates 合并)"},
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需求要求的能力清单 — gaps = 要求 - 候选已具备",
                },
                "own_customer": {
                    "type": "string",
                    "description": "本客户标识 (candidate.customer 与之相等才算 extend; 缺省空 = 全部自有)",
                },
                "threshold": {
                    "type": "number",
                    "default": 0.6,
                    "description": "extend/base 判定分数阈值 (缺省 0.6)",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        requirement = str(inputs.get("requirement") or params.get("requirement") or "")
        if not requirement.strip():
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
            # issue #422: 命中本客户项目 = extend; 命中他客户项目 = base (提炼公共能力,
            # 绝不写他客户仓, 绝不向他客户泄露项目名)
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
                "matched": matched,
                "gaps": gaps,
                "candidates_scanned": len(candidates),
                # issue #422: 扁平键供模板 ${step.<id>.result.matched_repo} 插值 —
                # 仅 extend 有值 (new/base 缺路径插值会按 #343 报错)
                "matched_repo": matched.get("repo", "") if verdict == "extend" else "",
                # base 判定的隐私红线: 新客户可见面绝不携带他客户项目名
                "matched_visible": {} if verdict == "base" else matched,
                # issue #427: 扩展可追溯 — extend 判定下发 extension 标签 (github_api 过滤空串)
                "extension_label": "extension" if verdict == "extend" else "",
                # issue #427: extend 运行的交付契约 — PR 必须带 Extension Log (CHANGELOG)
                # 并标注本运行是既有仓扩展; 非 extend 运行为空串 (prompt 逐字节不变)
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
            summary=f"判定 {verdict} (score={max(best_score, 0.0):.2f}, {len(candidates)} 候选)",
        )
