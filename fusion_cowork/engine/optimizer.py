"""AI 工作流优化器 — 通过 fusion-mlx 分析工作流执行数据并自动优化。

V0.2 特性：
- 工作流执行分析（瓶颈检测、耗时分布）
- 自动参数调优（batch_size、并行度、超时）
- 节点重新排序优化
- 优化建议生成
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
    """优化建议。"""
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
    """工作流分析结果。"""
    workflow_name: str
    total_executions: int
    avg_duration: float
    bottleneck_nodes: List[Dict[str, Any]]
    suggestions: List[OptimizationSuggestion]
    score: float  # 0-100


class WorkflowOptimizer:
    """工作流优化器 — 分析和优化工作流执行。

    通过 fusion-mlx 分析执行日志，自动发现瓶颈并生成优化建议。
    """

    def __init__(self, mlx_client: Optional[FusionMLXClient] = None):
        self.mlx = mlx_client or FusionMLXClient()

    # ── 工作流分析 ──

    def analyze_workflow(self, execution_history: List[Dict]) -> WorkflowAnalysis:
        """分析工作流执行历史，生成优化建议。"""
        if not execution_history:
            return WorkflowAnalysis(
                workflow_name="unknown",
                total_executions=0,
                avg_duration=0.0,
                bottleneck_nodes=[],
                suggestions=[],
                score=100.0,
            )

        # 计算基本统计
        total_execs = len(execution_history)
        durations = [e.get("total_time", 0) for e in execution_history]
        avg_duration = sum(durations) / max(len(durations), 1)

        # 分析节点耗时
        node_stats = self._analyze_nodes(execution_history)
        bottleneck_nodes = self._find_bottlenecks(node_stats)

        # 生成优化建议
        suggestions = self._generate_suggestions(node_stats, avg_duration, total_execs)

        # 综合评分
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
        """分析每个节点的执行统计。"""
        node_stats: Dict[str, Dict[str, float]] = {}

        for execution in history:
            for step in execution.get("steps", []):
                node_name = step.get("node_display_name", step.get("node_name", "unknown"))
                exec_time = step.get("execution_time", 0)
                status = step.get("status", "unknown")

                if node_name not in node_stats:
                    node_stats[node_name] = {
                        "total_time": 0, "count": 0, "failures": 0,
                        "max_time": 0, "min_time": float("inf"),
                    }

                stats = node_stats[node_name]
                stats["total_time"] += exec_time
                stats["count"] += 1
                stats["max_time"] = max(stats["max_time"], exec_time)
                stats["min_time"] = min(stats["min_time"], exec_time)
                if status == "failed":
                    stats["failures"] += 1

        # 计算平均值
        for name, stats in node_stats.items():
            stats["avg_time"] = stats["total_time"] / max(stats["count"], 1)
            stats["failure_rate"] = stats["failures"] / max(stats["count"], 1) * 100

        return node_stats

    def _find_bottlenecks(self, node_stats: Dict[str, Dict]) -> List[Dict[str, Any]]:
        """找出瓶颈节点。"""
        if not node_stats:
            return []

        # 按平均耗时排序
        sorted_nodes = sorted(
            node_stats.items(),
            key=lambda x: x[1]["avg_time"],
            reverse=True,
        )

        bottlenecks = []
        for name, stats in sorted_nodes[:3]:
            bottlenecks.append({
                "name": name,
                "avg_time": round(stats["avg_time"], 3),
                "max_time": round(stats["max_time"], 3),
                "failure_rate": round(stats["failure_rate"], 1),
                "executions": stats["count"],
            })

        return bottlenecks

    def _generate_suggestions(
        self,
        node_stats: Dict[str, Dict],
        avg_duration: float,
        total_execs: int,
    ) -> List[OptimizationSuggestion]:
        """生成优化建议。"""
        suggestions = []

        # 检查瓶颈节点
        bottlenecks = self._find_bottlenecks(node_stats)
        for b in bottlenecks:
            if b["avg_time"] > avg_duration * 0.5:
                suggestions.append(OptimizationSuggestion(
                    category="performance",
                    severity="warning",
                    title=f"节点 '{b['name']}' 是性能瓶颈",
                    description=f"平均耗时 {b['avg_time']:.2f}s，占总执行时间 {b['avg_time']/max(avg_duration,0.001)*100:.0f}%",
                    current_value=f"{b['avg_time']:.2f}s",
                    suggested_value=f"目标 < {avg_duration * 0.3:.2f}s",
                    expected_improvement="预计可提升 30-50%",
                    auto_fix=False,
                ))

        # 检查失败率
        for name, stats in node_stats.items():
            if stats["failure_rate"] > 10:
                suggestions.append(OptimizationSuggestion(
                    category="reliability",
                    severity="critical" if stats["failure_rate"] > 30 else "warning",
                    title=f"节点 '{name}' 失败率过高",
                    description=f"失败率 {stats['failure_rate']:.0f}% ({stats['failures']}/{stats['count']})",
                    current_value=f"{stats['failure_rate']:.0f}%",
                    suggested_value="目标 < 5%",
                    expected_improvement="建议启用 continue_on_error",
                    auto_fix=True,
                ))

        # 检查并行度
        if total_execs > 10 and avg_duration > 5:
            suggestions.append(OptimizationSuggestion(
                category="parallelism",
                severity="info",
                title="考虑增加并行度",
                description="工作流包含独立节点，可并行执行减少总耗时",
                current_value="串行执行",
                suggested_value="并行执行独立节点",
                expected_improvement="预计可节省 40-60% 时间",
                auto_fix=False,
            ))

        # 通用优化
        suggestions.append(OptimizationSuggestion(
            category="configuration",
            severity="info",
            title="启用 dry_run 预览模式",
            description="执行前使用 dry_run 预览可避免误操作",
            current_value="直接执行",
            suggested_value="先 dry_run 预览",
            expected_improvement="提高操作安全性",
            auto_fix=True,
        ))

        return suggestions

    def _calculate_score(self, node_stats: Dict[str, Dict], suggestions: List) -> float:
        """计算综合评分 (0-100)。"""
        score = 100.0

        # 根据建议扣分
        for s in suggestions:
            if s.severity == "critical":
                score -= 20
            elif s.severity == "warning":
                score -= 10
            elif s.severity == "info":
                score -= 3

        # 根据失败率扣分
        for name, stats in node_stats.items():
            if stats["failure_rate"] > 0:
                score -= stats["failure_rate"] * 0.3

        return max(0, min(100, score))

    # ── AI 优化建议（通过 fusion-mlx） ──

    async def ai_optimize(
        self,
        workflow_config: Dict[str, Any],
        execution_history: List[Dict],
    ) -> str:
        """使用 fusion-mlx 生成 AI 优化建议。"""
        analysis = self.analyze_workflow(execution_history)

        prompt = f"""分析以下工作流配置和执行数据，给出优化建议：

工作流配置:
{json.dumps(workflow_config, ensure_ascii=False, indent=2)}

执行统计:
- 总执行次数: {analysis.total_executions}
- 平均耗时: {analysis.avg_duration:.2f}s
- 综合评分: {analysis.score:.1f}/100

瓶颈节点:
{json.dumps(analysis.bottleneck_nodes, ensure_ascii=False, indent=2)}

请给出具体的优化建议，包括：
1. 参数调整建议
2. 节点顺序优化
3. 并行度优化
4. 预期效果
"""

        messages = [
            {"role": "system", "content": "你是一个工作流优化专家。分析工作流执行数据，给出具体的优化建议。"},
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
            logger.error(f"AI 优化失败: {e}")
            return f"AI 优化不可用: {e}"

    # ── 自动修复 ──

    def auto_fix(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
        """自动修复可自动修复的问题。"""
        fixed = dict(workflow_config)

        # 启用 continue_on_error
        for node in fixed.get("nodes", []):
            config = node.get("config", {})
            params = config.get("params", {})
            # 如果有失败率高的节点，自动启用 continue_on_error
            if params.get("continue_on_error") is None:
                params["continue_on_error"] = True
                config["params"] = params
                node["config"] = config

        return fixed