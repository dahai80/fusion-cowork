"""UltraReview - P2-7.

Multi-agent code review:
  1. Collect: read target files (path list or git diff changeset)
  2. Review: multi-lens (security/correctness/style/tests) parallel LLM review, each producing findings
  3. Aggregate: merge + dedupe + sort by severity
  4. Synthesize: LLM generates a summary report

Degraded when LLM unavailable: returns a file listing + explanation, no findings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
MAX_FILE_CHARS = 8000
MAX_FILES = 20

LENS_DEFINITIONS: Dict[str, str] = {
    # i18n: user-facing
    "security": "安全视角: 注入/鉴权/敏感信息泄露/不安全反序列化/SSRF/路径穿越等 OWASP 风险",
    # i18n: user-facing
    "correctness": "正确性视角: 逻辑错误/边界条件/空值处理/异常吞没/竞态/资源泄漏",
    # i18n: user-facing
    "style": "风格视角: 命名/重复代码/过长函数/复杂度/可读性/与既有模式不一致",
    # i18n: user-facing
    "tests": "测试视角: 缺失测试/测试覆盖盲区/脆弱测试/错误原因通过的测试",
}


@dataclass
class ReviewFinding:
    file: str
    line: int
    severity: str
    category: str
    message: str
    lens: str = ""


@dataclass
class ReviewReport:
    target: str
    files_reviewed: List[str] = field(default_factory=list)
    findings: List[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    degraded: bool = False
    error: str = ""


async def run_ultra_review(
    paths: List[str],
    *,
    model: str = "",
    mlx_client: Any = None,
    lenses: List[str] | None = None,
) -> ReviewReport:
    """Run a multi-lens code review."""
    if not paths:
        # i18n: user-facing
        return ReviewReport(target="", error="未指定审查目标 (文件/目录)")

    chosen_lenses = lenses or list(LENS_DEFINITIONS.keys())
    invalid = [name for name in chosen_lenses if name not in LENS_DEFINITIONS]
    if invalid:
        # i18n: user-facing
        return ReviewReport(target="", error=f"未知审查视角: {invalid}")

    # i18n: user-facing
    target = paths[0] if len(paths) == 1 else f"{len(paths)} 个目标"
    report = ReviewReport(target=target)

    # 1. collect file contents
    file_contents = _collect_files(paths)
    if not file_contents:
        # i18n: user-facing
        report.error = "无可审查文件 (路径不存在或为空)"
        return report
    report.files_reviewed = list(file_contents.keys())
    logger.info(f"collected {len(file_contents)} files for review, lenses {chosen_lenses}")

    client = mlx_client
    if client is None:
        from ..ai.mlx_client import FusionMLXClient

        client = FusionMLXClient()
    resolved_model = await _resolve_model(client, model)

    # 2. parallel multi-lens review
    tasks = [_review_with_lens(client, resolved_model, lens, file_contents) for lens in chosen_lenses]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_findings: List[ReviewFinding] = []
    any_ok = False
    for lens, res in zip(chosen_lenses, results):
        if isinstance(res, list):
            all_findings.extend(res)
            any_ok = True
        else:
            logger.warning(f"lens {lens} review failed: {res}")

    if not any_ok:
        report.degraded = True
        report.summary = _fallback_summary(file_contents)
        logger.warning("all lens LLM reviews failed, degrading to file listing")
        return report

    # 3. aggregate + dedupe + sort
    report.findings = _aggregate(all_findings)

    # 4. synthesize
    summary = await _synthesize(client, resolved_model, report)
    if not summary:
        report.degraded = True
        report.summary = _fallback_summary(file_contents, report.findings)
    else:
        report.summary = summary

    return report


def _collect_files(paths: List[str]) -> Dict[str, str]:
    """Collect file path -> content, supports .py files and directory recursion."""
    out: Dict[str, str] = {}
    for p in paths:
        path = Path(p)
        if path.is_dir():
            for f in sorted(path.rglob("*.py")):
                if len(out) >= MAX_FILES:
                    break
                if _is_ignored(f):
                    continue
                content = _read_file(f)
                if content is not None:
                    out[str(f)] = content
        elif path.is_file():
            content = _read_file(path)
            if content is not None:
                out[str(path)] = content
    return dict(list(out.items())[:MAX_FILES])


def _read_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:MAX_FILE_CHARS]
    except Exception as e:
        logger.debug(f"read failed {path}: {e}")
        return None


def _is_ignored(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts & {".venv", "__pycache__", ".git", "node_modules", "build", "dist", ".eggs"})


async def _resolve_model(client: Any, model: str) -> str:
    if model:
        return model
    try:
        models = await client.list_models()
        if models:
            mid = models[0].get("id", models[0].get("model", ""))
            if mid:
                return mid
    except Exception as e:
        logger.debug(f"list_models failed: {e}")
    return "qwen3.5-9b"


async def _review_with_lens(client: Any, model: str, lens: str, file_contents: Dict[str, str]) -> List[ReviewFinding]:
    """Single-lens LLM review, returns a findings list."""
    context = _build_context(file_contents)
    # i18n: user-facing
    prompt = (
        f"你是代码审查专家, 以「{LENS_DEFINITIONS[lens]}」审查以下代码。\n"  # i18n: user-facing
        f'输出 JSON 数组, 每项 {{"file": "...", "line": 0, "severity": "critical|high|medium|low|info", '  # i18n: user-facing
        f'"category": "...", "message": "..."}}。无问题则输出 []。只输出 JSON。\n\n{context}'  # i18n: user-facing
    )
    try:
        resp = await client.chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2048,
        )
        return _parse_findings(resp.content.strip(), lens)
    except Exception as e:
        logger.warning(f"lens {lens} LLM call failed: {e}")
        raise


def _build_context(file_contents: Dict[str, str]) -> str:
    parts = []
    for path, content in file_contents.items():
        parts.append(f"### {path}\n```\n{content}\n```")
    return "\n\n".join(parts)


def _parse_findings(raw: str, lens: str) -> List[ReviewFinding]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return []
    out = []
    if not isinstance(data, list):
        return []
    for item in data[:50]:
        if not isinstance(item, dict):
            continue
        out.append(
            ReviewFinding(
                file=str(item.get("file", "")),
                line=int(item.get("line", 0) or 0),
                severity=str(item.get("severity", "info")).lower(),
                category=str(item.get("category", "")),
                message=str(item.get("message", "")),
                lens=lens,
            )
        )
    return out


def _aggregate(findings: List[ReviewFinding]) -> List[ReviewFinding]:
    """Dedupe (same file/line/category/message) + sort by severity."""
    seen = set()
    unique = []
    for f in findings:
        key = (f.file, f.line, f.category, f.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    unique.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 99))
    return unique


async def _synthesize(client: Any, model: str, report: ReviewReport) -> str:
    """LLM synthesizes the review summary."""
    findings_desc = "\n".join(
        f"- [{f.severity}] {f.file}:{f.line} ({f.lens}/{f.category}) {f.message}" for f in report.findings[:30]
    )
    # i18n: user-facing
    prompt = (
        f"审查了 {len(report.files_reviewed)} 个文件, 发现 {len(report.findings)} 个问题。\n"  # i18n: user-facing
        f"请生成简明审查总结: 整体评价 + 关键问题 + 修复建议。\n\n问题清单:\n{findings_desc}"  # i18n: user-facing
    )
    try:
        resp = await client.chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        return resp.content.strip()
    except Exception as e:
        logger.warning(f"synthesis LLM call failed: {e}")
        return ""


def _fallback_summary(file_contents: Dict[str, str], findings: List[ReviewFinding] | None = None) -> str:
    """Degraded summary: file listing + (if any) finding counts."""
    # i18n: user-facing
    lines = ["# 代码审查 (降级模式: LLM 不可用)", ""]
    # i18n: user-facing
    lines.append(f"审查文件: {len(file_contents)} 个")
    for p in file_contents:
        lines.append(f"- {p}")
    if findings:
        lines.append("")
        # i18n: user-facing
        lines.append(f"发现问题: {len(findings)} 个 (按 severity)")
        sev_count: Dict[str, int] = {}
        for f in findings:
            sev_count[f.severity] = sev_count.get(f.severity, 0) + 1
        for sev in sorted(sev_count, key=lambda s: SEVERITY_ORDER.get(s, 99)):
            lines.append(f"  - {sev}: {sev_count[sev]}")
    return "\n".join(lines)


def collect_git_diff_files(repo_root: str = ".") -> List[str]:
    """Collect .py files changed in git diff (helper CLI)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AM", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip().endswith(".py")]
        return files
    except Exception as e:
        logger.warning(f"git diff collection failed: {e}")
        return []
