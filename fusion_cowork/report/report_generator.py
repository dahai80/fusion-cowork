"""Batch report generator — renders workflow execution results into Markdown/HTML/PDF reports.

V0.2 features:
- workflow execution report (Markdown / HTML)
- batch report generation (multi-workflow summary)
- scheduled reports (auto-delivered to a target directory)
- report template engine (Jinja2)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ..engine.workflow import WorkflowExecution, WorkflowStatus

logger = logging.getLogger(__name__)


@dataclass
class ReportConfig:
    """Report configuration."""

    title: str = "Fusion-Cowork 自动化报告"  # i18n: user-facing
    author: str = "Fusion-Cowork"
    include_timeline: bool = True
    include_stats: bool = True
    include_logs: bool = True
    output_format: str = "markdown"  # markdown | html
    theme: str = "default"  # default | dark | light


class ReportGenerator:
    """Report generator — renders workflow execution results into readable reports."""

    def __init__(self, config: Optional[ReportConfig] = None):
        self.config = config or ReportConfig()

    # -- Single-workflow report --

    def generate_workflow_report(
        self,
        execution: WorkflowExecution,
        format: str = "",
    ) -> str:
        """Generate a single workflow execution report."""
        fmt = format or self.config.output_format

        if fmt == "html":
            return self._generate_html(execution)
        return self._generate_markdown(execution)

    def _generate_markdown(self, execution: WorkflowExecution) -> str:
        """Generate a Markdown report."""
        lines = []
        lines.append(f"# {self.config.title}")
        lines.append()
        lines.append(f"**工作流**: {execution.workflow_name}")  # i18n: user-facing
        lines.append(f"**状态**: {self._status_icon(execution.status)} {execution.status.value}")  # i18n: user-facing
        lines.append(  # i18n: user-facing
            f"**开始时间**: {datetime.fromtimestamp(execution.started_at).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if execution.completed_at:
            lines.append(
                f"**完成时间**: {datetime.fromtimestamp(execution.completed_at).strftime('%Y-%m-%d %H:%M:%S')}"  # i18n: user-facing
            )
            lines.append(f"**总耗时**: {execution.total_time:.2f}s")  # i18n: user-facing
        if execution.error:
            lines.append(f"**错误**: {execution.error}")  # i18n: user-facing
        lines.append()

        if execution.result_summary:
            lines.append(f"**摘要**: {execution.result_summary}")  # i18n: user-facing
            lines.append()

        if self.config.include_stats and execution.steps:
            lines.append("## 执行步骤")  # i18n: user-facing
            lines.append()
            lines.append("| 节点 | 状态 | 耗时 | 摘要 |")  # i18n: user-facing
            lines.append("|------|------|------|------|")
            for step in execution.steps:
                icon = self._status_icon(step.status)
                summary = (step.summary[:50] + "...") if len(step.summary) > 50 else step.summary
                lines.append(
                    f"| {step.node_display_name} | {icon} {step.status.value} | {step.execution_time:.2f}s | {summary} |"
                )
            lines.append()

        # Statistics
        if self.config.include_stats:
            success = sum(1 for s in execution.steps if s.status.value == "success")
            failed = sum(1 for s in execution.steps if s.status.value == "failed")
            total = len(execution.steps)
            lines.append(  # i18n: user-facing
                f"**统计**: {success}/{total} 节点成功" + (f", {failed} 失败" if failed else "")
            )
            lines.append()

        lines.append("---")
        lines.append(  # i18n: user-facing
            f"*由 Fusion-Cowork 于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 自动生成*"
        )
        lines.append()

        return "\n".join(lines)

    def _generate_html(self, execution: WorkflowExecution) -> str:
        """Generate an HTML report."""
        _markdown = self._generate_markdown(execution)
        # Simple Markdown-to-HTML conversion
        import html as html_mod

        html_lines = ["<!DOCTYPE html><html><head><meta charset='utf-8'>"]
        html_lines.append(f"<title>{html_mod.escape(self.config.title)}</title>")
        html_lines.append("<style>")
        html_lines.append(
            "body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }"
        )
        html_lines.append("h1 { color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }")
        html_lines.append("h2 { color: #16213e; margin-top: 30px; }")
        html_lines.append("table { border-collapse: collapse; width: 100%; margin: 10px 0; }")
        html_lines.append("th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }")
        html_lines.append("th { background: #f5f5f7; }")
        html_lines.append(".success { color: #4caf50; } .failed { color: #f44336; } .running { color: #2196f3; }")
        html_lines.append("</style></head><body>")
        html_lines.append(f"<h1>{html_mod.escape(self.config.title)}</h1>")
        html_lines.append(  # i18n: user-facing
            f"<p><strong>工作流:</strong> {html_mod.escape(execution.workflow_name)}</p>"
        )
        html_lines.append(
            f"<p><strong>状态:</strong> <span class='{execution.status.value}'>{execution.status.value}</span></p>"  # i18n: user-facing
        )
        html_lines.append(f"<p><strong>耗时:</strong> {execution.total_time:.2f}s</p>")  # i18n: user-facing

        if execution.steps:
            html_lines.append("<h2>执行步骤</h2>")  # i18n: user-facing
            html_lines.append(  # i18n: user-facing
                "<table><tr><th>节点</th><th>状态</th><th>耗时</th><th>摘要</th></tr>"
            )
            for step in execution.steps:
                html_lines.append(
                    f"<tr><td>{html_mod.escape(step.node_display_name)}</td>"
                    f"<td class='{step.status.value}'>{step.status.value}</td>"
                    f"<td>{step.execution_time:.2f}s</td>"
                    f"<td>{html_mod.escape(step.summary[:80])}</td></tr>"
                )
            html_lines.append("</table>")

        html_lines.append(
            f"<p><em>由 Fusion-Cowork 于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 自动生成</em></p>"  # i18n: user-facing
        )
        html_lines.append("</body></html>")
        return "\n".join(html_lines)

    # -- Batch report --

    def generate_batch_report(
        self,
        executions: List[WorkflowExecution],
        title: str = "",
    ) -> str:
        """Generate a batch report (multi-workflow summary)."""
        title = title or f"批量报告 ({len(executions)} 个工作流)"  # i18n: user-facing
        lines = []
        lines.append(f"# {title}")
        lines.append()
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")  # i18n: user-facing
        lines.append(f"**工作流总数**: {len(executions)}")  # i18n: user-facing
        lines.append()

        # Summary statistics
        success = sum(1 for e in executions if e.status == WorkflowStatus.SUCCESS)
        failed = sum(1 for e in executions if e.status == WorkflowStatus.FAILED)
        total_time = sum(e.total_time for e in executions)
        lines.append("## 汇总统计")  # i18n: user-facing
        lines.append()
        lines.append("| 指标 | 值 |")  # i18n: user-facing
        lines.append("|------|-----|")
        lines.append(f"| 成功 | {success} |")  # i18n: user-facing
        lines.append(f"| 失败 | {failed} |")  # i18n: user-facing
        lines.append(f"| 总耗时 | {total_time:.2f}s |")  # i18n: user-facing
        lines.append(f"| 平均耗时 | {(total_time / max(len(executions), 1)):.2f}s |")  # i18n: user-facing
        lines.append()

        # Per-workflow details
        lines.append("## 工作流详情")  # i18n: user-facing
        lines.append()
        for i, execution in enumerate(executions, 1):
            icon = "✅" if execution.status == WorkflowStatus.SUCCESS else "❌"
            lines.append(f"### {i}. {icon} {execution.workflow_name}")
            lines.append()
            lines.append(f"- **状态**: {execution.status.value}")  # i18n: user-facing
            lines.append(f"- **耗时**: {execution.total_time:.2f}s")  # i18n: user-facing
            if execution.error:
                lines.append(f"- **错误**: {execution.error}")  # i18n: user-facing
            if execution.result_summary:
                lines.append(f"- **摘要**: {execution.result_summary}")  # i18n: user-facing
            lines.append()

        lines.append("---")
        lines.append(  # i18n: user-facing
            f"*由 Fusion-Cowork 批量报告生成器于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 自动生成*"
        )
        return "\n".join(lines)

    # -- Save report --

    def save_report(
        self,
        content: str,
        output_path: str,
        filename: str = "",
    ) -> str:
        """Save the report to a file."""
        path = Path(output_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)

        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = ".html" if "<html" in content else ".md"
            filename = f"report_{timestamp}{ext}"

        filepath = path / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"Report saved: {filepath}")
        return str(filepath)

    def _status_icon(self, status) -> str:
        """Get the status icon."""
        icons = {
            "success": "✅",
            "failed": "❌",
            "running": "🔄",
            "pending": "⏳",
            "cancelled": "⏹️",
        }
        return icons.get(status.value if hasattr(status, "value") else str(status), "❓")
