"""深度研究 — P2-6。

多 Agent 编排:
  1. 规划: LLM 将问题分解为 N 个子问题
  2. 搜索: 对每个子问题并行 web 搜索 (DuckDuckGo Lite) + 抓取摘要
  3. 合成: LLM 汇总为带引用的 Markdown 报告

LLM 不可用时降级: 跳过规划 (原问题直接搜索) + 跳过合成 (返回原始发现)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 10.0
_FETCH_TIMEOUT = 15.0
_MAX_FETCH_CHARS = 4000
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0"
_LINK_RE = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_SNIPPET_RE = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)


@dataclass
class ResearchSubQuestion:
    question: str
    rationale: str = ""


@dataclass
class ResearchFinding:
    sub_question: str
    title: str
    url: str
    snippet: str = ""


@dataclass
class ResearchReport:
    question: str
    sub_questions: List[ResearchSubQuestion] = field(default_factory=list)
    findings: List[ResearchFinding] = field(default_factory=list)
    synthesis: str = ""
    sources_used: int = 0
    degraded: bool = False
    error: str = ""


async def run_deep_research(
    question: str,
    *,
    depth: int = 3,
    max_sources: int = 3,
    model: str = "",
    mlx_client: Any = None,
) -> ResearchReport:
    """运行深度研究, 返回带引用的报告。"""
    if not question:
        return ResearchReport(question="", error="研究问题不能为空")

    report = ResearchReport(question=question)
    client = mlx_client
    if client is None:
        from ..ai.mlx_client import FusionMLXClient

        client = FusionMLXClient()

    resolved_model = await _resolve_model(client, model)
    logger.info(f"深度研究启动: question={question!r} model={resolved_model} depth={depth}")

    # 1. 规划: 分解子问题
    sub_qs = await _plan(client, resolved_model, question, depth)
    if not sub_qs:
        report.degraded = True
        logger.warning("规划失败/无 LLM, 退化为单问题搜索")
        sub_qs = [ResearchSubQuestion(question=question, rationale="降级: 原问题直接搜索")]
    report.sub_questions = sub_qs

    # 2. 搜索: 并行搜索每个子问题
    findings = await _search_all(sub_qs, max_sources)
    report.findings = findings
    report.sources_used = len(findings)
    logger.info(f"搜索完成: {len(findings)} 条发现")

    if not findings:
        report.error = "未找到任何搜索结果"
        return report

    # 3. 合成: LLM 汇总报告
    synthesis = await _synthesize(client, resolved_model, question, findings)
    if not synthesis:
        report.degraded = True
        report.synthesis = _fallback_synthesis(question, findings)
        logger.warning("合成失败/无 LLM, 使用降级汇总")
    else:
        report.synthesis = synthesis

    return report


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


async def _plan(client: Any, model: str, question: str, depth: int) -> List[ResearchSubQuestion]:
    """LLM 分解问题为子问题列表。"""
    prompt = (
        f"将以下研究问题分解为 {depth} 个可独立搜索的子问题, 输出 JSON 数组, "
        f'每个元素为 {{"question": "...", "rationale": "..."}}。只输出 JSON。\n\n问题: {question}'
    )
    try:
        resp = await client.chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        raw = resp.content.strip()
        subs = _parse_subquestions(raw)
        if subs:
            logger.info(f"规划出 {len(subs)} 个子问题")
        return subs
    except Exception as e:
        logger.warning(f"规划 LLM 调用失败: {e}")
        return []


def _parse_subquestions(raw: str) -> List[ResearchSubQuestion]:
    """从 LLM 输出解析子问题 JSON, 容错去除 markdown 包裹。"""
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
    for item in data[:10]:
        if isinstance(item, dict) and item.get("question"):
            out.append(ResearchSubQuestion(question=str(item["question"]), rationale=str(item.get("rationale", ""))))
        elif isinstance(item, str):
            out.append(ResearchSubQuestion(question=item))
    return out


async def _search_all(sub_qs: List[ResearchSubQuestion], max_sources: int) -> List[ResearchFinding]:
    """并行搜索所有子问题。"""
    tasks = [_search_one(sq, max_sources) for sq in sub_qs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    findings: List[ResearchFinding] = []
    for r in results:
        if isinstance(r, list):
            findings.extend(r)
    return findings


async def _search_one(sq: ResearchSubQuestion, max_sources: int) -> List[ResearchFinding]:
    """单个子问题: 搜索 + 抓取 top 结果摘要。"""
    results = await _ddg_search(sq.question, max_sources)
    findings = []
    for r in results:
        snippet = r.get("snippet", "")
        if r.get("url"):
            content = await _fetch_text(r["url"])
            if content:
                snippet = content[:_MAX_FETCH_CHARS]
        findings.append(
            ResearchFinding(sub_question=sq.question, title=r.get("title", ""), url=r.get("url", ""), snippet=snippet)
        )
    return findings


async def _ddg_search(query: str, max_results: int) -> List[Dict[str, str]]:
    """DuckDuckGo Lite 搜索, 返回 [{title,url,snippet}]。"""
    try:
        async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/", params={"q": query}, headers={"User-Agent": _UA}
            )
            resp.raise_for_status()
            html = resp.text
            links = _LINK_RE.findall(html)
            snippets = _SNIPPET_RE.findall(html)
            import html as html_mod

            out = []
            for i, (url, title) in enumerate(links[:max_results]):
                snippet = ""
                if i < len(snippets):
                    snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[i]).strip())
                out.append(
                    {
                        "title": html_mod.unescape(re.sub(r"<[^>]+>", "", title).strip()),
                        "url": url,
                        "snippet": snippet,
                    }
                )
            return out
    except Exception as e:
        logger.warning(f"搜索失败 query={query!r}: {e}")
        return []


async def _fetch_text(url: str) -> str:
    """抓取 URL 并提取纯文本。"""
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA})
            resp.raise_for_status()
            if "text/html" not in resp.headers.get("content-type", ""):
                return ""
            text = re.sub(r"<script[^>]*>.*?</script>", "", resp.text, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text
    except Exception as e:
        logger.debug(f"抓取失败 url={url}: {e}")
        return ""


async def _synthesize(client: Any, model: str, question: str, findings: List[ResearchFinding]) -> str:
    """LLM 合成带引用的研究报告。"""
    context_parts = []
    for i, f in enumerate(findings, 1):
        context_parts.append(f"[{i}] ({f.sub_question}) {f.title}\nURL: {f.url}\n{f.snippet[:800]}")
    context = "\n\n".join(context_parts)
    prompt = (
        f"基于以下搜索发现, 撰写关于「{question}」的研究报告。\n"
        f"要求: 结构化 (要点 + 结论), 关键论断用 [n] 标注引用编号。\n\n{context}"
    )
    try:
        resp = await client.chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=2048,
        )
        return resp.content.strip()
    except Exception as e:
        logger.warning(f"合成 LLM 调用失败: {e}")
        return ""


def _fallback_synthesis(question: str, findings: List[ResearchFinding]) -> str:
    """无 LLM 时的降级汇总: 按子问题罗列发现。"""
    lines = [f"# 研究报告: {question}", "", ">(降级模式: LLM 不可用, 仅汇总原始搜索发现)", ""]
    by_sq: Dict[str, List[ResearchFinding]] = {}
    for f in findings:
        by_sq.setdefault(f.sub_question, []).append(f)
    for sq, items in by_sq.items():
        lines.append(f"## {sq}")
        for i, f in enumerate(items, 1):
            lines.append(f"{i}. [{f.title}]({f.url})")
            if f.snippet:
                lines.append(f"   > {f.snippet[:200]}")
        lines.append("")
    return "\n".join(lines)
