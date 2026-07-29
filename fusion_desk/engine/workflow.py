"""工作流引擎 — 工作流定义、执行、数据传递和错误处理。

工作流由一系列节点通过边连接形成 DAG（有向无环图），
引擎负责：
1. 拓扑排序节点执行顺序
2. 数据传递（节点间通过输入/输出传递）
3. 并行执行（无依赖节点可并行）
4. 错误处理与重试
5. 执行状态追踪
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from .node import BaseNode, NodeConfig, NodeRegistry, NodeResult, NodeStatus
from .hooks import HookEvent

logger = logging.getLogger(__name__)


class WorkflowStatus(Enum):
    """工作流执行状态。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"  # 部分成功


@dataclass
class WorkflowStep:
    """工作流中的一步执行记录。"""
    node_id: str
    node_name: str
    node_display_name: str
    status: NodeStatus
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    execution_time: float = 0.0
    error: Optional[str] = None
    summary: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowExecution:
    """工作流执行记录。"""
    id: str
    workflow_id: str
    workflow_name: str
    status: WorkflowStatus
    started_at: float
    completed_at: Optional[float] = None
    total_time: float = 0.0
    steps: List[WorkflowStep] = field(default_factory=list)
    error: Optional[str] = None
    result_summary: str = ""


@dataclass
class Edge:
    """工作流中节点间的连接边。"""
    source_id: str
    target_id: str
    source_output: str = "output"  # 源节点的输出端口
    target_input: str = "input"    # 目标节点的输入端口
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "source_output": self.source_output,
            "target_input": self.target_input,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Edge:
        return cls(
            source_id=data["source_id"],
            target_id=data["target_id"],
            source_output=data.get("source_output", "output"),
            target_input=data.get("target_input", "input"),
            label=data.get("label", ""),
        )


