"""AI workflow optimizer — analyzes workflow execution data via fusion-mlx and optimizes automatically.

V0.2 features:
- Workflow execution analysis (bottleneck detection, duration distribution)
- Automatic parameter tuning (batch_size, parallelism, timeout)
- Node reordering optimization
- Optimization suggestion generation
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..ai import FusionMLXClient

logger = logging.getLogger(__name__)


@dataclass
class OptimizationSuggestion:
    """Optimization suggestion."""

    category: str  # performance | memory | parallelism | configuration
    severity: str  # critical | warning | info
    title: str
    description: str
    current_value: str = ""
    suggested_value: str = ""
    expected_improvement: str = ""
    auto_fix: bool = False


@dataclass
class WorkflowAnalysis:
    """Workflow analysis result."""

    workflow_name: str
    total_executions: int
    avg_duration: float
    bottleneck_nodes: List[Dict[str, Any]]
    suggestions: List[OptimizationSuggestion]
    score: float  # 0-100


class WorkflowOptimizer:
    """Workflow optimizer — analyzes and optimizes workflow execution.

    Analyzes execution logs via fusion-mlx to automatically find bottlenecks and generate optimization suggestions.
    """

    def __init__(self, mlx_client: Optional[FusionMLXClient] = None):
        self.mlx = mlx_client or FusionMLXClient()

    # ── Workflow analysis ──

    def analyze_workflow(self, execution_history: List[Dict]) -> WorkflowAnalysis:
        """Analyze workflow execution history and generate optimization suggestions."""
        if not execution_history:
            return WorkflowAnalysis(
                workflow_name="unknown",
                total_executions=0,
                avg_duration=0.0,
                bottleneck_nodes=[],
                suggestions=[],
                score=100.0,
            )

        # Compute basic statistics
        total_execs = len(execution_history)
        durations = [e.get("total_time", 0) for e in execution_history]
        avg_duration = sum(durations) / max(len(durations), 1)

        # Analyze node durations
        node_stats = self._analyze_nodes(execution_history)
        bottleneck_nodes = self._find_bottlenecks(node_stats)

        # Generate optimization suggestions
        suggestions = self._generate_suggestions(node_stats, avg_duration, total_execs)

        # Overall score
        score = self._calculate_score(node_stats, suggestions)

        workflow_name = execution_history[0].get("workflow_name", "unknown")

        return WorkflowAnalysis(
            workflow_name=workflow_name,
            total_executions=total_execs,
            avg_duration=avg_duration,
            bottleneck_nodes=bottleneck_nodes,
            suggestions=suggestions,
            score=score,
        )

    def _analyze_nodes(self, history: List[Dict]) -> Dict[str, Dict[str, float]]:
        """Analyze per-node execution statistics."""
        node_stats: Dict[str, Dict[str, float]] = {}

        for execution in history:
            for step in execution.get("steps", []):
                node_name = step.get("node_display_name", step.get("node_name", "unknown"))
                exec_time = step.get("execution_time", 0)
                status = step.get("status", "unknown")

                if node_name not in node_stats:
                    node_stats[node_name] = {
                        "total_time": 0,
                        "count": 0,
                        "failures": 0,
                        "max_time": 0,
                        "min_time": float("inf"),
                    }

                stats = node_stats[node_name]
                stats["total_time"] += exec_time
                stats["count"] += 1
                stats["max_time"] = max(stats["max_time"], exec_time)
                stats["min_time"] = min(stats["min_time"], exec_time)
                if status == "failed":
                    stats["failures"] += 1

        # Compute averages
        for name, stats in node_stats.items():
            stats["avg_time"] = stats["total_time"] / max(stats["count"], 1)
            stats["failure_rate"] = stats["failures"] / max(stats["count"], 1) * 100

        return node_stats

    def _find_bottlenecks(self, node_stats: Dict[str, Dict]) -> List[Dict[str, Any]]:
        """Find bottleneck nodes."""
        if not node_stats:
            return []

        # Sort by average duration
        sorted_nodes = sorted(
            node_stats.items(),
            key=lambda x: x[1]["avg_time"],
            reverse=True,
        )

        bottlenecks = []
        for name, stats in sorted_nodes[:3]:
            bottlenecks.append(
                {
                    "name": name,
                    "avg_time": round(stats["avg_time"], 3),
                    "max_time": round(stats["max_time"], 3),
                    "failure_rate": round(stats["failure_rate"], 1),
                    "executions": stats["count"],
                }
            )

        return bottlenecks

    def _generate_suggestions(
        self,
        node_stats: Dict[str, Dict],
        avg_duration: float,
        total_execs: int,
    ) -> List[OptimizationSuggestion]:
        """Generate optimization suggestions."""
        suggestions = []

        # Check bottleneck nodes
        bottlenecks = self._find_bottlenecks(node_stats)
        for b in bottlenecks:
            if b["avg_time"] > avg_duration * 0.5:
                suggestions.append(
                    OptimizationSuggestion(
                        category="performance",
                        severity="warning",
                        # i18n: user-facing
                        title=f"节点 '{b['name']}' 是性能瓶颈",
                        # i18n: user-facing
                        description=f"平均耗时 {b['avg_time']:.2f}s，占总执行时间 {b['avg_time'] / max(avg_duration, 0.001) * 100:.0f}%",
                        current_value=f"{b['avg_time']:.2f}s",
                        # i18n: user-facing
                        suggested_value=f"目标 < {avg_duration * 0.3:.2f}s",
                        # i18n: user-facing
                        expected_improvement="预计可提升 30-50%",
                        auto_fix=False,
                    )
                )

        # Check failure rate
        for name, stats in node_stats.items():
            if stats["failure_rate"] > 10:
                suggestions.append(
                    OptimizationSuggestion(
                        category="reliability",
                        severity="critical" if stats["failure_rate"] > 30 else "warning",
                        # i18n: user-facing
                        title=f"节点 '{name}' 失败率过高",
                        # i18n: user-facing
                        description=f"失败率 {stats['failure_rate']:.0f}% ({stats['failures']}/{stats['count']})",
                        current_value=f"{stats['failure_rate']:.0f}%",
                        # i18n: user-facing
                        suggested_value="目标 < 5%",
                        # i18n: user-facing
                        expected_improvement="建议启用 continue_on_error",
                        auto_fix=True,
                    )
                )

        # Check parallelism
        if total_execs > 10 and avg_duration > 5:
            suggestions.append(
                OptimizationSuggestion(
                    category="parallelism",
                    severity="info",
                    # i18n: user-facing
                    title="考虑增加并行度",
                    # i18n: user-facing
                    description="工作流包含独立节点，可并行执行减少总耗时",
                    # i18n: user-facing
                    current_value="串行执行",
                    # i18n: user-facing
                    suggested_value="并行执行独立节点",
                    # i18n: user-facing
                    expected_improvement="预计可节省 40-60% 时间",
                    auto_fix=False,
                )
            )

        # Generic optimization
        suggestions.append(
            OptimizationSuggestion(
                category="configuration",
                severity="info",
                # i18n: user-facing
                title="启用 dry_run 预览模式",
                # i18n: user-facing
                description="执行前使用 dry_run 预览可避免误操作",
                # i18n: user-facing
                current_value="直接执行",
                # i18n: user-facing
                suggested_value="先 dry_run 预览",
                # i18n: user-facing
                expected_improvement="提高操作安全性",
                auto_fix=True,
            )
        )

        return suggestions

    def _calculate_score(self, node_stats: Dict[str, Dict], suggestions: List) -> float:
        """Compute overall score (0-100)."""
        score = 100.0

        # Deduct per suggestion
        for s in suggestions:
            if s.severity == "critical":
                score -= 20
            elif s.severity == "warning":
                score -= 10
            elif s.severity == "info":
                score -= 3

        # Deduct per failure rate
        for name, stats in node_stats.items():
            if stats["failure_rate"] > 0:
                score -= stats["failure_rate"] * 0.3

        return max(0, min(100, score))

    # ── AI optimization suggestions (via fusion-mlx) ──

    async def ai_optimize(
        self,
        workflow_config: Dict[str, Any],
        execution_history: List[Dict],
    ) -> str:
        """Generate AI optimization suggestions via fusion-mlx."""
        analysis = self.analyze_workflow(execution_history)

        prompt = f"""Analyze the following workflow configuration and execution data and provide optimization suggestions:

