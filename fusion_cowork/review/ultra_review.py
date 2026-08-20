"""UltraReview — P2-7。

多 Agent 代码审查:
  1. 收集: 读取目标文件 (路径列表或 git diff 变更集)
  2. 审查: 多视角 (security/correctness/style/tests) 并行 LLM 审查, 各产出 findings
  3. 聚合: 合并 + 去重 + 按 severity 排序
  4. 合成: LLM 生成总结报告

LLM 不可用时降级: 返回文件清单 + 说明, 不产出 findings。
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
    "security": "安全视角: 注入/鉴权/敏感信息泄露/不安全反序列化/SSRF/路径穿越等 OWASP 风险",
    "correctness": "正确性视角: 逻辑错误/边界条件/空值处理/异常吞没/竞态/资源泄漏",
    "style": "风格视角: 命名/重复代码/过长函数/复杂度/可读性/与既有模式不一致",
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
    """运行多视角代码审查。"""
    if not paths:
        return ReviewReport(target="", error="未指定审查目标 (文件/目录)")

    chosen_lenses = lenses or list(LENS_DEFINITIONS.keys())
    invalid = [name for name in chosen_lenses if name not in LENS_DEFINITIONS]
    if invalid:
        return ReviewReport(target="", error=f"未知审查视角: {invalid}")

    target = paths[0] if len(paths) == 1 else f"{len(paths)} 个目标"
    report = ReviewReport(target=target)

    # 1. 收集文件内容
    file_contents = _collect_files(paths)
    if not file_contents:
        report.error = "无可审查文件 (路径不存在或为空)"
        return report
    report.files_reviewed = list(file_contents.keys())
    logger.info(f"审查收集 {len(file_contents)} 个文件, 视角 {chosen_lenses}")

    client = mlx_client
    if client is None:
        from ..ai.mlx_client import FusionMLXClient

        client = FusionMLXClient()
    resolved_model = await _resolve_model(client, model)

    # 2. 并行多视角审查
    tasks = [_review_with_lens(client, resolved_model, lens, file_contents) for lens in chosen_lenses]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_findings: List[ReviewFinding] = []
    any_ok = False
    for lens, res in zip(chosen_lenses, results):
        if isinstance(res, list):
            all_findings.extend(res)
            any_ok = True
        else:
            logger.warning(f"视角 {lens} 审查失败: {res}")

    if not any_ok:
        report.degraded = True
        report.summary = _fallback_summary(file_contents)
        logger.warning("全部视角 LLM 审查失败, 降级返回文件清单")
        return report

    # 3. 聚合 + 去重 + 排序
    report.findings = _aggregate(all_findings)

    # 4. 合成
    summary = await _synthesize(client, resolved_model, report)
    if not summary:
        report.degraded = True
        report.summary = _fallback_summary(file_contents, report.findings)
    else:
        report.summary = summary

    return report


def _collect_files(paths: List[str]) -> Dict[str, str]:
    """收集文件路径→内容, 支持 .py 文件与目录递归。"""
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
        logger.debug(f"读取失败 {path}: {e}")
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
        logger.debug(f"list_models 失败: {e}")
    return "qwen3.5-9b"


async def _review_with_lens(client: Any, model: str, lens: str, file_contents: Dict[str, str]) -> List[ReviewFinding]:
    """单视角 LLM 审查, 返回 findings 列表。"""
    context = _build_context(file_contents)
    prompt = (
        f"你是代码审查专家, 以「{LENS_DEFINITIONS[lens]}」审查以下代码。\n"
        f'输出 JSON 数组, 每项 {{"file": "...", "line": 0, "severity": "critical|high|medium|low|info", '
        f'"category": "...", "message": "..."}}。无问题则输出 []。只输出 JSON。\n\n{context}'
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
        logger.warning(f"视角 {lens} LLM 调用失败: {e}")
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
    """去重 (同 file/line/category/message) + 按 severity 排序。"""
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
    """LLM 合成审查总结。"""
    findings_desc = "\n".join(
        f"- [{f.severity}] {f.file}:{f.line} ({f.lens}/{f.category}) {f.message}" for f in report.findings[:30]
    )
    prompt = (
        f"审查了 {len(report.files_reviewed)} 个文件, 发现 {len(report.findings)} 个问题。\n"
        f"请生成简明审查总结: 整体评价 + 关键问题 + 修复建议。\n\n问题清单:\n{findings_desc}"
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
        logger.warning(f"合成 LLM 调用失败: {e}")
        return ""


def _fallback_summary(file_contents: Dict[str, str], findings: List[ReviewFinding] | None = None) -> str:
    """降级总结: 文件清单 + (若有) 问题计数。"""
    lines = ["# 代码审查 (降级模式: LLM 不可用)", ""]
    lines.append(f"审查文件: {len(file_contents)} 个")
    for p in file_contents:
        lines.append(f"- {p}")
    if findings:
        lines.append("")
        lines.append(f"发现问题: {len(findings)} 个 (按 severity)")
        sev_count: Dict[str, int] = {}
        for f in findings:
            sev_count[f.severity] = sev_count.get(f.severity, 0) + 1
        for sev in sorted(sev_count, key=lambda s: SEVERITY_ORDER.get(s, 99)):
            lines.append(f"  - {sev}: {sev_count[sev]}")
    return "\n".join(lines)


def collect_git_diff_files(repo_root: str = ".") -> List[str]:
    """收集 git diff 变更的 .py 文件 (辅助 CLI)。"""
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
        logger.warning(f"git diff 收集失败: {e}")
        return []