class Workflow:
    """工作流定义 — 一组节点和边的有向图。

    类似 n8n 的工作流概念，但更轻量、Python 原生。
    支持：
    - 节点编排（DAG）
    - 条件分支（通过条件节点）
    - 循环（通过循环节点）
    - 数据传递
    - 错误处理
    """

    def __init__(
        self,
        name: str = "",
        description: str = "",
        workflow_id: str = "",
        tags: List[str] = None,
    ):
        self.id = workflow_id or f"wf_{uuid.uuid4().hex[:12]}"
        self.name = name or f"工作流_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.description = description
        self.tags = tags or []
        self.nodes: Dict[str, BaseNode] = {}
        self.edges: List[Edge] = []
        self.created_at = time.time()
        self.updated_at = self.created_at

    def add_node(self, node: BaseNode) -> str:
        """添加节点到工作流，返回节点 ID。"""
        self.nodes[node.id] = node
        self.updated_at = time.time()
        return node.id

    def remove_node(self, node_id: str) -> bool:
        """移除节点及其所有边。"""
        if node_id not in self.nodes:
            return False
        del self.nodes[node_id]
        self.edges = [e for e in self.edges
                      if e.source_id != node_id and e.target_id != node_id]
        self.updated_at = time.time()
        return True

    def connect(self, source_id: str, target_id: str,
                source_output: str = "output", target_input: str = "input") -> bool:
        """连接两个节点。"""
        if source_id not in self.nodes or target_id not in self.nodes:
            return False
        # 检查是否形成环（简单检查）
        if self._would_create_cycle(source_id, target_id):
            logger.warning(f"连接 {source_id} → {target_id} 会形成环，已阻止")
            return False
        self.edges.append(Edge(
            source_id=source_id,
            target_id=target_id,
            source_output=source_output,
            target_input=target_input,
        ))
        self.updated_at = time.time()
        return True

    def _would_create_cycle(self, source_id: str, target_id: str) -> bool:
        """检查添加边是否会形成环（BFS 从 target 出发是否能到达 source）。"""
        visited: Set[str] = set()
        queue = [target_id]
        while queue:
            current = queue.pop(0)
            if current == source_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            for edge in self.edges:
                if edge.source_id == current:
                    queue.append(edge.target_id)
        return False

    def get_upstream_nodes(self, node_id: str) -> List[str]:
        """获取指定节点的上游节点 ID 列表。"""
        upstream = []
        for edge in self.edges:
            if edge.target_id == node_id:
                upstream.append(edge.source_id)
        return upstream

    def get_downstream_nodes(self, node_id: str) -> List[str]:
        """获取指定节点的下游节点 ID 列表。"""
        downstream = []
        for edge in self.edges:
            if edge.source_id == node_id:
                downstream.append(edge.target_id)
        return downstream

    def topological_sort(self) -> List[str]:
        """拓扑排序，返回节点执行顺序。"""
        in_degree: Dict[str, int] = {nid: 0 for nid in self.nodes}
        for edge in self.edges:
            if edge.target_id in in_degree:
                in_degree[edge.target_id] = in_degree.get(edge.target_id, 0) + 1

        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        sorted_nodes = []

        while queue:
            node_id = queue.pop(0)
            sorted_nodes.append(node_id)
            for edge in self.edges:
                if edge.source_id == node_id and edge.target_id in in_degree:
                    in_degree[edge.target_id] -= 1
                    if in_degree[edge.target_id] == 0:
                        queue.append(edge.target_id)

        # 检查是否有环（未排序的节点）
        if len(sorted_nodes) != len(self.nodes):
            missing = set(self.nodes.keys()) - set(sorted_nodes)
            logger.error(f"工作流存在环，无法排序: {missing}")
            return []

        return sorted_nodes

    def get_start_nodes(self) -> List[str]:
        """获取没有上游的起始节点。"""
        all_targets = {e.target_id for e in self.edges}
        return [nid for nid in self.nodes if nid not in all_targets]

    def validate(self) -> List[str]:
        """校验工作流完整性，返回错误列表。"""
        errors = []
        if not self.nodes:
            errors.append("工作流没有节点")
            return errors

        start_nodes = self.get_start_nodes()
        if not start_nodes:
            errors.append("工作流没有起始节点（所有节点都有上游依赖）")

        # 检查孤立节点
        all_connected: Set[str] = set()
        for edge in self.edges:
            all_connected.add(edge.source_id)
            all_connected.add(edge.target_id)
        isolated = set(self.nodes.keys()) - all_connected
        if len(self.nodes) > 1 and isolated:
            for nid in isolated:
                errors.append(f"节点 '{self.nodes[nid].config.label or nid}' 是孤立的")

        # 检查每个节点配置
        for nid, node in self.nodes.items():
            node_errors = node.validate_config()
            for e in node_errors:
                errors.append(f"节点 '{node.config.label or nid}': {e}")

        return errors

    def to_dict(self) -> Dict[str, Any]:
        """序列化工作流为字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "edges": [
                {"source_id": e.source_id, "target_id": e.target_id,
                 "source_output": e.source_output, "target_input": e.target_input,
                 "label": e.label}
                for e in self.edges
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """序列化工作流为 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Workflow:
        """从字典反序列化工作流。

        支持两种节点格式：
        - dict: {"n1": {...}, "n2": {...}}  (标准格式)
        - list: [{"id": "n1", ...}, ...]     (模板格式)
        """
        wf = cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            workflow_id=data.get("id", ""),
            tags=data.get("tags", []),
        )
        # 恢复节点（支持 dict 和 list 两种格式）
        nodes_data = data.get("nodes", {})
        if isinstance(nodes_data, list):
            for node_data in nodes_data:
                nid = node_data.get("id", "")
                node_class = NodeRegistry.get(node_data.get("name", ""))
                if node_class:
                    node = node_class.from_dict(node_data)
                    wf.nodes[node.id] = node
                else:
                    logger.warning(f"未知节点类型: {node_data.get('name')}，跳过")
        elif isinstance(nodes_data, dict):
            for nid, node_data in nodes_data.items():
                node_class = NodeRegistry.get(node_data.get("name", ""))
                if node_class:
                    node = node_class.from_dict(node_data)
                    wf.nodes[node.id] = node
                else:
                    logger.warning(f"未知节点类型: {node_data.get('name')}，跳过")
        # 恢复边
        for edge_data in data.get("edges", []):
            edge = Edge(
                source_id=edge_data["source_id"],
                target_id=edge_data["target_id"],
                source_output=edge_data.get("source_output", "output"),
                target_input=edge_data.get("target_input", "input"),
                label=edge_data.get("label", ""),
            )
            wf.edges.append(edge)
        wf.created_at = data.get("created_at", time.time())
        wf.updated_at = data.get("updated_at", time.time())
        return wf

    @classmethod
    def from_json(cls, json_str: str) -> Workflow:
        """从 JSON 字符串反序列化工作流。"""
        return cls.from_dict(json.loads(json_str))


