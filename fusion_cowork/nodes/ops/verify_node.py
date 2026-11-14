"""issue #480: verify_objective — mandatory verification node (deliverable vs top-level objective).

MAST FC3 (arXiv 2503.13657): explicit high-level objective verification adds
+15.6% success on ProgramDev — verification must be an explicit DAG node, not
implied by role prompts. This node judges a deliverable against the run's
top-level objective under a rubric:

- strategy="keyword" (default): deterministic — each rubric item must appear
  (case-insensitive substring) in the deliverable text; min_ratio gates the
  overall verdict. Zero-LLM, CI-safe.
- strategy="llm": judge prompt over FusionMLXClient chat, JSON verdict per item
  ({results: [{item, passed, evidence}]}, lenient fallback parse).
- on_fail="fail" (default): verification miss = node FAILED (run sees it);
  on_fail="flag": SUCCESS with passed=false in data (branch routing).

Workflow.validate() pairs with this node: graphs whose terminal deliverables
lack a verify_objective predecessor fail validation under
verification_policy="strict" (default "lenient" = warning only, "off" = silent).

Results feed the ApprovalJournal for audit (engine-side, issue #480): rubric +
verdict recorded as a decided journal row.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)

_DEFAULT_MIN_RATIO = 1.0
_JUDGE_MAX_DELIVERABLE_CHARS = 60000


def _keyword_check(deliverable: str, rubric: List[str]) -> List[Dict[str, Any]]:
    # Deterministic: case-insensitive substring containment per rubric item
    low = deliverable.lower()
    out = []
    for item in rubric:
        needle = str(item).strip().lower()
        out.append({"item": item, "passed": bool(needle and needle in low), "evidence": "keyword match"})
    return out


def _extract_json_block(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


@register_node
class VerifyObjectiveNode(BaseNode):
    """目标校验节点 — 交付物对照顶层目标 + rubric 的显式验证步 (issue #480)。"""

    name = "verify_objective"
    display_name = "目标校验"
    category = NodeCategory.LOGIC
    description = (
        "Verify a deliverable against the top-level objective under a rubric "
        "(keyword strategy deterministic, llm strategy via chat judge); "
        "verification miss fails the run by default"
    )
    icon = "✅"
    default_label = "目标校验"

    inputs = [
        {"key": "deliverable", "label": "待验交付物", "type": "string"},
        {"key": "objective", "label": "顶层目标", "type": "string"},
    ]
    outputs = [
        {"key": "passed", "label": "校验结论", "type": "boolean"},
        {"key": "rubric_results", "label": "逐项结果", "type": "array"},
        {"key": "objective", "label": "校验目标", "type": "string"},
        {"key": "summary", "label": "结论摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "顶层目标 (对照基准); 空时取上游输入 objective"},
                "deliverable": {
                    "type": "string",
                    "description": "待验交付物文本; 空时取上游输入 deliverable/data 文本",
                },
                "rubric": {"type": "array", "description": "校验项清单 (字符串列表)", "items": {"type": "string"}},
                "strategy": {
                    "type": "string",
                    "enum": ["keyword", "llm"],
                    "description": "keyword = 确定性子串包含 (零 LLM, 缺省); llm = chat judge 逐项判",
                    "default": "keyword",
                },
                "min_ratio": {
                    "type": "number",
                    "description": f"rubric 通过比例阈值 (缺省 {_DEFAULT_MIN_RATIO})",
                    "default": _DEFAULT_MIN_RATIO,
                },
                "on_fail": {
                    "type": "string",
                    "enum": ["fail", "flag"],
                    "description": "fail = 校验不过节点 FAILED (缺省); flag = SUCCESS 但 passed=false (分支路由)",
                    "default": "fail",
                },
                "llm_model": {"type": "string", "description": "strategy=llm 的判官模型 (空 = 引擎选模)"},
            },
            "required": ["rubric"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = getattr(self.config, "params", {}) or {}
        objective = str(params.get("objective") or inputs.get("objective") or "").strip()
        deliverable = str(params.get("deliverable") or inputs.get("deliverable") or inputs.get("data") or "").strip()
        rubric_raw = params.get("rubric")
        if not isinstance(rubric_raw, list) or not rubric_raw:
            return NodeResult(status=NodeStatus.FAILED, error="verify_objective 参数 rubric 必填 (非空数组)")
        rubric = [str(item) for item in rubric_raw if str(item).strip()]
        if not rubric:
            return NodeResult(status=NodeStatus.FAILED, error="verify_objective 参数 rubric 必填 (非空数组)")
        if not objective:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="verify_objective 缺 objective — 顶层目标对照基准必填 (params.objective 或上游输入)",
            )
        if not deliverable:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="verify_objective 缺 deliverable — 待验交付物必填 (params.deliverable 或上游输入)",
            )
        try:
            min_ratio = float(params.get("min_ratio", _DEFAULT_MIN_RATIO))
        except (TypeError, ValueError):
            min_ratio = _DEFAULT_MIN_RATIO
        strategy = str(params.get("strategy") or "keyword").strip().lower()
        on_fail = str(params.get("on_fail") or "fail").strip().lower()

        logger.info(
            f"verify_objective 开始: strategy={strategy} rubric={len(rubric)} 项 "
            f"deliverable={len(deliverable)} chars objective={objective[:60]!r}"
        )
        if strategy == "llm":
            rubric_results = await self._llm_judge(objective, deliverable, rubric, str(params.get("llm_model") or ""))
        else:
            rubric_results = _keyword_check(deliverable, rubric)

        passed_count = sum(1 for r in rubric_results if r["passed"])
        ratio = passed_count / len(rubric_results) if rubric_results else 0.0
        overall = ratio >= min_ratio
        summary = (
            f"verify_objective: {passed_count}/{len(rubric_results)} rubric 项通过 (ratio={ratio:.2f} ≥ {min_ratio})"
            if overall
            else (
                f"verify_objective 未通过: {passed_count}/{len(rubric_results)} rubric 项通过 (ratio={ratio:.2f} < {min_ratio})"
            )
        )
        data = {
            "passed": overall,
            "objective": objective,
            "rubric_results": rubric_results,
            "strategy": strategy,
            "ratio": ratio,
            "min_ratio": min_ratio,
            "summary": summary,
        }
        if not overall and on_fail != "flag":
            failed_items = [r["item"] for r in rubric_results if not r["passed"]]
            logger.warning(f"{summary}; 未过项: {failed_items}")
            return NodeResult(status=NodeStatus.FAILED, error=summary, data=data)
        logger.info(summary)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data=data,
            summary=summary,
        )

    async def _llm_judge(self, objective: str, deliverable: str, rubric: List[str], model: str) -> List[Dict[str, Any]]:
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        client = FusionMLXClient()
        rubric_lines = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(rubric))
        prompt = (
            "You are a strict verification judge. Given the objective, the deliverable and a rubric, "
            "decide for EACH rubric item whether the deliverable satisfies it. Respond with ONLY a JSON "
            'object: {"results": [{"item": "<rubric item>", "passed": true|false, "evidence": "<why>"}]}.\n\n'
            f"Objective:\n{objective}\n\nRubric:\n{rubric_lines}\n\n"
            f"Deliverable:\n{deliverable[:_JUDGE_MAX_DELIVERABLE_CHARS]}"
        )
        try:
            async with client:
                resp = await client.chat(model=model, messages=[{"role": "user", "content": prompt}])
            import json as _json

            parsed = _json.loads(_extract_json_block(resp.content or ""))
            rows = parsed.get("results") or []
            by_item = {str(r.get("item", "")).strip().lower(): r for r in rows if isinstance(r, dict)}
        except Exception as e:
            logger.error(f"verify_objective llm judge failed: {e}")
            raise RuntimeError(f"verify_objective llm judge failed: {e}") from e
        out = []
        for item in rubric:
            row = by_item.get(item.strip().lower()) or {}
            out.append(
                {
                    "item": item,
                    "passed": bool(row.get("passed", False)),
                    "evidence": str(row.get("evidence") or ""),
                }
            )
        return out
