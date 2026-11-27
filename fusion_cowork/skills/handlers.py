from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

Handler = Callable[[Dict[str, Any], Any], Awaitable[Dict[str, Any]]]

_HANDLERS: Dict[str, Handler] = {}


def get_handler(name: str) -> Optional[Handler]:
    return _HANDLERS.get(name)


def register_handler(name: str, fn: Handler) -> None:
    _HANDLERS[name] = fn


def _resolve_model(params: Dict[str, Any]) -> str:
    return str(params.get("model") or os.environ.get("FUSION_MLX_MODEL") or "qwen3.5-9b")


def _read_contract(path: Path, scope) -> str:
    if not scope.ensure_allowed(path):
        raise PermissionError(f"Sandbox rejected out-of-scope read: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"Contract file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


async def _playbook_clauses(kb_id: str, query: str, top_k: int) -> tuple[list[Dict[str, Any]], str]:
    if not kb_id:
        # i18n: user-facing
        return [], "未配置知识库 (kb_id 为空), 审查要点仅来自指令"
    from ..ai.mlx_client import KBClient

    try:
        async with KBClient() as kb:
            hits = await kb.search(kb_id, query, top_k=top_k)
        return list(hits or []), ""
    except Exception as e:
        logger.warning(f"KB retrieval failed (continuing local review): {e}")
        # i18n: user-facing
        return [], f"知识库检索失败: {e}"


async def contract_review(params: Dict[str, Any], package: Any) -> Dict[str, Any]:
    from ..ai.mlx_client import FusionMLXClient
    from ..security import get_scoped_folder_manager

    scope = get_scoped_folder_manager()
    contract_path = Path(str(params.get("contract_path", ""))).expanduser()
    contract_text = _read_contract(contract_path, scope)
    hits, kb_note = await _playbook_clauses(
        str(params.get("kb_id", "")),
        str(params.get("playbook_query", "contract review checkpoints")),
        int(params.get("top_k", 5) or 5),
    )
    clauses = []
    for i, hit in enumerate(hits, start=1):
        text = str(hit.get("text") or hit.get("content") or "").strip()
        source = str(hit.get("source") or hit.get("doc_id") or hit.get("id") or "playbook")
        clauses.append(f"[P{i}] ({source}) {text}")
    citations = "\n".join(clauses) if clauses else "(no KB hits)"

    prompt = (
        f"{package.instructions}\n\n"
        f"## Output template\n{package.output_template}\n\n"
        f"## Review checkpoints (KB)\n{citations}\n\n"
        f"## Full contract text\n{contract_text}"
    )
    async with FusionMLXClient() as llm:
        resp = await llm.chat(
            model=_resolve_model(params),
            messages=[
                {"role": "system", "content": "You are a contract review assistant; answer only based on the given contract and review checkpoints, citing checkpoint numbers item by item."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=int(params.get("max_tokens", 4096) or 4096),
        )
    memo = resp.content if hasattr(resp, "content") else str(resp)

    out_dir = Path(str(params.get("output_dir") or contract_path.parent)).expanduser()
    out_path = out_dir / f"{contract_path.stem}.memo.md"
    if not scope.ensure_allowed(out_path):
        raise PermissionError(f"Sandbox rejected out-of-scope write: {out_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # i18n: user-facing
    header = f"# 合同审查备忘: {contract_path.name}\n\n"
    if kb_note:
        header += f"> {kb_note}\n\n"
    out_path.write_text(header + memo, encoding="utf-8")
    logger.info(f"contract-review done: {contract_path} -> {out_path} (clauses={len(hits)})")
    return {
        "status": "success",
        # i18n: user-facing
        "summary": f"合同审查完成, 输出 {out_path.name} (引用 {len(hits)} 条要点)",
        "data": {
            "memo_path": str(out_path),
            "clauses": len(hits),
            "kb_note": kb_note,
            "contract_chars": len(contract_text),
        },
    }


def _lifecycle_events(source_dir: Path, cutoff: float) -> tuple[list[Dict[str, Any]], int]:
    events: list[Dict[str, Any]] = []
    skipped = 0
    for path in sorted(source_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            logger.warning(f"Failed to read lifecycle log {path}: {e}")
            skipped += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if float(ev.get("ts", 0) or 0) >= cutoff:
                events.append(ev)
    return events, skipped


def _is_failure(ev: Dict[str, Any]) -> bool:
    name = str(ev.get("event", "")).lower()
    payload = ev.get("payload") or {}
    status = str(payload.get("status", "")).lower()
    return "fail" in name or "error" in name or status in ("failed", "blocked", "error", "canceled")


async def weekly_readout(params: Dict[str, Any], package: Any) -> Dict[str, Any]:
    days = int(params.get("days") or 7)
    source_dir = Path(
        str(params.get("source_dir") or os.environ.get("FUSION_COWORK_LIFECYCLE_DIR") or "~/.fusion-cowork/lifecycle")
    ).expanduser()
    out_dir = Path(str(params.get("output_dir") or "~/.fusion-cowork/reports")).expanduser()
    cutoff = time.time() - days * 86400
    events, skipped = ([], 0) if not source_dir.is_dir() else _lifecycle_events(source_dir, cutoff)

    by_event = Counter(str(ev.get("event", "") or "?") for ev in events)
    by_domain = Counter(str(ev.get("domain", "") or "?") for ev in events)
    failures = [ev for ev in events if _is_failure(ev)]
    runs = [ev for ev in events if str(ev.get("event", "")).lower() in ("run_end", "workflow_end", "run_finished")]

    lines = [
        # i18n: user-facing
        f"# 周报 ({days} 天)",
        "",
        # i18n: user-facing
        f"- 事件总数: {len(events)}",
        # i18n: user-facing
        f"- 运行结束: {len(runs)}",
        # i18n: user-facing
        f"- 失败/阻断: {len(failures)}",
        # i18n: user-facing
        f"- 跳过坏行: {skipped}",
        "",
        # i18n: user-facing
        "## 按域",
    ]
    # i18n: user-facing
    lines += [f"- {name}: {count}" for name, count in by_domain.most_common()] or ["- (无数据)"]
    # i18n: user-facing
    lines += ["", "## 事件 Top"]
    # i18n: user-facing
    lines += [f"- {name}: {count}" for name, count in by_event.most_common(10)] or ["- (无数据)"]
    # i18n: user-facing
    lines += ["", "## 失败项"]
    lines += [
        f"- {ev.get('event')} domain={ev.get('domain')} session={ev.get('session_id') or '-'}" for ev in failures[:20]
        # i18n: user-facing
    ] or ["- (无)"]

    narrative = ""
    if params.get("narrative"):
        from ..ai.mlx_client import FusionMLXClient

        try:
            async with FusionMLXClient() as llm:
                resp = await llm.chat(
                    model=_resolve_model(params),
                    messages=[
                        {"role": "system", "content": package.instructions or "Write a brief weekly report summary based on the statistics."},
                        {"role": "user", "content": "\n".join(lines)},
                    ],
                    temperature=0.3,
                    max_tokens=800,
                )
            narrative = resp.content if hasattr(resp, "content") else str(resp)
        except Exception as e:
            logger.warning(f"weekly-readout narrative generation failed (keeping statistics): {e}")
            # i18n: user-facing
            narrative = f"(叙述生成失败: {e})"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"weekly-readout-{time.strftime('%Y%m%d')}.md"
    body = "\n".join(lines) + "\n"
    if narrative:
        # i18n: user-facing
        body += f"\n## 摘要\n{narrative}\n"
    out_path.write_text(body, encoding="utf-8")
    logger.info(f"weekly-readout done: {out_path} (events={len(events)}, failures={len(failures)})")
    return {
        "status": "success",
        # i18n: user-facing
        "summary": f"周报生成: {out_path.name} (事件 {len(events)}, 失败 {len(failures)})",
        "data": {
            "report_path": str(out_path),
            "events": len(events),
            "runs": len(runs),
            "failures": len(failures),
            "narrative": bool(narrative),
        },
    }


register_handler("contract_review", contract_review)
register_handler("weekly_readout", weekly_readout)