class WorkflowEngine:
    """工作流执行引擎 — 负责执行工作流并管理执行状态。

    类似 n8n 的执行引擎，但完全本地、轻量、异步。
    支持：
    - 顺序执行（拓扑排序）
    - 并行执行（无依赖节点）
    - 错误重试
    - 执行取消
    - 进度回调
    """

    def __init__(self, permission_manager=None, hook_manager=None,
                 session_store=None, event_emitter=None):
        self._executions: Dict[str, WorkflowExecution] = {}
        self._cancel_flags: Dict[str, bool] = {}
        self._progress_callbacks: List[callable] = []
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._session_store = session_store
        self._event_emitter = event_emitter

    def on_progress(self, callback: callable) -> None:
        """注册进度回调。"""
        self._progress_callbacks.append(callback)

    def _notify_progress(self, execution: WorkflowExecution, step: WorkflowStep) -> None:
        """通知所有进度回调。"""
        for cb in self._progress_callbacks:
            try:
                cb(execution, step)
            except Exception as e:
                logger.error(f"进度回调出错: {e}")

    async def execute(
        self,
        workflow: Workflow,
        initial_input: Optional[Dict[str, Any]] = None,
        execution_id: str = "",
    ) -> WorkflowExecution:
        """执行工作流。"""
        exec_id = execution_id or f"exec_{uuid.uuid4().hex[:12]}"
        execution = WorkflowExecution(
            id=exec_id,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            status=WorkflowStatus.RUNNING,
            started_at=time.time(),
        )
        self._executions[exec_id] = execution
        self._cancel_flags[exec_id] = False

        # Session: auto-create
        session = None
        if self._session_store:
            from .session import Session
            session = Session(
                workflow_id=workflow.id,
                workflow_name=workflow.name,
                status="running",
                initial_input=initial_input or {},
                execution_id=exec_id,
            )
            self._session_store.save(session)
            logger.debug(f"Session 自动创建: {session.id}")

        # Event: WORKFLOW_START
        if self._event_emitter:
            from .events import EventType
            self._event_emitter.create_event(
                EventType.WORKFLOW_START,
                execution_id=exec_id,
                data={"workflow_id": workflow.id, "workflow_name": workflow.name},
            )

        # 校验工作流
        errors = workflow.validate()
        if errors:
            execution.status = WorkflowStatus.FAILED
            execution.error = "; ".join(errors)
            execution.completed_at = time.time()
            execution.total_time = execution.completed_at - execution.started_at
            return execution

        # 拓扑排序
        order = workflow.topological_sort()
        if not order:
            execution.status = WorkflowStatus.FAILED
            execution.error = "工作流存在环或无法排序"
            execution.completed_at = time.time()
            execution.total_time = execution.completed_at - execution.started_at
            return execution

        # 执行结果缓存
        node_results: Dict[str, NodeResult] = {}
        passed_data: Dict[str, Dict[str, Any]] = {}

        # 传递给起始节点的初始数据
        if initial_input:
            start_nodes = workflow.get_start_nodes()
            for sn in start_nodes:
                passed_data[sn] = initial_input

        # Hook: WORKFLOW_START
        if self._hook_manager:
            ctx = await self._hook_manager.fire(HookEvent.WORKFLOW_START, {
                "execution_id": exec_id, "workflow_id": workflow.id,
                "workflow_name": workflow.name,
            })
            if ctx and ctx.cancelled:
                execution.status = WorkflowStatus.CANCELLED
                execution.completed_at = time.time()
                execution.total_time = execution.completed_at - execution.started_at
                return execution

        try:
            for node_id in order:
                # 检查取消
                if self._cancel_flags.get(exec_id, False):
                    execution.status = WorkflowStatus.CANCELLED
                    break

                node = workflow.nodes.get(node_id)
                if not node:
                    continue

                # 收集输入数据
                node_input = {}
                upstream = workflow.get_upstream_nodes(node_id)
                for up_id in upstream:
                    if up_id in passed_data:
                        node_input.update(passed_data[up_id])

                # 加上初始输入
                if node_id in passed_data:
                    node_input.update(passed_data[node_id])

                # Permission check
                if self._permission_manager:
                    allowed = await self._permission_manager.check(node.name, "execute", node_input)
                    if not allowed:
                        logger.warning(f"节点 '{node.name}' 被权限系统拒绝执行")
                        step = WorkflowStep(
                            node_id=node_id,
                            node_name=node.name,
                            node_display_name=node.config.label or node.display_name,
                            status=NodeStatus.DENIED,
                            started_at=time.time(),
                            input_data=node_input,
                        )
                        step.completed_at = time.time()
                        step.execution_time = 0
                        step.error = "权限拒绝: 需要手动审批"
                        execution.steps.append(step)
                        if not node.config.continue_on_error:
                            execution.status = WorkflowStatus.FAILED
                            execution.error = f"节点 '{node.name}' 权限拒绝"
                            break
                        continue

                # Hook: PRE_NODE_EXECUTE
                if self._hook_manager:
                    hctx = await self._hook_manager.fire(HookEvent.PRE_NODE_EXECUTE, {
                        "execution_id": exec_id, "node_id": node_id,
                        "node_name": node.name, "input_data": node_input,
                    })
                    if hctx and hctx.cancelled:
                        logger.info(f"节点 '{node.name}' 被Hook取消")
                        step = WorkflowStep(
                            node_id=node_id,
                            node_name=node.name,
                            node_display_name=node.config.label or node.display_name,
                            status=NodeStatus.CANCELLED,
                            started_at=time.time(),
                            input_data=node_input,
                        )
                        step.completed_at = time.time()
                        step.execution_time = 0
                        step.error = "被Hook取消"
                        execution.steps.append(step)
                        if not node.config.continue_on_error:
                            execution.status = WorkflowStatus.CANCELLED
                            break
                        continue
                    if hctx and hctx.modified_data:
                        node_input = hctx.modified_data.get("input_data", node_input)

                # 创建步骤记录
                step = WorkflowStep(
                    node_id=node_id,
                    node_name=node.name,
                    node_display_name=node.config.label or node.display_name,
                    status=NodeStatus.RUNNING,
                    started_at=time.time(),
                    input_data=node_input,
                )
                execution.steps.append(step)

                # 执行节点
                try:
                    node.status = NodeStatus.RUNNING

                    # Event: NODE_START
                    if self._event_emitter:
                        from .events import EventType
                        self._event_emitter.create_event(
                            EventType.NODE_START,
                            execution_id=exec_id, node_id=node_id,
                            node_name=node.name,
                            data={"input_data": node_input},
                        )
                    result = await node.execute(node_input)
                    node.status = result.status
                    node.result = result

                    step.status = result.status
                    step.completed_at = time.time()
                    step.execution_time = step.completed_at - step.started_at
                    step.output_data = result.data or {}
                    step.error = result.error
                    step.summary = result.summary

                    # Hook: POST_NODE_EXECUTE
                    if self._hook_manager:
                        await self._hook_manager.fire(HookEvent.POST_NODE_EXECUTE, {
                            "execution_id": exec_id, "node_id": node_id,
                            "node_name": node.name, "result": result,
                        })

                    # 缓存结果
                    node_results[node_id] = result
                    passed_data[node_id] = result.data or {}

                    # 通知进度
                    self._notify_progress(execution, step)

                    # Event: NODE_END
                    if self._event_emitter:
                        from .events import EventType
                        self._event_emitter.create_event(
                            EventType.NODE_END,
                            execution_id=exec_id, node_id=node_id,
                            node_name=node.name,
                            data={"status": result.status.value},
                        )

                    if result.status == NodeStatus.FAILED:
                        if self._hook_manager:
                            await self._hook_manager.fire(HookEvent.NODE_ERROR, {
                                "execution_id": exec_id, "node_id": node_id,
                                "node_name": node.name, "error": result.error,
                            })
                        if not node.config.continue_on_error:
                            execution.status = WorkflowStatus.FAILED
                            execution.error = f"节点 '{node.config.label or node.name}' 执行失败: {result.error}"
                            break
                        else:
                            logger.warning(f"节点 '{node.config.label or node.name}' 失败但继续执行")

                except Exception as e:
                    node.status = NodeStatus.FAILED
                    step.status = NodeStatus.FAILED
                    step.completed_at = time.time()
                    step.execution_time = step.completed_at - step.started_at
                    step.error = str(e)
                    logger.error(f"节点 '{node.config.label or node.name}' 执行异常: {e}")

                    if self._hook_manager:
                        await self._hook_manager.fire(HookEvent.NODE_ERROR, {
                            "execution_id": exec_id, "node_id": node_id,
                            "node_name": node.name, "error": str(e),
                        })

                    self._notify_progress(execution, step)

                    if not node.config.continue_on_error:
                        execution.status = WorkflowStatus.FAILED
                        execution.error = f"节点 '{node.config.label or node.name}' 异常: {e}"
                        break

            # 最终状态
            if execution.status == WorkflowStatus.RUNNING:
                execution.status = WorkflowStatus.SUCCESS
            elif execution.status == WorkflowStatus.FAILED:
                pass  # 已设置

        except Exception as e:
            execution.status = WorkflowStatus.FAILED
            execution.error = f"工作流引擎异常: {e}"
            logger.exception(f"工作流执行异常: {e}")

        finally:
            execution.completed_at = time.time()
            execution.total_time = execution.completed_at - execution.started_at

            # Hook: WORKFLOW_END / WORKFLOW_CANCEL
            if self._hook_manager:
                if execution.status == WorkflowStatus.CANCELLED:
                    await self._hook_manager.fire(HookEvent.WORKFLOW_CANCEL, {
                        "execution_id": exec_id, "workflow_id": workflow.id,
                    })
                await self._hook_manager.fire(HookEvent.WORKFLOW_END, {
                    "execution_id": exec_id, "workflow_id": workflow.id,
                    "status": execution.status.value,
                    "total_time": execution.total_time,
                })

            # Event: WORKFLOW_END
            if self._event_emitter:
                from .events import EventType
                self._event_emitter.create_event(
                    EventType.WORKFLOW_END,
                    execution_id=exec_id,
                    data={"status": execution.status.value, "total_time": execution.total_time},
                )

            # Session: auto-update
            if session and self._session_store:
                steps_data = [
                    {"node_id": s.node_id, "status": s.status.value if hasattr(s.status, "value") else s.status}
                    for s in execution.steps
                ]
                self._session_store.update_steps(session.id, steps_data)
                self._session_store.update_status(session.id, execution.status.value, completed_at=time.time())

            # 生成摘要
            success_count = sum(1 for s in execution.steps if s.status == NodeStatus.SUCCESS)
            total_count = len(execution.steps)
            execution.result_summary = (
                f"工作流 '{workflow.name}' 执行完成: "
                f"{success_count}/{total_count} 节点成功, "
                f"耗时 {execution.total_time:.2f}s"
            )

            del self._cancel_flags[exec_id]

        return execution

    def cancel(self, execution_id: str) -> bool:
        """取消正在执行的工作流。"""
        if execution_id in self._cancel_flags:
            self._cancel_flags[execution_id] = True
            return True
        return False

    def get_execution(self, execution_id: str) -> Optional[WorkflowExecution]:
        """获取执行记录。"""
        return self._executions.get(execution_id)

    def list_executions(self, limit: int = 20) -> List[WorkflowExecution]:
        """列出最近的执行记录。"""
        sorted_execs = sorted(
            self._executions.values(),
            key=lambda e: e.started_at,
            reverse=True,
        )
        return sorted_execs[:limit]

    def clear_executions(self) -> None:
        """清空执行记录。"""
        self._executions.clear()