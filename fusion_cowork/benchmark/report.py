"""ReportRenderer — renders CapabilityMatrix + BenchmarkResult into Markdown/HTML."""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .matrix import CapabilityLevel, CapabilityMatrix
from .runner import BenchmarkRunner

logger = logging.getLogger(__name__)

_LEVEL_ICON = {
    CapabilityLevel.NONE: "❌",
    CapabilityLevel.PARTIAL: "⚠️",
    CapabilityLevel.FULL: "✅",
    CapabilityLevel.ADVANCED: "⭐",
}


class ReportRenderer:
    """Comparison report renderer."""

    def __init__(self, matrix: Optional[CapabilityMatrix] = None, runner: Optional[BenchmarkRunner] = None):
        self._matrix = matrix or CapabilityMatrix()
        self._runner = runner

    def render_markdown(self) -> str:
        lines: List[str] = []
        s = self._matrix.summary()
        lines.append("# Claude Cowork vs Fusion-Cowork Capability Comparison Report")
        lines.append(f"\n> Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

        lines.append("## 📊 Overview\n")
        lines.append("| Metric | Value |")
        lines.append("|------|-----|")
        lines.append(f"| Total capabilities | {s['total_capabilities']} |")
        lines.append(f"| Desk FULL+ | {s['desk_full_or_above']} |")
        lines.append(f"| Desk ADVANCED | {s['desk_advanced']} |")
        lines.append(f"| Cowork FULL+ | {s['cowork_full_or_above']} |")
        lines.append(f"| Desk unique capabilities | {s['desk_unique']} |")
        lines.append(f"| Cowork unique capabilities | {s['cowork_unique']} |")
        lines.append(f"| Parity rate | {s['parity_score']} |")
        lines.append("")

        for cat in s["categories"]:
            caps = self._matrix.by_category(cat)
            lines.append(f"## {cat}\n")
            lines.append("| Capability | Cowork | Desk | Parity |")
            lines.append("|------|--------|------|--------|")
            for c in caps:
                cw = _LEVEL_ICON.get(c.cowork_level, "?") + " " + c.cowork_level.name
                dk = _LEVEL_ICON.get(c.desk_level, "?") + " " + c.desk_level.name
                lines.append(f"| {c.name} | {cw} | {dk} | {c.parity:.0%} |")
            lines.append("")

        desk_only = [c for c in self._matrix.list_all() if c.desk_level.value > 0 and c.cowork_level.value == 0]
        if desk_only:
            lines.append("## 🏆 Fusion-Cowork Unique Strengths\n")
            for c in desk_only:
                lines.append(f"- **{c.name}** — {c.desk_detail}")
            lines.append("")

        if self._runner and self._runner.results():
            lines.append("## ⏱️ Performance Benchmarks\n")
            bsum = self._runner.summary()
            lines.append("| Node | Avg(ms) | Min(ms) | Max(ms) | Runs |")
            lines.append("|------|----------|----------|----------|----------|")
            for name, st in bsum.get("nodes", {}).items():
                lines.append(f"| {name} | {st['avg_ms']} | {st['min_ms']} | {st['max_ms']} | {st['runs']} |")
            lines.append("")

        return "\n".join(lines)

    def render_html(self) -> str:
        md = self.render_markdown()
        body = _md_to_html_body(md)
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Cowork vs Fusion-Cowork Comparison Report</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:960px;margin:2em auto;padding:0 1em;color:#333}}
table{{border-collapse:collapse;width:100%;margin:1em 0}}
th,td{{border:1px solid #ddd;padding:8px 12px;text-align:left}}
th{{background:#f5f5f5;font-weight:600}}
h1{{color:#1a1a1a}}h2{{color:#333;border-bottom:2px solid #e0e0e0;padding-bottom:4px}}
blockquote{{border-left:4px solid #ddd;margin:1em 0;padding:0.5em 1em;color:#666}}
</style>
</head>
<body>
{body}
</body>
</html>"""

    def save(self, path: str, fmt: str = "markdown") -> str:
        fmt = fmt.lower()
        if fmt == "html":
            content = self.render_html()
        else:
            content = self.render_markdown()
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Comparison report saved: {path}")
        return path


def _md_to_html_body(md: str) -> str:
    import re

    lines = md.split("\n")
    html: List[str] = []
    in_table = False
    in_ul = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                html.append("<table>")
            in_table = True
            row = "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
            html.append(row)
            continue
        if in_table:
            html.append("</table>")
            in_table = False

        m = re.match(r"^(#{1,6})\s+(.+)", stripped)
        if m:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            lvl = len(m.group(1))
            html.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
            continue

        if stripped.startswith(">"):
            if in_ul:
                html.append("</ul>")
                in_ul = False
            html.append(f"<blockquote><p>{stripped[1:].strip()}</p></blockquote>")
            continue

        m = re.match(r"^- \*\*(.+?)\*\*\s*[—-]\s*(.+)", stripped)
        if m:
            if not in_ul:
                html.append("<ul>")
                in_ul = True
            html.append(f"<li><strong>{m.group(1)}</strong> — {m.group(2)}</li>")
            continue
        if in_ul and not stripped.startswith("-"):
            html.append("</ul>")
            in_ul = False

        if not stripped:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            html.append("")
            continue

        html.append(f"<p>{stripped}</p>")

    if in_table:
        html.append("</table>")
    if in_ul:
        html.append("</ul>")

    return "\n".join(html)