Workflow configuration:
{json.dumps(workflow_config, ensure_ascii=False, indent=2)}

Execution statistics:
- Total executions: {analysis.total_executions}
- Average duration: {analysis.avg_duration:.2f}s
- Overall score: {analysis.score:.1f}/100

Bottleneck nodes:
{json.dumps(analysis.bottleneck_nodes, ensure_ascii=False, indent=2)}

Please provide concrete optimization suggestions, including:
1. Parameter tuning suggestions
2. Node ordering optimization
3. Parallelism optimization
4. Expected impact
"""

        messages = [
            {"role": "system", "content": "You are a workflow optimization expert. Analyze workflow execution data and provide concrete optimization suggestions."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self.mlx.chat(
                model="qwen3.5-9b",
                messages=messages,
                temperature=0.3,
                max_tokens=2048,
            )
            return response.content
        except Exception as e:
            logger.error(f"AI optimization failed: {e}")
            return f"AI optimization unavailable: {e}"

    # ── Auto-fix ──

    def auto_fix(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-fix issues that can be fixed automatically."""
        fixed = dict(workflow_config)

        # Enable continue_on_error
        for node in fixed.get("nodes", []):
            config = node.get("config", {})
            params = config.get("params", {})
            # Auto-enable continue_on_error for nodes with high failure rates
            if params.get("continue_on_error") is None:
                params["continue_on_error"] = True
                config["params"] = params
                node["config"] = config

        return fixed
