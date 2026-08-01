"""批量报表生成器 — 将工作流执行结果生成 Markdown/HTML/PDF 报告。

V0.2 特性：
- 工作流执行报告（Markdown / HTML）
- 批量报表生成（多工作流汇总）
- 定时报表（自动发送到指定目录）
- 报告模板引擎（Jinja2）
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
    """报告配置。"""
    title: str = "Fusion-Cowork 自动化报告"
    author: str = "Fusion-Cowork"
    include_timeline: bool = True
    include_stats: bool = True
    include_logs: bool = True
    output_format: str = "markdown"  # markdown | html
    theme: str = "default"  # default | dark | light


class ReportGenerator:
    """报表生成器 — 将工作流执行结果转换为可读报告。"""

    def __init__(self, config: Optional[ReportConfig] = None):
        self.config = config or ReportConfig()

    # ── 单工作流报告 ──

    def generate_workflow_report(
        self,
        execution: WorkflowExecution,
        format: str = "",
    ) -> str:
        """生成单个工作流执行报告。"""
        fmt = format or self.config.output_format

        if fmt == "html":
            return self._generate_html(execution)
        return self._generate_markdown(execution)

    def _generate_markdown(self, execution: WorkflowExecution) -> str:
        """生成 Markdown 报告。"""
        lines = []
        lines.append(f"# {self.config.title}")
        lines.append()
        lines.append(f"**工作流**: {execution.workflow_name}")
        lines.append(f"**状态**: {self._status_icon(execution.status)} {execution.status.value}")
        lines.append(f"**开始时间**: {datetime.fromtimestamp(execution.started_at).strftime('%Y-%m-%d %H:%M:%S')}")
        if execution.completed_at:
            lines.append(f"**完成时间**: {datetime.fromtimestamp(execution.completed_at).strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"**总耗时**: {execution.total_time:.2f}s")
        if execution.error:
            lines.append(f"**错误**: {execution.error}")
        lines.append()

        if execution.result_summary:
            lines.append(f"**摘要**: {execution.result_summary}")
            lines.append()

        if self.config.include_stats and execution.steps:
            lines.append("## 执行步骤")
            lines.append()
            lines.append("| 节点 | 状态 | 耗时 | 摘要 |")
            lines.append("|------|------|------|------|")
            for step in execution.steps:
                icon = self._status_icon(step.status)
                summary = (step.summary[:50] + "...") if len(step.summary) > 50 else step.summary
                lines.append(f"| {step.node_display_name} | {icon} {step.status.value} | {step.execution_time:.2f}s | {summary} |")
            lines.append()

        # 统计数据
        if self.config.include_stats:
            success = sum(1 for s in execution.steps if s.status.value == "success")
            failed = sum(1 for s in execution.steps if s.status.value == "failed")
            total = len(execution.steps)
            lines.append(f"**统计**: {success}/{total} 节点成功" + (f", {failed} 失败" if failed else ""))
            lines.append()

        lines.append("---")
        lines.append(f"*由 Fusion-Cowork 于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 自动生成*")
        lines.append()

        return "\n".join(lines)

    def _generate_html(self, execution: WorkflowExecution) -> str:
        """生成 HTML 报告。"""
        _markdown = self._generate_markdown(execution)
        # 简单 Markdown 转 HTML
        import html as html_mod

        html_lines = ["<!DOCTYPE html><html><head><meta charset='utf-8'>"]
        html_lines.append(f"<title>{html_mod.escape(self.config.title)}</title>")
        html_lines.append("<style>")
        html_lines.append("body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }")
        html_lines.append("h1 { color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }")
        html_lines.append("h2 { color: #16213e; margin-top: 30px; }")
        html_lines.append("table { border-collapse: collapse; width: 100%; margin: 10px 0; }")
        html_lines.append("th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }")
        html_lines.append("th { background: #f5f5f7; }")
        html_lines.append(".success { color: #4caf50; } .failed { color: #f44336; } .running { color: #2196f3; }")
        html_lines.append("</style></head><body>")
        html_lines.append(f"<h1>{html_mod.escape(self.config.title)}</h1>")
        html_lines.append(f"<p><strong>工作流:</strong> {html_mod.escape(execution.workflow_name)}</p>")
        html_lines.append(f"<p><strong>状态:</strong> <span class='{execution.status.value}'>{execution.status.value}</span></p>")
        html_lines.append(f"<p><strong>耗时:</strong> {execution.total_time:.2f}s</p>")

        if execution.steps:
            html_lines.append("<h2>执行步骤</h2>")
            html_lines.append("<table><tr><th>节点</th><th>状态</th><th>耗时</th><th>摘要</th></tr>")
            for step in execution.steps:
                html_lines.append(f"<tr><td>{html_mod.escape(step.node_display_name)}</td>"
                                f"<td class='{step.status.value}'>{step.status.value}</td>"
                                f"<td>{step.execution_time:.2f}s</td>"
                                f"<td>{html_mod.escape(step.summary[:80])}</td></tr>")
            html_lines.append("</table>")

        html_lines.append(f"<p><em>由 Fusion-Cowork 于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 自动生成</em></p>")
        html_lines.append("</body></html>")
        return "\n".join(html_lines)

    # ── 批量报告 ──

    def generate_batch_report(
        self,
        executions: List[WorkflowExecution],
        title: str = "",
    ) -> str:
        """生成批量报告（多工作流汇总）。"""
        title = title or f"批量报告 ({len(executions)} 个工作流)"
        lines = []
        lines.append(f"# {title}")
        lines.append()
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**工作流总数**: {len(executions)}")
        lines.append()

        # 汇总统计
        success = sum(1 for e in executions if e.status == WorkflowStatus.SUCCESS)
        failed = sum(1 for e in executions if e.status == WorkflowStatus.FAILED)
        total_time = sum(e.total_time for e in executions)
        lines.append("## 汇总统计")
        lines.append()
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 成功 | {success} |")
        lines.append(f"| 失败 | {failed} |")
        lines.append(f"| 总耗时 | {total_time:.2f}s |")
        lines.append(f"| 平均耗时 | {(total_time / max(len(executions), 1)):.2f}s |")
        lines.append()

        # 每个工作流详情
        lines.append("## 工作流详情")
        lines.append()
        for i, execution in enumerate(executions, 1):
            icon = "✅" if execution.status == WorkflowStatus.SUCCESS else "❌"
            lines.append(f"### {i}. {icon} {execution.workflow_name}")
            lines.append()
            lines.append(f"- **状态**: {execution.status.value}")
            lines.append(f"- **耗时**: {execution.total_time:.2f}s")
            if execution.error:
                lines.append(f"- **错误**: {execution.error}")
            if execution.result_summary:
                lines.append(f"- **摘要**: {execution.result_summary}")
            lines.append()

        lines.append("---")
        lines.append(f"*由 Fusion-Cowork 批量报告生成器于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 自动生成*")
        return "\n".join(lines)

    # ── 保存报告 ──

    def save_report(
        self,
        content: str,
        output_path: str,
        filename: str = "",
    ) -> str:
        """保存报告到文件。"""
        path = Path(output_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)

        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = ".html" if "<html" in content else ".md"
            filename = f"report_{timestamp}{ext}"

        filepath = path / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"报告已保存: {filepath}")
        return str(filepath)

    def _status_icon(self, status) -> str:
        """获取状态图标。"""
        icons = {
            "success": "✅",
            "failed": "❌",
            "running": "🔄",
            "pending": "⏳",
            "cancelled": "⏹️",
        }
        return icons.get(status.value if hasattr(status, 'value') else str(status), "❓")
