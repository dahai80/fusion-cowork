"""issue #249: run-context token budget + rolling summary — resume 链不再整链回放祖先步骤体。

三层细节降级 (超预算时按序丢, 不做字符串中段截断):
  full body (output_data 原样) → compact (标量截 200 字符/键数截 10) → ref-only
  (只剩 step://<run>/<node> 引用行) → ancestor_summary 里再丢 output_summary → 丢条目。

环境变量:
  FUSION_COWORK_RESUME_BUDGET_TOKENS  模型侧上下文预算 (默认 12000, 下限 200)
  FUSION_COWORK_RESUME_KEEP_RECENT    最近 N 步保全量 output_data (默认 8, 下限 1)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET_TOKENS = 12000
_MIN_BUDGET_TOKENS = 200
_DEFAULT_KEEP_RECENT = 8
_MAX_ANCESTOR_ENTRIES = 50
_SCALAR_CHARS = 200
_MAX_KEYS = 10

# 快照可选状态: success/completed = 正常成功步; skipped = 断点续跑预载副本
# (engine 跳过重跑时产出的 SKIPPED 步, 携带恢复的 output_data)。
SNAPSHOT_ELIGIBLE_STATUSES = ("success", "completed", "skipped")


def estimate_tokens(text: str) -> int:
    # 记录式启发: CJK 字符 ≈ 1 token, 其余 ≈ 4 字符/token (文档化预算, 非精确计数)
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return max(1, cjk + (len(text) - cjk + 3) // 4)


def budget_tokens() -> int:
    raw = (os.getenv("FUSION_COWORK_RESUME_BUDGET_TOKENS") or "").strip()
    if not raw:
        return _DEFAULT_BUDGET_TOKENS
    try:
        return max(_MIN_BUDGET_TOKENS, int(raw))
    except ValueError:
        return _DEFAULT_BUDGET_TOKENS


def keep_recent() -> int:
    raw = (os.getenv("FUSION_COWORK_RESUME_KEEP_RECENT") or "").strip()
    if not raw:
        return _DEFAULT_KEEP_RECENT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_KEEP_RECENT


def step_ref(run_id: str, node_id: str) -> str:
    return f"step://{run_id}/{node_id}"


def _cap_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_SCALAR_CHARS]
    if isinstance(value, dict):
        return {k: _cap_value(v) for k, v in list(value.items())[:_MAX_KEYS]}
    if isinstance(value, list):
        return [_cap_value(v) for v in value[:_MAX_KEYS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_SCALAR_CHARS]


def summarize_step(run_id: str, step: Dict[str, Any]) -> Dict[str, Any]:
    # ancestor_summary 条目: 每节点结果 + 关键输出 (截断) + 引用行; 无完整 output_data
    return {
        "node_id": step.get("node_id", ""),
        "node_name": step.get("node_name", ""),
        "status": step.get("status", ""),
        "summary": str(step.get("summary") or "")[:_SCALAR_CHARS],
        "output_ref": step_ref(run_id, str(step.get("node_id", ""))),
        "output_summary": _cap_value(step.get("output_data") or {}),
    }


def _snapshot_entry(step: Dict[str, Any], status: str) -> Dict[str, Any]:
    # engine 快照恢复只认 success/completed → skipped 副本归一化为 success (携带恢复输出)
    entry = dict(step)
    entry["status"] = status
    return entry


def _ctx_tokens(snapshot: List[Dict[str, Any]], summaries: List[Dict[str, Any]]) -> int:
    return estimate_tokens(repr(snapshot)) + estimate_tokens(repr(summaries))


def build_resume_snapshot(
    run_id: str,
    steps: List[Dict[str, Any]],
    cut_index: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """切点前的可选步骤 → (快照, 本轮新增 ancestor 摘要, 统计)。

    近 keep_recent 步保全量 output_data (执行正确性); 更早步收成摘要
    (快照条目降级为 compact 输出), 摘要行进 ancestor_summary。超预算按
    full → compact → ref-only → 丢摘要键 → 丢条目逐级降细节。
    """
    eligible = [
        s
        for s in steps[:cut_index]
        if isinstance(s, dict)
        and str(s.get("status", "")).lower() in SNAPSHOT_ELIGIBLE_STATUSES
        and not s.get("user_correction")
    ]
    keep = keep_recent()
    older: List[Dict[str, Any]] = []
    recent: List[Dict[str, Any]] = eligible
    if keep < len(eligible):
        older = eligible[: len(eligible) - keep]
        recent = eligible[len(eligible) - keep :]

    snapshot = [_snapshot_entry(s, "success") for s in recent]
    ancestor_new = [summarize_step(run_id, s) for s in older]

    limit = budget_tokens()

    def demote(entry: Dict[str, Any]) -> bool:
        # True = 细节又降了一级; False = 已到 ref-only 底线
        out = entry.get("output_data")
        entry.setdefault("output_ref", step_ref(run_id, str(entry.get("node_id", ""))))
        if out is None:
            return False
        capped = _cap_value(out) if isinstance(out, dict) else {}
        if capped == out:
            entry.pop("output_data", None)
        else:
            entry["output_data"] = capped
        return True

    while _ctx_tokens(snapshot, ancestor_new) > limit:
        if snapshot and demote(snapshot[0]):
            continue
        if snapshot:
            # 最老条目已 ref-only → 逐个跳过找还能降的; 全 ref-only 后丢最老条目
            dropped = snapshot.pop(0)
            ancestor_new.insert(0, summarize_step(run_id, dropped))
            continue
        if ancestor_new and "output_summary" in ancestor_new[0]:
            ancestor_new[0].pop("output_summary")
            continue
        if ancestor_new:
            ancestor_new.pop(0)
            continue
        break

    full = sum(1 for s in snapshot if "output_data" in s)
    stats = {
        "eligible": len(eligible),
        "full_steps": full,
        "snapshot_steps": len(snapshot),
        "ancestor_new": len(ancestor_new),
        "estimated_tokens": _ctx_tokens(snapshot, ancestor_new),
        "budget": limit,
    }
    logger.info(
        f"[run-context] resume 快照: eligible={stats['eligible']} full={full} "
        f"snapshot={stats['snapshot_steps']} ancestor_new={stats['ancestor_new']} "
        f"~{stats['estimated_tokens']}tok (budget {limit})"
    )
    return snapshot, ancestor_new, stats


def compose_ancestor_summary(
    prior: List[Dict[str, Any]],
    newer: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    # 链式续接: 祖先摘要 + 本轮新增; 超上限丢最老 (线性增长封顶 → 链深亚线性)
    merged = list(prior or []) + list(newer or [])
    truncated = 0
    if len(merged) > _MAX_ANCESTOR_ENTRIES:
        truncated = len(merged) - _MAX_ANCESTOR_ENTRIES
        merged = merged[-_MAX_ANCESTOR_ENTRIES:]
    return merged, truncated
