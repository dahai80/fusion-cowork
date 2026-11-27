"""Workflow engine — workflow definition, execution, data passing, and error handling.

A workflow is a DAG formed by nodes connected via edges. The engine is
responsible for:
1. Topologically sorting node execution order
2. Data passing (nodes exchange data via inputs/outputs)
3. Ready-set wave execution (nodes whose predecessors completed run in
   parallel within the same wave; concurrency=1 degrades to serial
   — CLAIM: verified by tests/test_issue_189_node_executor.py)
4. Error handling and retry
5. Execution state tracking
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from ..observability.ops_telemetry import failure_signature, workflow_definition_hash
from ..observability.trace import set_trace_id
from ._harness_guard import harness_import_error
from .executors import NodeExecutionContext, executor_for_node, executor_names
from .hooks import HookEvent
from .kernel_gates import (
    attribute_node_failure,
    build_approval_gate,
    classify_destructive,
    failure_attribution_enabled,
    is_spec_failure,
    json_repairs_in_step,
)
from .node import BaseNode, NodeConfig, NodeRegistry, NodeResult, NodeStatus, _coerce_bool
from .permission_manager import (
    HITL_GATE_TOOLS,
    _sanitize_approval_params,
    reset_approval_context,
    reset_approval_evidence,
    set_approval_context,
    set_approval_evidence,
)

try:  # issue #527: HITL approval queue goes through the shared-kernel HumanGate contract (fusion-harness #66)
    from fusion_harness import AWAITING_HUMAN_MARKER, HumanGate
except ImportError as exc:
    raise harness_import_error(exc) from exc

logger = logging.getLogger(__name__)

# issue #462: current execution-engine context — sub_workflow nodes build a child
# engine with shared permission/HITL config from this (set/reset by _run_node during node execution)
_current_engine: ContextVar[Optional[WorkflowEngine]] = ContextVar("fc_current_engine", default=None)


def _parse_hitl_timeout(raw: Any) -> float:
    # issue #187: approval wait timeout parsing — accepts plain seconds or s/m/h suffix; bad values fall back to the default 1800s
    val = str(raw or "").strip().lower()
    if not val:
        return 1800.0
    try:
        if val.endswith("s"):
            return float(val[:-1])
        if val.endswith("m"):
            return float(val[:-1]) * 60
        if val.endswith("h"):
            return float(val[:-1]) * 3600
        return float(val)
    except ValueError:
        logger.warning(f"FUSION_COWORK_HITL_TIMEOUT unparseable: {raw!r}, falling back to default 1800s")
        return 1800.0


# issue #120: run-scoped step logs — `grep "[<run_id>]"` rebuilds the execution timeline.
# params keeps scalars only, summarizes complex values, masks sensitive keys; verdict distinguishes business gating (gated/blocked) from failure.
_LOG_SECRET_MARKERS = ("secret", "token", "password", "passwd", "api_key", "apikey", "authorization", "credential")
_LOG_STR_CAP = 200
_LOG_MAX_KEYS = 20


def _sanitize_log_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_LOG_STR_CAP]
    if isinstance(value, dict):
        return f"<dict:{len(value)}>"
    if isinstance(value, (list, tuple, set)):
        return f"<{type(value).__name__}:{len(value)}>"
    return f"<{type(value).__name__}>"


def _sanitize_params(params: Any) -> str:
    if not isinstance(params, dict):
        return str(_sanitize_log_value(params))
    out: Dict[str, Any] = {}
    for k, v in list(params.items())[:_LOG_MAX_KEYS]:
        key = str(k)
        if any(m in key.lower() for m in _LOG_SECRET_MARKERS):
            out[key] = "***"
            continue
        out[key] = _sanitize_log_value(v)
    if len(params) > _LOG_MAX_KEYS:
        out["..."] = f"<+{len(params) - _LOG_MAX_KEYS} more>"
    return json.dumps(out, ensure_ascii=False, default=str)


# issue #326: approval evidence snapshot caps — 3 keys/2000 chars per producing node, at most 3 upstream nodes
_EVIDENCE_MAX_NODES = 3
_EVIDENCE_MAX_KEYS = 3
_EVIDENCE_MAX_CHARS = 2000


def _evidence_cap_value(v: Any) -> Any:
    # Evidence value snapshot: scalars passed through (long strings truncated), dict/list JSON-truncated — preview only, not full output
    if isinstance(v, str):
        return v[:_EVIDENCE_MAX_CHARS]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    text = json.dumps(v, ensure_ascii=False, default=str)
    return text[:_EVIDENCE_MAX_CHARS]


def _collect_upstream_evidence(workflow: Workflow, node: BaseNode, passed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    # issue #326: snapshot of direct upstream output of a gated node — the approval card shows the review subject (PRD/architecture, etc.)
    evidence: List[Dict[str, Any]] = []
    seen: set = set()
    for edge in workflow.edges:
        if edge.target_id != node.id or edge.source_id in seen or edge.source_id not in passed_data:
            continue
        seen.add(edge.source_id)
        src = workflow.nodes.get(edge.source_id)
        out = passed_data[edge.source_id]
        keys = list(out.items())[:_EVIDENCE_MAX_KEYS] if isinstance(out, dict) else [("output", out)]
        evidence.append(
            {
                "node_id": edge.source_id,
                "node_name": getattr(src, "display_name", None) or getattr(src, "name", edge.source_id),
                "output": {str(k): _evidence_cap_value(v) for k, v in keys},
            }
        )
        if len(evidence) >= _EVIDENCE_MAX_NODES:
            break
    return evidence


# issue #478: refs accept an optional `|literal` fallback — when the path misses,
# the fallback is used instead of leaving the literal ${...} behind (which the
# fail-fast #343 check would reject). E.g. ${step.n1.result.artifacts.0|-} keeps
# canonical artifact handoff when artifacts exist and degrades gracefully when empty.
_STEP_REF_RE = re.compile(r"\$\{step\.([A-Za-z0-9_\-]+)((?:\.[A-Za-z0-9_\-]+)*)(?:\|([^\}]*))?\}")
_RESULT_REF_RE = re.compile(r"\$\{result\.([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)(?:\|([^\}]*))?\}")


def _walk_ref_path(cur: Any, parts: List[str]) -> Tuple[Any, bool]:
    # issue #327: drill down an a.b.c path — dict by key, list by index; any missing level is a miss
    for part in parts:
        if isinstance(cur, dict):
            if part not in cur:
                return None, False
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None, False
        else:
            return None, False
        if cur is None:
            return None, False
    return cur, True


def _resolve_result_ref(local_input: Dict[str, Any], parts: List[str]) -> Tuple[Any, bool]:
    # issue #343: upstream output often wraps in a {"result": {...}} envelope (github_api and similar nodes) —
    # the shorthand looks up a top-level key first, then drills into the result envelope subtree on miss
    found, ok = _walk_ref_path(local_input, parts)
    if not ok and isinstance(local_input, dict):
        found, ok = _walk_ref_path(local_input.get("result"), parts)
    return found, ok


def _interpolate_step_refs(value: Any, passed_data: Dict[str, Dict[str, Any]], local_input: Dict[str, Any]) -> Any:
    """issue #327: resolve ${step.<id>.<path>} / ${result.<path>} references before node start.

    - ${step.n1.result.last_result.report} — explicitly take a given node's output (passed_data)
    - ${result.repo_path} — shorthand: take a value from this node's merged upstream input (local_input),
      drilling into the upstream {"result": {...}} envelope subtree on miss (issue #343)
    - Full-string hits preserve the original type (issue_number stays numeric, not str); embedded hits are str-ified
    - Misses keep the literal + WARNING (same convention as template _substitute_params, never silently dropped)
    """
    if isinstance(value, str):
        m = _STEP_REF_RE.fullmatch(value)
        if m:
            found, ok = _walk_ref_path(
                passed_data.get(m.group(1)), m.group(2).lstrip(".").split(".") if m.group(2) else []
            )
            if ok:
                return found
            if m.group(3) is not None:
                # issue #478: explicit fallback — a missing path degrades to the literal, no ${...} residue left behind
                return m.group(3)
            logger.warning(f"step reference miss, keeping literal: {value[:120]}")
            return value
        m = _RESULT_REF_RE.fullmatch(value)
        if m:
            found, ok = _resolve_result_ref(local_input, m.group(1).split("."))
            if ok:
                return found
            if m.group(2) is not None:
                return m.group(2)
            logger.warning(f"result reference miss, keeping literal: {value[:120]}")
            return value

        def _repl(m: re.Match[str]) -> str:
            fallback = m.group(3) if m.re is _STEP_REF_RE else m.group(2)
            if m.re is _STEP_REF_RE:
                found, ok = _walk_ref_path(
                    passed_data.get(m.group(1)), m.group(2).lstrip(".").split(".") if m.group(2) else []
                )
            else:
                found, ok = _resolve_result_ref(local_input, m.group(1).split("."))
            if not ok:
                if fallback is not None:
                    return fallback
                logger.warning(f"reference miss, keeping literal: {m.group(0)[:120]}")
                return m.group(0)
            return json.dumps(found, ensure_ascii=False) if isinstance(found, (dict, list)) else str(found)

        out = _STEP_REF_RE.sub(_repl, value)
        out = _RESULT_REF_RE.sub(_repl, out)
        return out
    if isinstance(value, dict):
        return {k: _interpolate_step_refs(v, passed_data, local_input) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_step_refs(v, passed_data, local_input) for v in value]
    return value


# issue #343: scan only the three reference prefixes the engine can resolve — free placeholders like ${run_id} are unaffected
_UNRESOLVED_REF_RE = re.compile(r"\$\{(?:result|step|param)\.[^}]*\}")


def _find_unresolved_refs(value: Any, out: Optional[List[str]] = None) -> List[str]:
    if out is None:
        out = []
    if isinstance(value, str):
        out.extend(m.group(0) for m in _UNRESOLVED_REF_RE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            _find_unresolved_refs(v, out)
    elif isinstance(value, list):
        for v in value:
            _find_unresolved_refs(v, out)
    return out


def _resolve_data_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in str(path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _skip_if_hit(conditions: Any, passed_data: Dict[str, Dict[str, Any]], node_input: Dict[str, Any]) -> str:
    # issue #422: when all conditions hit, return the hit description (for the skipped step's summary), else empty string.
    # Single condition: {"source": "<node_id>"?, "path": "a.b", equals|not_equals|in|not_in}
    # source defaults to evaluating against the merged upstream input; with source set, that node's output is used (missing output = None)
    for cond in conditions or []:
        if not isinstance(cond, dict):
            continue
        source = str(cond.get("source", "") or "")
        data = passed_data.get(source) if source else node_input
        if data is None:
            data = {}
        val = _resolve_data_path(data, str(cond.get("path", "") or ""))
        if "equals" in cond:
            ok = val == cond["equals"]
        elif "not_equals" in cond:
            ok = val != cond["not_equals"]
        elif "in" in cond:
            ok = val in (cond["in"] or [])
        elif "not_in" in cond:
            ok = val not in (cond["not_in"] or [])
        elif "gte" in cond:
            ok = isinstance(val, (int, float)) and val >= cond["gte"]
        elif "lte" in cond:
            ok = isinstance(val, (int, float)) and val <= cond["lte"]
        else:
            ok = bool(val)
        # exp/cond-distill: negate flips the hit — lets skip_if express
        # "run only when <cond>" (flywheel condition-DSL inversion)
        if cond.get("negate"):
            ok = not ok
        if not ok:
            return ""
    return "; ".join(
        f"{c.get('source') or 'upstream'}.{c.get('path', '')}={c.get('equals', c.get('in', c.get('not_in', '?')))}"
        for c in (conditions or [])
        if isinstance(c, dict)
    )


def _node_verdict(status: NodeStatus, output: Any) -> str:
    # verdict ≠ status: node output can flag business gating (blocked/passed=false/gated)
    if status == NodeStatus.FAILED:
        return "failed"
    if status == NodeStatus.DENIED:
        return "gated"
    data = output if isinstance(output, dict) else {}
    if data.get("blocked") is True:
        return "blocked"
    if data.get("passed") is False or data.get("gated") is True or data.get("requires_approval") is True:
        return "gated"
    return "success"


def _extract_tokens(output: Any) -> Optional[Dict[str, int]]:
    # issue #143: extract token usage from node output (usage dict: prompt/completion/total)
    if not isinstance(output, dict):
        return None
    usage = output.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        prompt_t = int(usage.get("prompt_tokens", 0) or 0)
        completion_t = int(usage.get("completion_tokens", 0) or 0)
        total_raw = usage.get("total_tokens")
        total_t = int(total_raw) if total_raw is not None else prompt_t + completion_t
    except (TypeError, ValueError):
        return None
    if prompt_t <= 0 and completion_t <= 0 and total_t <= 0:
        return None
    return {"prompt_tokens": prompt_t, "completion_tokens": completion_t, "total_tokens": total_t}


class WorkflowStatus(Enum):
    """Workflow execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"  # partial success
    # issue #187: completed but some nodes were permission-denied (auto-deny on timeout / manual deny) — never silently treated as all-green
    COMPLETED_WITH_DENIALS = "completed_with_denials"


@dataclass
class WorkflowStep:
    """Execution record for one step in a workflow."""

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
    # issue #143: aggregated node token usage (from output usage field), for the run waterfall view
    tokens: Optional[Dict[str, int]] = None
    # issue #182: retry observability — final attempt number (1-based) + per-attempt errors (successful attempt not recorded)
    attempt: int = 1
    attempt_errors: List[str] = field(default_factory=list)
    # issue #189: executor used by this step (simple/agent)
    executor: str = "simple"
    # issue #227: user-correction marker — instructions injected mid-run land as a marker step in the timeline
    # (a zero-duration marker with status=skipped, not a node execution), for the run view to render "user correction"
    user_correction: bool = False
    # issue #460: session-level checkpoint — the run session id this step belongs to, persisted with the snapshot;
    # after resume is accepted, agent seat nodes can reuse the original session (missing field in old snapshots = unchanged behavior)
    session_id: Optional[str] = None
    # issue #554: number of LLM JSON repair-ladder triggers in this step (0 = no repair), observable via telemetry/run API
    json_repairs: int = 0


@dataclass
class WorkflowExecution:
    """Workflow execution record."""

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
    # issue #189: run-level concurrency cap + executor distribution (simple:N, agent:M)
    concurrency: int = 1
    executors: Dict[str, int] = field(default_factory=dict)
    # issue #571: MAST attribution of the first failed node (mode/cluster/confidence/evidence;
    # enabled when FUSION_COWORK_FAILURE_ATTRIBUTION=1, default None)
    attribution: Optional[Dict[str, Any]] = None


@dataclass
class _RunState:
    """issue #189: shared execution state of a single run (shared by the wave scheduler and per-node executors)."""

    exec_id: str
    workflow: Workflow
    execution: WorkflowExecution
    node_results: Dict[str, NodeResult]
    passed_data: Dict[str, Dict[str, Any]]
    completed_ids: Set[str]
    hitl_lock: asyncio.Lock


@dataclass
class Edge:
    """Connection edge between nodes in a workflow."""

    source_id: str
    target_id: str
    source_output: str = "output"  # source node's output port
    target_input: str = "input"  # target node's input port
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
        # issue #135 F-2: for a malformed edge shape (e.g. {"from","to"}) missing required keys, raise a readable ValueError
        # instead of leaving a KeyError 500; the API layer converts it to 400
        missing = [key for key in ("source_id", "target_id") if not str(data.get(key, "") or "").strip()]
        if missing:
            raise ValueError(f"edge data missing required fields {missing}: {data}")
        return cls(
            source_id=data["source_id"],
            target_id=data["target_id"],
            source_output=data.get("source_output", "output"),
            target_input=data.get("target_input", "input"),
            label=data.get("label", ""),
        )


class Workflow:
    """Workflow definition — a directed graph of nodes and edges.

    Similar to n8n's workflow concept but lighter and Python-native.
    Supports:
    - Node orchestration (DAG)
    - Conditional branching (via condition nodes)
    - Loops (via loop nodes)
    - Data passing
    - Error handling
    """

    def __init__(
        self,
        name: str = "",
        description: str = "",
        workflow_id: str = "",
        tags: List[str] = None,
        verification_policy: str = "lenient",
    ):
        self.id = workflow_id or f"wf_{uuid.uuid4().hex[:12]}"
        # i18n: user-facing
        self.name = name or f"工作流_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.description = description
        self.tags = tags or []
        # issue #480: terminal deliverables must have a verify_objective predecessor — strict = validation error,
        # lenient = warning only (default, zero breakage for existing workflows), off = silent
        self.verification_policy = str(verification_policy or "lenient").strip().lower()
        self.nodes: Dict[str, BaseNode] = {}
        self.edges: List[Edge] = []
        self.created_at = time.time()
        self.updated_at = self.created_at

    def add_node(self, node: BaseNode) -> str:
        """Add a node to the workflow, returning the node ID."""
        self.nodes[node.id] = node
        self.updated_at = time.time()
        return node.id

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and all its edges."""
        if node_id not in self.nodes:
            return False
        del self.nodes[node_id]
        self.edges = [e for e in self.edges if e.source_id != node_id and e.target_id != node_id]
        self.updated_at = time.time()
        return True

    def connect(
        self, source_id: str, target_id: str, source_output: str = "output", target_input: str = "input"
    ) -> bool:
        """Connect two nodes."""
        if source_id not in self.nodes or target_id not in self.nodes:
            return False
        # Check whether it would form a cycle (simple check)
        if self._would_create_cycle(source_id, target_id):
            logger.warning(f"connecting {source_id} → {target_id} would form a cycle, blocked")
            return False
        self.edges.append(
            Edge(
                source_id=source_id,
                target_id=target_id,
                source_output=source_output,
                target_input=target_input,
            )
        )
        self.updated_at = time.time()
        return True

    def _would_create_cycle(self, source_id: str, target_id: str) -> bool:
        """Check whether adding an edge would form a cycle (BFS from target to see if source is reachable)."""
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

    @staticmethod
    def _has_cycle(edges: List[Edge]) -> bool:
        """HI-16: cycle detection over a given edge set (Kahn topological sort; nodes left after in-degree-0 exhaustion = cycle).
        from_dict restores all edges at once, bypassing connect's per-edge cycle check, so the check must happen here.
        """
        nodes = set()
        for e in edges:
            nodes.add(e.source_id)
            nodes.add(e.target_id)
        in_degree = {n: 0 for n in nodes}
        for e in edges:
            in_degree[e.target_id] += 1
        queue = [n for n, d in in_degree.items() if d == 0]
        visited_count = 0
        while queue:
            cur = queue.pop(0)
            visited_count += 1
            for e in edges:
                if e.source_id == cur:
                    in_degree[e.target_id] -= 1
                    if in_degree[e.target_id] == 0:
                        queue.append(e.target_id)
        return visited_count != len(nodes)

    def get_upstream_nodes(self, node_id: str) -> List[str]:
        """Get the upstream node ID list of the given node."""
        upstream = []
        for edge in self.edges:
            if edge.target_id == node_id:
                upstream.append(edge.source_id)
        return upstream

    def get_downstream_nodes(self, node_id: str) -> List[str]:
        """Get the downstream node ID list of the given node."""
        downstream = []
        for edge in self.edges:
            if edge.source_id == node_id:
                downstream.append(edge.target_id)
        return downstream

    def topological_sort(self) -> List[str]:
        """Topological sort, returning the node execution order."""
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

        # Check for cycles (unsorted nodes)
        if len(sorted_nodes) != len(self.nodes):
            missing = set(self.nodes.keys()) - set(sorted_nodes)
            logger.error(f"workflow contains a cycle, cannot sort: {missing}")
            return []

        return sorted_nodes

    def get_start_nodes(self) -> List[str]:
        """Get start nodes that have no upstream."""
        all_targets = {e.target_id for e in self.edges}
        return [nid for nid in self.nodes if nid not in all_targets]

    def validate(self) -> List[str]:
        """Validate workflow integrity, returning a list of errors."""
        errors = []
        if not self.nodes:
            errors.append("workflow has no nodes")
            return errors

        start_nodes = self.get_start_nodes()
        if not start_nodes:
            errors.append("workflow has no start node (all nodes have upstream dependencies)")

        # Check isolated nodes — issue #189: under parallel waves, unconnected independent branches are a legal shape
        # (the old version errored directly, so a workflow of "3 dependency-free parallel nodes" could never pass validation);
        # here we only warn, and the scheduler executes them as independent waves
        all_connected: Set[str] = set()
        for edge in self.edges:
            all_connected.add(edge.source_id)
            all_connected.add(edge.target_id)
        isolated = set(self.nodes.keys()) - all_connected
        if len(self.nodes) > 1 and isolated:
            logger.warning(f"workflow contains unconnected nodes, they will run as independent branches: {sorted(isolated)}")

        # Check each node's config
        for nid, node in self.nodes.items():
            node_errors = node.validate_config()
            for e in node_errors:
                errors.append(f"node '{node.config.label or nid}': {e}")

        # issue #480: terminal deliverables must have a verify_objective predecessor (policy-configurable) —
        # MAST FC3: an explicit objective-verification step is the biggest lever on orchestration success rate; intercept at the DAG layer when missing
        if self.verification_policy in ("strict", "lenient"):
            unverified = self._terminal_nodes_without_verification()
            if unverified:
                msg = (
                    f"terminal deliverables missing verify_objective predecessor: {sorted(unverified)} — "
                    f"add a verify_objective node between the deliverable and the top-level goal (verification_policy={self.verification_policy})"
                )
                if self.verification_policy == "strict":
                    errors.append(msg)
                else:
                    logger.warning(f"workflow '{self.name}': {msg}")

        return errors

    def _terminal_nodes_without_verification(self) -> Set[str]:
        # Terminal nodes = no outgoing edges; considered verified if itself or any ancestor is verify_objective
        outgoing = {e.source_id for e in self.edges}
        terminals = [nid for nid in self.nodes if nid not in outgoing]
        if not terminals:
            return set()
        incoming: Dict[str, List[str]] = {}
        for e in self.edges:
            incoming.setdefault(e.target_id, []).append(e.source_id)
        verified: Set[str] = set()
        for terminal in terminals:
            stack = [terminal]
            seen: Set[str] = set()
            ok = False
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                node = self.nodes.get(cur)
                if node is not None and node.name == "verify_objective":
                    ok = True
                    break
                stack.extend(incoming.get(cur, []))
            if not ok:
                verified.add(terminal)
        return verified

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the workflow to a dict."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "verification_policy": self.verification_policy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "edges": [
                {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "source_output": e.source_output,
                    "target_input": e.target_input,
                    "label": e.label,
                }
                for e in self.edges
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize the workflow to a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Workflow:
        """Deserialize a workflow from a dict.

        Supports two node formats:
        - dict: {"n1": {...}, "n2": {...}}  (standard format)
        - list: [{"id": "n1", ...}, ...]     (template format)
        """
        wf = cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            workflow_id=data.get("id", ""),
            tags=data.get("tags", []),
            verification_policy=data.get("verification_policy", "lenient"),
        )
        # Restore nodes (supporting both dict and list formats)
        nodes_data = data.get("nodes", {})
        if isinstance(nodes_data, list):
            for node_data in nodes_data:
                nid = node_data.get("id", "")
                node_class = NodeRegistry.get(node_data.get("name", ""))
                if node_class:
                    node = node_class.from_dict(node_data)
                    wf.nodes[node.id] = node
                else:
                    logger.warning(f"unknown node type: {node_data.get('name')}, skipping")
        elif isinstance(nodes_data, dict):
            for nid, node_data in nodes_data.items():
                node_class = NodeRegistry.get(node_data.get("name", ""))
                if node_class:
                    node = node_class.from_dict(node_data)
                    wf.nodes[node.id] = node
                else:
                    logger.warning(f"unknown node type: {node_data.get('name')}, skipping")
        # Restore edges (issue #135 F-2: uniformly go through Edge.from_dict, which raises a readable ValueError for malformed edge shapes)
        for edge_data in data.get("edges", []):
            wf.edges.append(Edge.from_dict(edge_data))
        # HI-16: from_dict restores all edges at once, bypassing connect's per-edge cycle check, so the check must happen here
        if wf._has_cycle(wf.edges):
            logger.warning(f"deserialized workflow '{wf.name}' contains a cycle, rejected (from_dict)")
            raise ValueError(f"workflow '{wf.name}' contains a cycle: the edge set has circular dependencies")
        wf.created_at = data.get("created_at", time.time())
        wf.updated_at = data.get("updated_at", time.time())
        return wf

    @classmethod
    def from_json(cls, json_str: str) -> Workflow:
        """Deserialize a workflow from a JSON string."""
        return cls.from_dict(json.loads(json_str))


_REPAIR_SYSTEM_PROMPT = (
    "You are a workflow node repair advisor. A node failed after exhausting its retry budget. "
    'Reply with JSON only: {"action": "retry"|"skip"|"abort", "params": {}}. '
    'Use "retry" with corrected "params" only if a parameter fix can plausibly resolve the error; '
    '"skip" if the step is optional for the overall goal; "abort" otherwise.'
)


def _extract_repair_json(content: str) -> Dict[str, Any]:
    # exp/dynamic-orchestration: robust repair-decision extraction — brace scan +
    # json.loads, any failure means abort (fail-closed, no silent proceed).
    text = (content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return {"action": "abort", "params": {}}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"action": "abort", "params": {}}
    action = obj.get("action") if isinstance(obj, dict) else None
    if action not in ("retry", "skip", "abort"):
        return {"action": "abort", "params": {}}
    params = obj.get("params")
    return {"action": action, "params": params if isinstance(params, dict) else {}}


class WorkflowEngine:
    """Workflow execution engine — executes workflows and manages execution state.

    Similar to n8n's execution engine but fully local, lightweight, and async.
    Supports:
    - Topological sort + ready-set wave execution (concurrency=1 serial, >1 parallel within a wave — CLAIM: verified by tests/test_engine.py)
    - Error retry
    - Execution cancellation
    - Progress callbacks
    """

    def __init__(
        self,
        permission_manager=None,
        hook_manager=None,
        session_store=None,
        event_emitter=None,
        trajectory_recorder=None,
        budget_tracker=None,
        hitl_pause=None,
        node_timeout_s: float | None = None,
        approval_journal=None,
        kernel_checkpoints=None,
        hitl_gate=None,
        ops_telemetry=None,
        repair_client=None,
    ):
        self._executions: Dict[str, WorkflowExecution] = {}
        self._cancel_flags: Dict[str, bool] = {}
        # W8: per-execution pause — asyncio.Event, set=running / clear=paused; checkpoint wait between nodes
        self._pause_events: Dict[str, asyncio.Event] = {}
        # issue #187: HITL on by default — high-risk nodes pending local approval pause at node boundaries, and resume in place after confirm;
        # the fail-closed philosophy no longer depends on an env nobody sets. Opt-out must be explicit: FUSION_COWORK_HITL_PAUSE=0 (headless)
        if hitl_pause is None:
            raw = os.environ.get("FUSION_COWORK_HITL_PAUSE", "").strip().lower()
            hitl_pause = raw not in ("0", "false", "off", "no")
        self._hitl_pause = bool(hitl_pause)
        # issue #260: per-run HITL gate override — unattended runs can fail fast per run without changing the global env
        self._hitl_pause_overrides: Dict[str, bool] = {}
        # issue #187: approval wait cap — auto-deny on timeout, never silently let through
        self._hitl_timeout = _parse_hitl_timeout(os.environ.get("FUSION_COWORK_HITL_TIMEOUT", "1800"))
        # issue #404: HITL email notification callback — wired by create_web_app (when SMTP_HOST exists), the engine just calls it
        self.hitl_notifier: Optional[Any] = None
        # issue #257: node-level watchdog — nodes without an explicit timeout default to a 1800s cap, failing structurally on timeout;
        # FUSION_COWORK_NODE_TIMEOUT_S=0 disables the default watchdog (restores old behavior)
        if node_timeout_s is None:
            raw = os.environ.get("FUSION_COWORK_NODE_TIMEOUT_S", "").strip()
            node_timeout_s = float(raw) if raw else 1800.0
        self._node_timeout_default = max(0.0, float(node_timeout_s))
        # issue #257: long-node heartbeat — emits PROGRESS events periodically so "running" and "hung" are distinguishable
        raw_hb = os.environ.get("FUSION_COWORK_NODE_HEARTBEAT_S", "").strip()
        self._node_heartbeat_s = float(raw_hb) if raw_hb else 30.0
        # exec_id -> wait deadline (epoch ts), for frontend countdown
        self._hitl_deadlines: Dict[str, float] = {}
        # exec_id -> approval decision (True=approved / False=denied), written by resolve_hitl
        self._hitl_decisions: Dict[str, bool] = {}
        # issue #187: set of runs granted one-shot approval — skips permission re-check after wake (rule not persisted)
        self._hitl_once_grants: Set[str] = set()
        # issue #225: exec_id -> param overrides edited at approval time — merged into the gated node's input after approval wake
        self._hitl_param_overrides: Dict[str, Dict[str, Any]] = {}
        # issue #303: exec_id -> decision attribution {approval_id, decided_by, decided_at} — lands on the blocked step after wake
        self._hitl_attributions: Dict[str, Dict[str, Any]] = {}
        # issue #309: exec_id -> per-item decision items[] — merged into the gated node's input as batch_verdict after wake
        self._hitl_batch_verdicts: Dict[str, List[Dict[str, Any]]] = {}
        # issue #508: exec_id -> structured human reply — merged into the gated node's input/output as human_reply after wake
        self._hitl_replies: Dict[str, Dict[str, Any]] = {}
        # issue #328: revision loop — stashed review comments on deny + pending revision request + comment injection on rerun + count ledger
        self._hitl_comments: Dict[str, str] = {}
        self._pending_revisions: Dict[str, Dict[str, Any]] = {}
        self._revision_comments: Dict[str, Dict[str, str]] = {}
        self._revision_counts: Dict[str, Dict[str, int]] = {}
        # Set of runs currently parked at a HITL wait point (distinct from W8 manual pause)
        self._hitl_waiting: Set[str] = set()
        # issue #508: exec_id -> waiting node id — makes the awaiting_human branch state visible
        self._hitl_waiting_node: Dict[str, str] = {}
        # W8/B1: run redirect directive queue — applied at node boundaries (set_params/skip_remaining/insert_node)
        self._directives: Dict[str, List[Dict[str, Any]]] = {}
        # issue #579: interrupt delivery mode — exec_id -> event, set by add_directive(mode=interrupt);
        # the wave wait races against it — a hit voids in-flight node attempts, and directives are applied in enqueue order at the boundary then rerun
        self._interrupt_events: Dict[str, asyncio.Event] = {}
        self._progress_callbacks: List[callable] = []
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._session_store = session_store
        # issue #463: approval decision journal (durable HITL) — zero behavior change when not injected
        self._approval_journal = approval_journal
        # issue #470: kernel checkpoint binding (harness CheckpointStore + ReceiptCache) —
        # zero behavior change when not injected; when injected, a pre-state checkpoint is recorded
        # before each node executes, a receipt is recorded on success, and on re-dispatch of the same
        # run_id committed nodes replay from the receipt instead of re-executing
        self._kernel_checkpoints = kernel_checkpoints
        # issue #527: HITL approval queue goes through the shared-kernel HumanGate (fusion-harness #66) —
        # park point ask(policy="block", ttl_s) + wait(), approval side resolve() single winner,
        # lazy expiry at ttl; approval routing policy (guard/gate tools/journal/once) still all in cowork.
        # An injected hitl_gate can replace it (tests); if not injected it is lazily built on first park
        # (FUSION_COWORK_HITL_GATE_DIR, fallback ~/.fusion-cowork/hitl_gate) —
        # reading the queue from disk on every engine construction is costly; runs that never enter HITL pay zero
        self._hitl_gate = hitl_gate
        # issue #545: DAG run/node telemetry (flywheel Loop D) — None = off, zero cost;
        # inject OpsTelemetry (wired by create_web_app per FUSION_OPS_DSN)
        self._ops_telemetry = ops_telemetry
        # exp/dynamic-orchestration: LLM-informed one-shot node repair (docs/pr-repair-step.md)
        # None = lazily build FusionMLXClient, only touched when a node sets on_error_repair=True; tests can inject a stub
        self._repair_client = repair_client
        # exec_id -> the kernel question id currently parked — for single-winner resolve + resume/cancel bridging
        self._hitl_questions: Dict[str, str] = {}
        # issue #554: kernel destructive_gate — issues a single-redeem token after approval passes;
        # exec_id -> token (executors can redeem to verify, replays rejected); gate lazily built
        self._approval_gate = None
        self._approval_tokens: Dict[str, str] = {}
        self._trajectory_recorder = trajectory_recorder
        self._event_emitter = event_emitter
        # HI-19: budget tracking — checked at the top of the node loop; over budget with enforce aborts execution
        self._budget_tracker = budget_tracker
        # R-1: _executions cap; evicts the oldest terminal entries beyond it (old version only grew, a memory leak for long-running engines)
        self._max_executions = 100
        # issue #228: exec_id -> workspace id — capability gates judge by the workspace capability pool (unset = empty, zero behavior change)
        self._run_workspace: Dict[str, str] = {}
        # issue #465: set of nodes executing within a wave — redirect directives only take effect at wave boundaries; in-wave targets are structurally rejected
        self._wave_running: Dict[str, Set[str]] = {}
        # issue #465: exec_id -> structured redirect rejection (enqueued when add_directive rejects synchronously, fetched by the API)
        self._directive_errors: Dict[str, List[Dict[str, Any]]] = {}
        # issue #465: exec_id -> the running workflow reference — directive structural validation (node/edge existence)
        self._run_workflows: Dict[str, Any] = {}

    def pop_directive_error(self, execution_id: str) -> Optional[Dict[str, Any]]:
        # issue #465: take the most recent structured redirect rejection (directly usable as API 409/400 detail)
        errors = self._directive_errors.get(execution_id)
        if not errors:
            return None
        err = errors[-1]
        self._directive_errors[execution_id] = []
        return err

    def _reject_directive(self, execution_id: str, code: str, message: str) -> None:
        self._directive_errors.setdefault(execution_id, []).append({"code": code, "message": message})
        logger.warning(f"run redirect rejected: exec={execution_id} code={code} {message}")

    def set_run_workspace(self, execution_id: str, workspace_id: str) -> None:
        if execution_id:
            self._run_workspace[execution_id] = str(workspace_id or "")
            logger.debug(f"[{execution_id}] run workspace: {self._run_workspace[execution_id] or '-'}")

    def on_progress(self, callback: callable) -> None:
        """Register a progress callback."""
        self._progress_callbacks.append(callback)

    def _notify_progress(self, execution: WorkflowExecution, step: WorkflowStep) -> None:
        """Notify all progress callbacks."""
        for cb in self._progress_callbacks:
            try:
                cb(execution, step)
            except Exception as e:
                logger.error(f"progress callback error: {e}")

    def _resolve_concurrency(self, requested: int) -> int:
        # issue #189: explicit argument wins; else read env; default 1 (keeps old serial semantics; production start.sh sets 4)
        try:
            if requested and int(requested) > 0:
                return int(requested)
        except (TypeError, ValueError):
            logger.warning(f"invalid concurrency cap: {requested!r}, ignored")
        raw = os.environ.get("FUSION_COWORK_MAX_CONCURRENCY", "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                logger.warning(f"FUSION_COWORK_MAX_CONCURRENCY invalid: {raw!r}, using 1")
        return 1

    async def _llm_repair_decision(
        self, exec_id: str, node: BaseNode, node_input: Dict[str, Any], error: str, attempt_errors: List[str]
    ) -> Dict[str, Any]:
        # exp/dynamic-orchestration: one LLM call decides retry/skip/abort after
        # retry exhaustion. Any failure path (client init, HTTP, parse) = abort,
        # so the repair feature can never turn a crash into a silent pass.
        if self._repair_client is None:
            try:
                from ..ai.mlx_client import FusionMLXClient

                self._repair_client = FusionMLXClient()
            except Exception as init_err:
                logger.warning(f"[{exec_id}] llm_repair: client init failed, abort: {init_err}")
                return {"action": "abort", "params": {}}
        prompt = {
            "node": node.name,
            "node_type": node.config.executor,
            "params": _sanitize_params(dict(node.config.params or {})),
            "input": _sanitize_params(node_input),
            "error": (error or "")[:500],
            "attempt_errors": [e[:200] for e in (attempt_errors or [])[-5:]],
        }
        messages = [
            {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)[:6000]},
        ]
        try:
            model = (
                (node.config.model or "").strip()
                or os.environ.get("FUSION_MLX_MODEL", "").strip()
                or "Qwen3.8-27B-4bit"
            )
            resp = await self._repair_client.chat(
                model=model,
                messages=messages,
                temperature=0.0,
                max_tokens=300,
            )
            content = resp.content if hasattr(resp, "content") else str(resp)
        except Exception as llm_err:
            logger.warning(f"[{exec_id}] llm_repair: LLM call failed, abort: {llm_err}")
            return {"action": "abort", "params": {}}
        decision = _extract_repair_json(content)
        logger.info(f"[{exec_id}] llm_repair: node={node.name} action={decision['action']}")
        return decision

    async def execute(
        self,
        workflow: Workflow,
        initial_input: Optional[Dict[str, Any]] = None,
        execution_id: str = "",
        resume_steps: Optional[List[Dict[str, Any]]] = None,
        start_paused: bool = False,
        concurrency: int = 0,
    ) -> WorkflowExecution:
        """Execute the workflow.

        resume_steps: checkpoint resume — snapshots (to_dict) of already-completed steps.
        Hit nodes skip execution; their output_data is cached as downstream input and execution continues from the next node.
        start_paused: issue #191 — a resumed run starts paused, suspending at the first node boundary to wait for resume.
        concurrency: issue #189 — cap on parallel nodes within a wave; 0 = FUSION_COWORK_MAX_CONCURRENCY (default 1 serial).
        """
        exec_id = execution_id or f"exec_{uuid.uuid4().hex[:12]}"
        # issue #464: OTel spine — one trace_id (the exec_id) for the whole run, shared by LLM outbound headers/wave spans/
        # trajectory recording; execute isolates each task context so nothing leaks across runs
        set_trace_id(exec_id)
        effective_concurrency = self._resolve_concurrency(concurrency)
        execution = WorkflowExecution(
            id=exec_id,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            status=WorkflowStatus.RUNNING,
            started_at=time.time(),
            concurrency=effective_concurrency,
            executors=executor_names(list(workflow.nodes.values())),
        )
        self._executions[exec_id] = execution
        self._cancel_flags[exec_id] = False
        pause_evt = asyncio.Event()
        if not start_paused:
            pause_evt.set()
        self._pause_events[exec_id] = pause_evt
        # R-1: evict the oldest terminal entries when _executions exceeds the cap, preventing a long-running engine memory leak
        self._trim_executions()

        # issue #120: run-scoped log header — grep "[<run_id>]" to rebuild the timeline
        # issue #187: the first line records the HITL mode — auditable for headless, not silent
        hitl_mode = "pause" if self._hitl_pause_overrides.get(exec_id, self._hitl_pause) else "headless"
        logger.info(
            f"[{exec_id}] run start: workflow={workflow.name} nodes={len(workflow.nodes)} "
            f"hitl_mode={hitl_mode} hitl_timeout={self._hitl_timeout:g}s "
            f"concurrency={effective_concurrency} executors={execution.executors}"
        )
        # issue #545: Loop D telemetry — only sends definition hash/counts, never node inputs/outputs (tenant data red line)
        _wf_hash = workflow_definition_hash(workflow)
        if self._ops_telemetry is not None:
            await self._ops_telemetry.run_started(exec_id, _wf_hash, len(workflow.nodes), hitl_mode=hitl_mode)

        # Session: auto-create — issue #460: when the resume snapshot carries a session_id, adopt the original session
        # (reuse rather than create, preserving the agent seat's multi-turn history; missing field in old snapshots = create as before)
        session = None
        resume_session_id = ""
        for sstep in resume_steps or []:
            if isinstance(sstep, dict) and sstep.get("session_id"):
                resume_session_id = str(sstep["session_id"])
                break
        if self._session_store:
            from .session import Session

            if resume_session_id:
                session = self._session_store.get(resume_session_id)
                if session is not None:
                    session.status = "running"
                    self._session_store.save(session)
                    logger.info(f"[{exec_id}] resume adopted snapshot session {session.id} (session reuse)")
                else:
                    logger.warning(f"[{exec_id}] resume snapshot session not found, creating new: {resume_session_id}")
            if session is None:
                session = Session(
                    workflow_id=workflow.id,
                    workflow_name=workflow.name,
                    status="running",
                    initial_input=initial_input or {},
                    execution_id=exec_id,
                )
                self._session_store.save(session)
                logger.debug(f"Session auto-created: {session.id}")

        # D1 trajectory flywheel: bind session_id and attach the Hook handler
        if self._trajectory_recorder is not None:
            if session is not None:
                self._trajectory_recorder.bind_session(session.id)
            self._trajectory_recorder.attach()

        # Event: WORKFLOW_START
        if self._event_emitter:
            from .events import EventType

            self._event_emitter.create_event(
                EventType.WORKFLOW_START,
                execution_id=exec_id,
                data={"workflow_id": workflow.id, "workflow_name": workflow.name},
            )

        # Validate the workflow
        errors = workflow.validate()
        if errors:
            execution.status = WorkflowStatus.FAILED
            execution.error = "; ".join(errors)
            execution.completed_at = time.time()
            execution.total_time = execution.completed_at - execution.started_at
            return execution

        # Topological sort
        order = workflow.topological_sort()
        if not order:
            execution.status = WorkflowStatus.FAILED
            execution.error = "workflow contains a cycle or cannot be sorted"
            execution.completed_at = time.time()
            execution.total_time = execution.completed_at - execution.started_at
            return execution

        # Execution result cache
        node_results: Dict[str, NodeResult] = {}
        passed_data: Dict[str, Dict[str, Any]] = {}

        # Checkpoint resume: restore cache from completed step snapshots (P1-2)
        completed_ids: set = set()
        if resume_steps:
            # HI-16: verify snapshot node ids ⊆ current workflow nodes, rejecting topology mismatch (prevents old snapshot pollution)
            snap_ids = {
                sstep.get("node_id", "") for sstep in resume_steps if isinstance(sstep, dict) and sstep.get("node_id")
            }
            stale = snap_ids - set(workflow.nodes.keys())
            if stale:
                logger.warning(f"resume snapshot contains nodes not in the current workflow {stale}, ignoring those steps")
            for sstep in resume_steps:
                if not isinstance(sstep, dict):
                    continue
                s_status = str(sstep.get("status", "")).lower()
                if s_status not in ("success", "completed"):
                    continue
                nid = sstep.get("node_id", "")
                if not nid:
                    continue
                out = sstep.get("output_data") or {}
                passed_data[nid] = dict(out) if isinstance(out, dict) else {}
                node_results[nid] = NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=passed_data[nid],
                    summary=sstep.get("summary", "restored from snapshot"),
                )
                completed_ids.add(nid)
            if completed_ids:
                logger.info(f"checkpoint resume: restored {len(completed_ids)} completed steps, skipping rerun")

        # Initial data passed to start nodes
        if initial_input:
            start_nodes = workflow.get_start_nodes()
            for sn in start_nodes:
                passed_data[sn] = initial_input
        # issue #460: adopted session injected into node input — agent seat nodes read _session_id to reuse the original session
        # (underscore prefix = engine-internal field, excluded from interpolation)
        if session is not None and resume_session_id:
            for sn in workflow.get_start_nodes():
                base = passed_data.get(sn)
                d = dict(base) if isinstance(base, dict) else {}
                d.setdefault("_session_id", session.id)
                passed_data[sn] = d
            logger.debug(f"[{exec_id}] resume session _session_id injected into start node inputs")

        # Hook: WORKFLOW_START
        if self._hook_manager:
            ctx = await self._hook_manager.fire(
                HookEvent.WORKFLOW_START,
                {
                    "execution_id": exec_id,
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                },
            )
            if ctx and ctx.cancelled:
                execution.status = WorkflowStatus.CANCELLED
                execution.completed_at = time.time()
                execution.total_time = execution.completed_at - execution.started_at
                return execution

        try:
            # issue #465: structural validation reference — add_directive synchronously validates node/edge existence
            self._run_workflows[exec_id] = workflow
            # issue #579: interrupt event registered with the run — _wait_wave and add_directive
            # share the same object so interrupts enqueued after the wave starts can be observed in the race
            self._interrupt_events.setdefault(exec_id, asyncio.Event())
            state = _RunState(
                exec_id=exec_id,
                workflow=workflow,
                execution=execution,
                node_results=node_results,
                passed_data=passed_data,
                completed_ids=completed_ids,
                hitl_lock=asyncio.Lock(),
            )
            order_idx = 0
            done: Set[str] = set()
            while True:
                # Check cancellation
                if self._cancel_flags.get(exec_id, False):
                    execution.status = WorkflowStatus.CANCELLED
                    break

                # W8: pause checkpoint — suspend after this wave completes; resume/cancel wakes it
                pause_evt = self._pause_events.get(exec_id)
                if pause_evt is not None and not pause_evt.is_set():
                    logger.info(f"execution paused, waiting for resume: {exec_id}")
                    await pause_evt.wait()
                    if self._cancel_flags.get(exec_id, False):
                        execution.status = WorkflowStatus.CANCELLED
                        break
                    logger.info(f"execution resumed: {exec_id}")

                # issue #189: advance the cursor to the first unexecuted node before applying redirect directives —
                # under wave execution order_idx still points at nodes completed this wave; applying directly would
                # land set_params' default target on an already-executed node (param overrides discarded), and
                # skip_remaining would also record an extra skipped step for completed nodes.
                while order_idx < len(order) and order[order_idx] in done:
                    order_idx += 1
                # W8/B1: apply queued run redirect directives at the node boundary, then take the next batch of nodes
                order_idx = self._apply_directives(
                    exec_id, workflow, order, order_idx, node_results, passed_data, execution
                )
                while order_idx < len(order) and order[order_idx] in done:
                    order_idx += 1
                if order_idx >= len(order):
                    break

                # HI-19: budget check — abort when enforce and the budget is exceeded (kernel funnel contract is_exceeded)
                if self._budget_tracker is not None and self._budget_tracker.is_exceeded():
                    spent_usd = self._budget_tracker.record.cost_usd
                    logger.warning(
                        f"budget exhausted ${spent_usd:.4f} >= "
                        f"${self._budget_tracker.max_budget_usd:.4f}, aborting execution exec_id={exec_id}"
                    )
                    execution.status = WorkflowStatus.FAILED
                    execution.error = "budget exhausted: execution aborted"
                    break

                # issue #189: ready-set waves — nodes whose predecessors all completed run in the same wave.
                # With concurrency=1 each wave takes only the topological-order head, equivalent to the old serial loop.
                unscheduled = order[order_idx:]
                ready: List[str] = []
                for nid in unscheduled:
                    preds = {e.source_id for e in workflow.edges if e.target_id == nid}
                    if preds <= done:
                        ready.append(nid)
                if not ready:
                    logger.warning(f"[{exec_id}] no ready nodes (predecessors unscheduled), force-advancing in topological order: {unscheduled[0]}")
                    ready = unscheduled[:1]
                wave = ready[:1] if effective_concurrency <= 1 else ready[:effective_concurrency]

                logger.debug(f"[{exec_id}] wave start: concurrency={effective_concurrency} nodes={wave}")
                # issue #465: register in-wave nodes — wave-boundary defense line for redirect directives (defense 4)
                self._wave_running[exec_id] = set(wave)
                # issue #464: OTel spine — one span per wave, attributes carry the node list
                from ..observability.tracing import span

                with span("workflow.wave", run=exec_id, nodes=",".join(wave)):
                    # issue #579: interrupt race — for runs with a queued interrupt directive,
                    # the wave wait races the interrupt event; a hit voids in-flight attempts
                    # (no result step recorded, only an audit marker), directives applied at the boundary then rerun
                    tasks = [asyncio.ensure_future(self._run_node(state, nid)) for nid in wave]
                    results, interrupted, voided = await self._wait_wave(exec_id, wave, tasks)
                self._wave_running.pop(exec_id, None)
                if interrupted:
                    for nid in voided:
                        marker = WorkflowStep(
                            node_id=nid,
                            node_name=workflow.nodes[nid].name if nid in workflow.nodes else nid,
                            node_display_name=(
                                workflow.nodes[nid].config.label or workflow.nodes[nid].display_name
                                if nid in workflow.nodes
                                else nid
                            ),
                            status=NodeStatus.SKIPPED,
                            started_at=time.time(),
                            summary="user interrupt: node execution voided, will rerun after directives are applied",
                            user_correction=True,
                        )
                        marker.completed_at = time.time()
                        execution.steps.append(marker)
                        self._notify_progress(execution, marker)
                    logger.info(f"[{exec_id}] run interrupted: in-wave nodes voided pending rerun nodes={sorted(voided)}")

                # issue #189: concurrent completion order is nondeterministic; steps are persisted in topological order for determinism
                position = {nid: idx for idx, nid in enumerate(order)}
                collected: List[Tuple[int, WorkflowStep]] = []
                control = "continue"
                control_error = ""
                for nid, (steps, node_control, node_error) in zip(wave, results):
                    # issue #579: interrupted-and-voided nodes are not marked done — they rerun in the next wave after directives are applied
                    if interrupted and nid in voided:
                        continue
                    done.add(nid)
                    for step in steps:
                        collected.append((position.get(step.node_id, len(order)), step))
                    if node_control == "cancel":
                        control = "cancel"
                        control_error = node_error or control_error
                    elif node_control == "fail" and control != "cancel":
                        control = "fail"
                        control_error = node_error or control_error
                for _, step in sorted(collected, key=lambda item: item[0]):
                    # issue #460: step-level session attribution — non-marker steps carry the session id with the snapshot
                    if session is not None and step.session_id is None and not step.user_correction:
                        step.session_id = session.id
                    execution.steps.append(step)
                    self._notify_progress(execution, step)
                if control == "cancel":
                    execution.status = WorkflowStatus.CANCELLED
                    break
                if control == "fail":
                    # issue #328: revision loop — a deny is not terminal; return to the producing node and rerun with the review comments
                    rev = self._pending_revisions.pop(exec_id, None)
                    gate_id = str((rev or {}).get("gate_node") or "")
                    if rev is not None and rev.get("target") in workflow.nodes:
                        target = str(rev["target"])
                        done.discard(target)
                        if gate_id:
                            done.discard(gate_id)
                            passed_data.pop(gate_id, None)
                            node_results.pop(gate_id, None)
                        passed_data.pop(target, None)
                        node_results.pop(target, None)
                        self._revision_comments.setdefault(exec_id, {})[target] = str(rev.get("comments") or "")
                        order_idx = order.index(target) if target in order else order_idx
                        logger.info(f"[{exec_id}] revise: returning to node '{target}' for revision, run continues")
                        continue
                    execution.status = WorkflowStatus.FAILED
                    execution.error = control_error
                    break

            # Final status
            if execution.status == WorkflowStatus.RUNNING:
                # issue #187: mark completed runs that had permission-denied nodes separately — delivery shows "completed with denials"
                if any(s.status == NodeStatus.DENIED for s in execution.steps):
                    execution.status = WorkflowStatus.COMPLETED_WITH_DENIALS
                    logger.info(
                        f"[{exec_id}] run end: terminal status completed_with_denials (denied={sum(1 for s in execution.steps if s.status == NodeStatus.DENIED)})"
                    )
                else:
                    execution.status = WorkflowStatus.SUCCESS
            elif execution.status == WorkflowStatus.FAILED:
                pass  # already set

        except Exception as e:
            execution.status = WorkflowStatus.FAILED
            execution.error = f"workflow engine exception: {e}"
            logger.exception(f"workflow execution exception: {e}")

        finally:
            execution.completed_at = time.time()
            execution.total_time = execution.completed_at - execution.started_at
            # issue #465: exception bypasses also clear in-wave state (the terminal cleanup block only covers the normal loop-exit path)
            self._wave_running.pop(exec_id, None)
            self._interrupt_events.pop(exec_id, None)
            self._run_workflows.pop(exec_id, None)

            # Hook: WORKFLOW_END / WORKFLOW_CANCEL
            if self._hook_manager:
                if execution.status == WorkflowStatus.CANCELLED:
                    await self._hook_manager.fire(
                        HookEvent.WORKFLOW_CANCEL,
                        {
                            "execution_id": exec_id,
                            "workflow_id": workflow.id,
                        },
                    )
                await self._hook_manager.fire(
                    HookEvent.WORKFLOW_END,
                    {
                        "execution_id": exec_id,
                        "workflow_id": workflow.id,
                        "status": execution.status.value,
                        "total_time": execution.total_time,
                    },
                )

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
                    {
                        "node_id": s.node_id,
                        "node_name": s.node_name,
                        "node_display_name": s.node_display_name,
                        "status": s.status.value if hasattr(s.status, "value") else s.status,
                        "execution_time": s.execution_time,
                        "error": s.error,
                        "summary": s.summary,
                        "input_data": s.input_data,
                        "output_data": s.output_data,
                        # issue #460: session attribution exposed via session steps_snapshot
                        "session_id": s.session_id,
                    }
                    for s in execution.steps
                ]
                self._session_store.update_steps(session.id, steps_data)
                self._session_store.update_status(session.id, execution.status.value, completed_at=time.time())

            # issue #120: run-scoped run end — outcome/total/steps
            logger.info(
                f"[{exec_id}] run end:    outcome={execution.status.value} "
                f"total={execution.total_time:.3f}s steps={len(execution.steps)} "
                f"concurrency={execution.concurrency} executors={execution.executors}"
            )
            # issue #545: run terminal-state telemetry — outcome + total duration
            if self._ops_telemetry is not None:
                await self._ops_telemetry.run_finished(
                    exec_id,
                    execution.status.value,
                    int(execution.total_time * 1000),
                    workflow_hash=_wf_hash,
                )

            # Build summary
            success_count = sum(1 for s in execution.steps if s.status == NodeStatus.SUCCESS)
            total_count = len(execution.steps)
            executor_text = ", ".join(f"{k}:{v}" for k, v in sorted(execution.executors.items()))
            execution.result_summary = (
                f"workflow '{workflow.name}' finished: "
                f"{success_count}/{total_count} nodes succeeded, "
                f"took {execution.total_time:.2f}s, "
                f"concurrency={execution.concurrency} executors={executor_text or 'simple:0'}"
            )

            del self._cancel_flags[exec_id]
            self._pause_events.pop(exec_id, None)
            self._directives.pop(exec_id, None)
            self._run_workspace.pop(exec_id, None)
            # issue #465: in-wave state / structural validation reference / rejection queue cleaned up on run terminal state
            self._wave_running.pop(exec_id, None)
            self._run_workflows.pop(exec_id, None)
            self._directive_errors.pop(exec_id, None)
            self._hitl_pause_overrides.pop(exec_id, None)
            # issue #300: sweep HITL residue when the run reaches terminal state — _hitl_waiting/_hitl_deadlines
            # are normally cleared on the _hitl_await wake/timeout paths, but any bypass (cancel/exception/race)
            # that misses them would make GET /api/runs/{id} return hitl_pending=true forever on terminal runs.
            # Only clear stranded pending-approval items in the permission queue when the run truly entered HITL waiting —
            # deny-fast (issue #124) local pending approvals must survive run terminal state for the approval card.
            hitl_residue = exec_id in self._hitl_waiting or exec_id in self._hitl_deadlines
            if hitl_residue:
                logger.warning(f"[{exec_id}] run terminal state cleaning up HITL residue (waiting/deadline)")
            self._hitl_waiting.discard(exec_id)
            self._hitl_waiting_node.pop(exec_id, None)
            self._hitl_deadlines.pop(exec_id, None)
            # issue #527: best-effort cancel of the kernel HumanGate question on run terminal state (bypass-leak defense)
            self._hitl_gate_cancel(exec_id)
            # issue #554: best-effort token record cleanup on terminal state
            self._approval_tokens.pop(exec_id, None)
            self._hitl_attributions.pop(exec_id, None)
            self._hitl_batch_verdicts.pop(exec_id, None)
            self._hitl_replies.pop(exec_id, None)
            # issue #328: best-effort cleanup of revision-loop state
            self._hitl_comments.pop(exec_id, None)
            self._pending_revisions.pop(exec_id, None)
            self._revision_comments.pop(exec_id, None)
            self._revision_counts.pop(exec_id, None)
            if hitl_residue:
                discard = getattr(self._permission_manager, "discard_pending_by_execution", None)
                if callable(discard):
                    removed = discard(exec_id)
                    if removed:
                        logger.warning(f"[{exec_id}] run terminal state clearing stranded pending approvals: {removed}")

        return execution

    async def _run_node(self, state: _RunState, node_id: str) -> Tuple[List[WorkflowStep], str, str]:
        """Execute a single node, returning (step list, control directive, error).

        Control directives: continue=keep scheduling / fail=fail the whole run / cancel=cancel the whole run.
        Steps are persisted by the scheduler in topological order, keeping execution record order deterministic under concurrency.
        """
        workflow = state.workflow
        exec_id = state.exec_id
        node_results = state.node_results
        passed_data = state.passed_data
        node = workflow.nodes.get(node_id)
        if node is None:
            logger.warning(f"[{exec_id}] node to execute missing, skipping: {node_id}")
            return [], "continue", ""
        executor = executor_for_node(node)
        # issue #382: outbox context — side-effect writes use {run_id, node_id} as an idempotency receipt
        from .outbox import get_outbox_bridge, outbox_ctx

        _outbox = get_outbox_bridge()
        _ob_token = outbox_ctx.set({"run_id": exec_id, "node_id": node_id, "node_name": node.name} if _outbox else None)

        # Checkpoint resume: completed nodes skip execution (P1-2)
        if node_id in state.completed_ids:
            cached = node_results.get(node_id)
            skip_step = WorkflowStep(
                node_id=node_id,
                node_name=node.name,
                node_display_name=node.config.label or node.display_name,
                status=NodeStatus.SKIPPED,
                started_at=time.time(),
                input_data=passed_data.get(node_id, {}),
                output_data=cached.data if cached else {},
                summary="restored from snapshot, rerun skipped",
                executor=executor.name,
            )
            skip_step.completed_at = time.time()
            skip_step.execution_time = 0
            logger.debug(f"checkpoint resume: skipping node '{node.name}' (restored from snapshot)")
            return [skip_step], "continue", ""

        # Collect input data
        # A-1: port routing wiring — edge.source_output/target_input were previously decorative (execute merged all upstream).
        # Default edges (source_output="output", target_input="input") keep full merge (backward compatible;
        # existing nodes read inputs.get("data") etc.); non-default explicit ports → take by source_output,
        # key by target_input, eliminating fan-out nodes' multi-path inputs polluting each other.
        node_input = {}
        in_edges = [e for e in workflow.edges if e.target_id == node_id]
        for edge in in_edges:
            up_id = edge.source_id
            if up_id not in passed_data:
                continue
            src = passed_data[up_id]
            if edge.source_output == "output" and edge.target_input == "input":
                node_input.update(src)
            else:
                val = src.get(edge.source_output) if isinstance(src, dict) else src
                if val is not None:
                    node_input[edge.target_input] = val

        # Add initial input
        if node_id in passed_data:
            node_input.update(passed_data[node_id])

        # issue #327: ${step.<id>.<path>} / ${result.<path>} references in params/inputs are resolved here —
        # upstream artifacts (PRD/architecture/scaffold paths) explicitly wired into downstream targets, no more manual copy-paste in templates;
        # config.params replaced in place (every run builds a fresh Workflow via from_dict, no cross-run pollution)
        has_ref = "${step." in json.dumps(node_input, ensure_ascii=False, default=str) or any(
            "${" in str(v) for v in (node.config.params or {}).values()
        )
        if has_ref:
            node_input = _interpolate_step_refs(node_input, passed_data, node_input)
            node.config.params = _interpolate_step_refs(dict(node.config.params or {}), passed_data, node_input)
            # issue #343: leftover result./step./param. refs after interpolation = missing param at assembly or a broken reference chain —
            # fail with a clear error, never let a literal into the node (an int()/float() secondary crash is hard to understand)
            # issue #389/n12: the error must list the upstream fields available to this node, otherwise debugging means digging through the whole chain
            leftover = _find_unresolved_refs(node.config.params or {})
            if leftover:
                available = sorted(str(k) for k in (node_input or {}) if not str(k).startswith("_"))
                msg = (
                    f"unresolved interpolation: {', '.join(dict.fromkeys(leftover))}"
                    f" (available fields: {', '.join(available) if available else '<none>'})"
                )
                logger.error(f"[{exec_id}] {msg} node={node.name}")
                step = WorkflowStep(
                    node_id=node_id,
                    node_name=node.name,
                    node_display_name=node.config.label or node.display_name,
                    status=NodeStatus.FAILED,
                    started_at=time.time(),
                    input_data=node_input,
                    output_data={"unresolved_refs": sorted(set(leftover))},
                    executor=executor.name,
                )
                step.completed_at = time.time()
                step.execution_time = 0
                step.error = msg
                return [step], "fail", msg
            logger.debug(f"[{exec_id}] step reference resolution done: node={node.name}")

        # issue #470: kernel checkpoints — committed nodes replay from receipts (idempotent on same run_id re-dispatch),
        # uncommitted nodes record a pre-state checkpoint before executing (failed runs can be located from the latest checkpoint)
        if self._kernel_checkpoints is not None:
            try:
                replayed_output = self._kernel_checkpoints.replay_receipt(exec_id, node_id)
            except Exception as re_err:
                logger.warning(f"[{exec_id}] kernel receipt replay check failed (ignored): {re_err}")
                replayed_output = None
            if replayed_output is not None:
                logger.info(f"[{exec_id}] kernel checkpoint: node '{node.name}' receipt hit, replaying output without re-executing")
                step = WorkflowStep(
                    node_id=node_id,
                    node_name=node.name,
                    node_display_name=node.config.label or node.display_name,
                    status=NodeStatus.SKIPPED,
                    started_at=time.time(),
                    input_data=node_input,
                    output_data=replayed_output if isinstance(replayed_output, dict) else {"replayed": replayed_output},
                    summary="replayed from kernel checkpoint receipt (committed effects not re-executed)",
                    executor=executor.name,
                )
                step.completed_at = time.time()
                step.execution_time = 0
                return [step], "continue", ""
            try:
                self._kernel_checkpoints.save_node_checkpoint(
                    exec_id, node_id, node.name, node.config.params, node_input
                )
            except Exception as ck_err:
                logger.warning(f"[{exec_id}] kernel checkpoint save failed (ignored): {ck_err}")

        # issue #328: revision loop — when rerunning the producing node, merge review comments into the input (node reads revision_comments)
        rev_map = self._revision_comments.get(exec_id)
        if rev_map and node_id in rev_map:
            node_input["revision_comments"] = rev_map.pop(node_id)
            logger.info(f"[{exec_id}] revise: review comments merged into node input: node={node.name}")

        # issue #283: expose the run id to nodes — approval-task payloads carry
        # run_id so external decisions (agent-studio GUI) route back via webhook.
        node_input.setdefault("run_id", exec_id)

        # issue #422: data-driven conditional skip — when all config.skip_if conditions hit, record this node as SKIPPED
        # (no execution, no permission/HITL entry); the base primitive for verdict-based branch routing
        skip_desc = _skip_if_hit(node.config.skip_if, passed_data, node_input)
        # exp/degrade-semantics: degrade_on_skip runs the node on partial inputs
        # instead of skipping (fusion-horizon v3 flywheel finding); default OFF
        # preserves issue #422 skip semantics exactly
        degraded_desc = ""
        if skip_desc and node.config.degrade_on_skip:
            degraded_desc = skip_desc
            skip_desc = ""
            logger.info(
                f"[{exec_id}] skip_if hit but degrade_on_skip, running '{node.name}' on partial inputs: {degraded_desc}"
            )
        if skip_desc:
            logger.info(f"[{exec_id}] skip_if hit, skipping node '{node.name}': {skip_desc}")
            step = WorkflowStep(
                node_id=node_id,
                node_name=node.name,
                node_display_name=node.config.label or node.display_name,
                status=NodeStatus.SKIPPED,
                started_at=time.time(),
                input_data=node_input,
                output_data={"skip_if": skip_desc},
                summary=f"conditional skip (skip_if: {skip_desc})",
                executor=executor.name,
            )
            step.completed_at = time.time()
            step.execution_time = 0
            return [step], "continue", ""

        # Permission check — issue #189: serialize HITL gates within a run, avoiding approval decisions
        # cross-contaminating each other when multiple nodes share _hitl_decisions[exec_id]
        if self._permission_manager:
            # issue #142: the approval check must see effective params — config.params merged with upstream input
            check_input = {**(node.config.params or {}), **node_input}
            async with state.hitl_lock:
                # issue #138: write approval context (workflow/node_id) for the approval card of high-risk denials
                # issue #181: attach execution_id; HITL confirm routing uses it to wake the paused run
                token = set_approval_context(
                    {
                        "workflow": workflow.name,
                        "node_id": node_id,
                        "execution_id": exec_id,
                        # issue #228: capability gates judge by the workspace capability pool from this
                        "workspace_id": self._run_workspace.get(exec_id, ""),
                    }
                )
                # issue #326: attach upstream artifact evidence — the approval card carries an output snapshot when denied into the pending queue
                etoken = set_approval_evidence(_collect_upstream_evidence(workflow, node, passed_data))
                try:
                    # issue #413: approval_gate require_review=false — a review-exempt gate chosen at intake
                    # auto-passes without permission denial/HITL pause (only this node name is exempt; other high-risk nodes unaffected)
                    # issue #479: template param assembly renders booleans as the string "false" — strict `is False`
                    # misses the string form, letting the gate leak into permission denial/HITL pause; reuse the registry bool coercion before judging
                    if node.name == "approval_gate" and _coerce_bool(check_input.get("require_review")) is False:
                        allowed = True
                    else:
                        allowed = await self._permission_manager.check(node.name, "execute", check_input)
                finally:
                    reset_approval_context(token)
                    reset_approval_evidence(etoken)
                if (
                    not allowed
                    and self._hitl_pause_overrides.get(exec_id, self._hitl_pause)
                    and not node.config.continue_on_error
                ):
                    # issue #181: a local pending approval matching this run → pause at the node boundary for a human decision
                    allowed = await self._hitl_await(exec_id, workflow, node, check_input)
                    # issue #225: approval-edited params — merged into the gated node's input after approval wake (this execution only)
                    override = self._hitl_param_overrides.pop(exec_id, None)
                    if allowed and override:
                        node_input.update(override)
                        check_input.update(override)
                        logger.info(f"[{exec_id}] HITL: approval-edited params merged into node input: {sorted(override)}")
                    # issue #309: per-item decisions — batch_verdict merged into the gated node's input (partial completion visible)
                    batch = self._hitl_batch_verdicts.pop(exec_id, None)
                    if allowed and batch:
                        node_input["batch_verdict"] = batch
                        check_input["batch_verdict"] = batch
                        rejected = sum(1 for item in batch if not item.get("approved"))
                        logger.info(f"[{exec_id}] HITL: per-item decisions merged into node input: {len(batch)} items, {rejected} rejected")
                    # issue #508: structured human reply — human_reply merged into the gated node's input (passed downstream via gate output)
                    human_reply = self._hitl_replies.pop(exec_id, None)
                    if allowed and human_reply:
                        node_input["human_reply"] = human_reply
                        check_input["human_reply"] = human_reply
                        logger.info(f"[{exec_id}] HITL: structured human reply merged into node input: {sorted(human_reply)}")
                    # issue #554: kernel destructive grant — after a kernel-classified destructive node is approved,
                    # issue a single-redeem approval token (idempotent issue/redeem once/replay rejected)
                    if allowed and classify_destructive(node.name, check_input)[0]:
                        self._record_destructive_grant(exec_id, node)
            if not allowed:
                logger.warning(f"node '{node.name}' denied by the permission system")
                step = WorkflowStep(
                    node_id=node_id,
                    node_name=node.name,
                    node_display_name=node.config.label or node.display_name,
                    status=NodeStatus.DENIED,
                    started_at=time.time(),
                    input_data=check_input,
                    executor=executor.name,
                )
                step.completed_at = time.time()
                step.execution_time = 0
                step.error = "permission denied: manual approval required"
                logger.warning(
                    f"[{exec_id}] step end:   node={node.name} verdict=gated duration={step.execution_time:.3f}s "
                    f"executor={executor.name} params={_sanitize_params(node_input)} error={step.error}"
                )
                # P1-10 (audit 0912): purge the denied node's data from
                # the cache so downstream nodes cannot silently consume
                # unauthorized intermediate results.
                passed_data.pop(node_id, None)
                for _e in workflow.edges:
                    if _e.source_id == node_id:
                        passed_data.pop(_e.target_id, None)
                # P1-11 (audit 0912): emit NODE_DENIED + notification so
                # approvers actually learn about the pending action
                if self._event_emitter:
                    from .events import EventType

                    self._event_emitter.create_event(
                        EventType.NODE_DENIED,
                        execution_id=exec_id,
                        node_id=node_id,
                        node_name=node.name,
                        data={"error": step.error, "input_data": node_input},
                    )
                if self._hook_manager:
                    await self._hook_manager.fire(
                        HookEvent.NOTIFICATION,
                        {
                            "kind": "permission_denied",
                            "execution_id": exec_id,
                            "node_id": node_id,
                            "node_name": node.name,
                            "message": f"node '{node.name}' denied by the permission system, manual approval required",
                        },
                    )
                if not node.config.continue_on_error:
                    return [step], "fail", f"node '{node.name}' permission denied"
                return [step], "continue", ""

        # Hook: PRE_NODE_EXECUTE
        if self._hook_manager:
            hctx = await self._hook_manager.fire(
                HookEvent.PRE_NODE_EXECUTE,
                {
                    "execution_id": exec_id,
                    "node_id": node_id,
                    "node_name": node.name,
                    "input_data": node_input,
                },
            )
            if hctx and hctx.cancelled:
                logger.info(f"node '{node.name}' cancelled by hook")
                step = WorkflowStep(
                    node_id=node_id,
                    node_name=node.name,
                    node_display_name=node.config.label or node.display_name,
                    status=NodeStatus.CANCELLED,
                    started_at=time.time(),
                    input_data=node_input,
                    executor=executor.name,
                )
                step.completed_at = time.time()
                step.execution_time = 0
                step.error = "cancelled by hook"
                logger.warning(
                    f"[{exec_id}] step end:   node={node.name} verdict=blocked duration={step.execution_time:.3f}s "
                    f"executor={executor.name} params={_sanitize_params(node_input)} error={step.error}"
                )
                if not node.config.continue_on_error:
                    return [step], "cancel", ""
                return [step], "continue", ""
            if hctx and hctx.modified_data:
                node_input = hctx.modified_data.get("input_data", node_input)

        # Create step record
        step = WorkflowStep(
            node_id=node_id,
            node_name=node.name,
            node_display_name=node.config.label or node.display_name,
            status=NodeStatus.RUNNING,
            started_at=time.time(),
            input_data=node_input,
            executor=executor.name,
        )

        # issue #120: run-scoped step start — INFO
        logger.info(f"[{exec_id}] step start: node={node.name} node_id={node_id} executor={executor.name}")
        # issue #545: node_started telemetry — node type + run attribution
        if self._ops_telemetry is not None:
            await self._ops_telemetry.node_started(exec_id, node_id, node.name)
        # issue #554: per-node LLM JSON repair counter — reset before node execution, reported with telemetry after completion
        repair_token = json_repairs_in_step.set([0])

        # Execute node — CR-11: state is stored only in WorkflowStep, never written on the shared BaseNode
        # (concurrent execution of the same Workflow would overwrite node.status/result in place)
        try:
            # Event: NODE_START
            if self._event_emitter:
                from .events import EventType

                self._event_emitter.create_event(
                    EventType.NODE_START,
                    execution_id=exec_id,
                    node_id=node_id,
                    node_name=node.name,
                    data={"input_data": node_input},
                )
            # A-1: wire up NodeConfig.timeout/max_retries/retry_delay (previously dead config).
            # issue #257: nodes without explicit config get the engine default watchdog (FUSION_COWORK_NODE_TIMEOUT_S, default 1800s)
            timeout = float(node.config.timeout or 0.0)
            if timeout <= 0:
                timeout = self._node_timeout_default
            # issue #378: node-resolved watchdog floor — param-driven deadlines
            # (e.g. claude_dispatch dispatch timeout) must not lose to the engine
            # default watchdog; floor only ever raises the deadline
            try:
                floor = float(node.watchdog_floor())
            except Exception as fe:
                logger.warning(f"node '{node.name}' watchdog floor parse failed (ignored): {fe}")
                floor = 0.0
            if floor > timeout:
                logger.info(f"node '{node.name}' watchdog raised to {floor:g}s (node-resolved deadline)")
                timeout = floor
            max_retries = int(node.config.max_retries or 0)
            retry_delay = float(node.config.retry_delay or 0.0)
            # issue #182: business failures (FAILED result rather than exception) are not retried by default; retry only with explicit opt-in
            retry_business = bool(node.config.retry_on_business_failure)
            result = None
            last_err = None
            # issue #182: per-attempt error trail, written to step.attempt_errors for API observability after success
            attempt_errors: List[str] = []
            final_attempt = 1
            _err_class = ""
            # issue #571: MAST failure attribution (env off by default)
            attribution_on = failure_attribution_enabled()
            _failure_verdict: Optional[Dict[str, Any]] = None
            # issue #570: fusion_executor backend — Git-snapshot the workspace before the node executes,
            # roll back if the node finally fails (impl node failure recovery; only active when FUSION_SANDBOX_BACKEND=
            # fusion_executor and the backend is available; default path unchanged)
            backend_runner = None
            try:
                from .kernel_gates import get_sandbox_runner

                _br, _bb = get_sandbox_runner(cwd=self._run_workspace.get(exec_id, ""))
                if _bb == "fusion_executor" and _br is not None and hasattr(_br, "snapshot"):
                    backend_runner = _br
            except Exception as sbe:
                logger.warning(f"[{exec_id}] sandbox backend resolve failed (ignored): {sbe}")
            snapshot_id = ""
            if backend_runner is not None:
                try:
                    snapshot_id = await asyncio.to_thread(backend_runner.snapshot)
                    logger.info(f"[{exec_id}] node '{node.name}' pre-node workspace snapshot: {snapshot_id}")
                except Exception as se:
                    logger.warning(
                        f"[{exec_id}] node '{node.name}' workspace snapshot failed (continue without rollback): {se}"
                    )
            try:
                for attempt in range(max_retries + 1):
                    final_attempt = attempt + 1
                    ctx = NodeExecutionContext(
                        execution_id=exec_id,
                        workflow_id=workflow.id,
                        workflow_name=workflow.name,
                        node_id=node_id,
                        attempt=final_attempt,
                    )
                    # issue #257: heartbeat task — emits PROGRESS periodically during node execution, cancelled on completion
                    heartbeat_task: asyncio.Task | None = None
                    if self._event_emitter is not None and self._node_heartbeat_s > 0:
                        heartbeat_task = asyncio.create_task(self._node_heartbeat(exec_id, node_id, node.name))
                    try:
                        # issue #462: expose the engine context during node execution (used by sub_workflow recursive execution)
                        engine_token = _current_engine.set(self)
                        try:
                            if timeout > 0:
                                result = await asyncio.wait_for(
                                    executor.execute(node, node_input, ctx), timeout=timeout
                                )
                            else:
                                result = await executor.execute(node, node_input, ctx)
                        finally:
                            _current_engine.reset(engine_token)
                        if result.status == NodeStatus.FAILED:
                            err_txt = result.error or "business failure"
                            _err_class = "BusinessFailure"
                            attempt_errors.append(err_txt)
                            if retry_business and attempt < max_retries:
                                # issue #571: MAST attribution — spec-class failures do not burn the retry budget,
                                # failing directly to attribution escalation (env off by default = retries as usual)
                                if attribution_on and _failure_verdict is None:
                                    _failure_verdict = attribute_node_failure(
                                        exec_id, state.execution.steps, node_id, err_txt
                                    )
                                if _failure_verdict is not None and is_spec_failure(_failure_verdict):
                                    logger.warning(
                                        f"node '{node.name}' spec-class failure "
                                        f"(mode={_failure_verdict.get('mode')}) — skipping remaining retries, escalating to attribution"
                                    )
                                    break
                                logger.warning(
                                    f"node '{node.name}' business failure attempt={attempt + 1}/{max_retries + 1}: {err_txt}"
                                )
                                if retry_delay > 0:
                                    await asyncio.sleep(retry_delay)
                                continue
                            # business failure without opt-in / retries exhausted
                            break
                        # success
                        _err_class = ""
                        break
                    except TimeoutError as te:
                        _err_class = "TimeoutError"
                        last_err = f"node '{node.name}' timed out (timeout={timeout}s): {te}"
                        attempt_errors.append(last_err)
                        logger.warning(f"{last_err} attempt={attempt + 1}/{max_retries + 1}")
                        result = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as ne:
                        _err_class = type(ne).__name__
                        last_err = str(ne)
                        attempt_errors.append(f"exception: {last_err}")
                        logger.warning(f"node '{node.name}' exception attempt={attempt + 1}/{max_retries + 1}: {ne}")
                        result = None
                    finally:
                        if heartbeat_task is not None:
                            heartbeat_task.cancel()
                    if attempt < max_retries and retry_delay > 0:
                        await asyncio.sleep(retry_delay)
            finally:
                outbox_ctx.reset(_ob_token)
            if result is None:
                # retries exhausted without a successful result → build a failed NodeResult
                result = NodeResult(
                    status=NodeStatus.FAILED,
                    error=last_err or f"node '{node.name}' still failed after {max_retries} retries",
                )
            # exp/dynamic-orchestration: LLM-informed one-shot repair after retry
            # exhaustion (docs/pr-repair-step.md). Default OFF. One-shot: a second
            # failure after a repair-retry falls through to the normal failure path.
            if result.status == NodeStatus.FAILED and node.config.on_error_repair:
                decision = await self._llm_repair_decision(
                    exec_id, node, node_input, result.error or "", attempt_errors
                )
                action = decision.get("action", "abort")
                if action == "retry":
                    repaired_params = decision.get("params") or {}
                    if isinstance(repaired_params, dict) and repaired_params:
                        node_input = {**node_input, **repaired_params}
                        logger.info(
                            f"[{exec_id}] llm_repair: node={node.name} retrying with repaired params: {list(repaired_params.keys())}"
                        )
                    ctx2 = NodeExecutionContext(
                        execution_id=exec_id,
                        workflow_id=workflow.id,
                        workflow_name=workflow.name,
                        node_id=node_id,
                        attempt=final_attempt + 1,
                    )
                    try:
                        engine_token2 = _current_engine.set(self)
                        try:
                            if timeout > 0:
                                result = await asyncio.wait_for(
                                    executor.execute(node, node_input, ctx2), timeout=timeout
                                )
                            else:
                                result = await executor.execute(node, node_input, ctx2)
                        finally:
                            _current_engine.reset(engine_token2)
                        if result.status == NodeStatus.FAILED:
                            _err_class = "BusinessFailure"
                            attempt_errors.append(result.error or "repair retry business failure")
                            logger.warning(
                                f"[{exec_id}] llm_repair: node={node.name} repair retry failed: {result.error}"
                            )
                        else:
                            _err_class = ""
                            last_err = None
                            result.data = {
                                **(result.data or {}),
                                "_repair": {"action": "retry", "attempt": final_attempt + 1},
                            }
                            logger.info(f"[{exec_id}] llm_repair: node={node.name} repair retry succeeded")
                    except asyncio.CancelledError:
                        raise
                    except Exception as rp_err:
                        result = NodeResult(status=NodeStatus.FAILED, error=str(rp_err))
                        _err_class = type(rp_err).__name__
                        attempt_errors.append(f"repair retry exception: {rp_err}")
                        logger.warning(f"[{exec_id}] llm_repair: node={node.name} repair retry exception: {rp_err}")
                elif action == "skip":
                    logger.info(f"[{exec_id}] llm_repair: node={node.name} skip (error: {result.error})")
                    result = NodeResult(
                        status=NodeStatus.SKIPPED,
                        data=(result.data if isinstance(result.data, dict) else {}),
                        error=None,
                        summary="llm_repair: skip",
                    )
                    result.data["_repair"] = {"action": "skip"}
                    last_err = None
                # action == "abort" (or parse/LLM failure): fall through to the
                # normal failure path unchanged
            if result.status == NodeStatus.FAILED and backend_runner is not None:
                try:
                    _ok = await asyncio.to_thread(backend_runner.rollback, snapshot_id)
                    logger.warning(
                        f"[{exec_id}] node '{node.name}' failed — workspace rollback "
                        f"{'restored' if _ok else 'FAILED'} (fusion_executor backend)"
                    )
                except Exception as rbe:
                    logger.warning(f"[{exec_id}] node '{node.name}' workspace rollback error (ignored): {rbe}")
            # issue #571: final failure attribution — verdict lands on the node result + run summary (escalation routing input)
            if attribution_on and result.status == NodeStatus.FAILED:
                try:
                    if _failure_verdict is None:
                        _failure_verdict = attribute_node_failure(
                            exec_id, state.execution.steps, node_id, result.error or ""
                        )
                    if _failure_verdict is not None:
                        if not isinstance(result.data, dict):
                            result.data = {}
                        result.data["attribution"] = _failure_verdict
                        if state.execution.attribution is None:
                            state.execution.attribution = _failure_verdict
                        logger.info(
                            f"[{exec_id}] node '{node.name}' MAST attribution: "
                            f"mode={_failure_verdict.get('mode')} cluster={_failure_verdict.get('cluster')} "
                            f"confidence={_failure_verdict.get('confidence')}"
                        )
                except Exception as ae:
                    logger.warning(f"[{exec_id}] node '{node.name}' failure attribution error (ignored): {ae}")

            # issue #382: outbox journal — node result + declared effects committed in a single transaction;
            # a journaling failure does not affect the run (loud warning)
            if _outbox is not None:
                try:
                    _outbox.record_step(
                        exec_id,
                        node_id,
                        result.status.value,
                        result.summary or "",
                        node.collect_outbox_effects(),
                    )
                except Exception as oe:
                    logger.warning(f"node '{node.name}' outbox record_step failed (ignored): {oe}")

            # issue #182: attempt count and per-attempt errors written to the step, observable via the run steps API
            step.attempt = final_attempt
            step.attempt_errors = attempt_errors
            step.status = result.status
            step.completed_at = time.time()
            step.execution_time = step.completed_at - step.started_at
            step.output_data = result.data or {}
            step.tokens = _extract_tokens(step.output_data)
            step.error = result.error
            step.summary = result.summary
            # issue #554: repair counter collection — read the list count into the step, then restore the outer context
            step.json_repairs = json_repairs_in_step.get()[0]
            json_repairs_in_step.reset(repair_token)
            # issue #545: node_finished telemetry — duration/attempt count/stable failure signature
            # (error class + node type = mined_issues clustering key; no error text included)
            if self._ops_telemetry is not None:
                _sig = failure_signature(node.name, _err_class) if result.status == NodeStatus.FAILED else ""
                await self._ops_telemetry.node_finished(
                    exec_id,
                    node_id,
                    node.name,
                    result.status.value,
                    int(step.execution_time * 1000),
                    attempts=final_attempt,
                    failure_sig=_sig,
                    json_repairs=step.json_repairs,
                )

            # Hook: POST_NODE_EXECUTE
            if self._hook_manager:
                await self._hook_manager.fire(
                    HookEvent.POST_NODE_EXECUTE,
                    {
                        "execution_id": exec_id,
                        "node_id": node_id,
                        "node_name": node.name,
                        "result": result,
                    },
                )

            # Cache result
            node_results[node_id] = result
            passed_data[node_id] = result.data or {}

            # issue #470: record receipt on successful commit — on same run_id re-dispatch the node replays from the receipt (idempotent)
            if self._kernel_checkpoints is not None and result.status == NodeStatus.SUCCESS:
                try:
                    self._kernel_checkpoints.record_receipt(exec_id, node_id, result.data or {})
                except Exception as rc_err:
                    logger.warning(f"[{exec_id}] kernel receipt record failed (ignored): {rc_err}")

            # issue #480: verify_objective result lands in the approval journal (rubric + verdict auditable)
            if self._approval_journal is not None and node.name == "verify_objective":
                try:
                    action_id = f"verify:{exec_id}:{node_id}"
                    vdata = result.data or {}
                    self._approval_journal.record_pending(
                        action_id,
                        exec_id,
                        node_id=node_id,
                        node_name=node.name,
                        tool_name=node.name,
                        workflow=workflow.name,
                        params={
                            "objective": vdata.get("objective", ""),
                            "rubric": [r.get("item") for r in (vdata.get("rubric_results") or [])],
                            "strategy": vdata.get("strategy", ""),
                            "ratio": vdata.get("ratio"),
                            "min_ratio": vdata.get("min_ratio"),
                        },
                    )
                    self._approval_journal.resolve(
                        action_id,
                        approved=bool(vdata.get("passed", result.status == NodeStatus.SUCCESS)),
                        decided_by="engine",
                        comments=str(result.summary or ""),
                    )
                    logger.info(f"[{exec_id}] verify_objective result recorded in approval journal: action={action_id}")
                except Exception as vj_err:
                    logger.warning(f"[{exec_id}] verify_objective journal failed (ignored): {vj_err}")

            # Structured output validation (P1-9) — enforced when the node declares a schema
            if result.schema and result.status == NodeStatus.SUCCESS:
                try:
                    if not result.validate():
                        logger.warning(f"node '{node.name}' output failed schema validation")
                        result.error = f"output failed structured schema validation: {node.name}"
                        result.status = NodeStatus.FAILED
                        step.status = NodeStatus.FAILED
                        step.error = result.error
                        if not node.config.continue_on_error:
                            return [step], "fail", result.error
                except Exception as ve:
                    logger.error(f"node '{node.name}' schema validation exception: {ve}")
                    result.error = f"schema validation exception: {ve}"
                    result.status = NodeStatus.FAILED
                    step.status = NodeStatus.FAILED
                    step.error = result.error
                    if not node.config.continue_on_error:
                        return [step], "fail", result.error

            # Notify progress
            verdict = _node_verdict(result.status, result.data)
            if verdict == "success":
                logger.info(
                    f"[{exec_id}] step end:   node={node.name} verdict={verdict} "
                    f"duration={step.execution_time:.3f}s executor={executor.name} "
                    f"params={_sanitize_params(node_input)} error="
                )
            else:
                logger.warning(
                    f"[{exec_id}] step end:   node={node.name} verdict={verdict} "
                    f"duration={step.execution_time:.3f}s executor={executor.name} "
                    f"params={_sanitize_params(node_input)} error={result.error or ''}"
                )

            # Event: NODE_END
            if self._event_emitter:
                from .events import EventType

                self._event_emitter.create_event(
                    EventType.NODE_END,
                    execution_id=exec_id,
                    node_id=node_id,
                    node_name=node.name,
                    data={"status": result.status.value},
                )

            if result.status == NodeStatus.FAILED:
                if self._hook_manager:
                    await self._hook_manager.fire(
                        HookEvent.NODE_ERROR,
                        {
                            "execution_id": exec_id,
                            "node_id": node_id,
                            "node_name": node.name,
                            "error": result.error,
                        },
                    )
                if not node.config.continue_on_error:
                    return (
                        [step],
                        "fail",
                        f"node '{node.config.label or node.name}' execution failed: {result.error}",
                    )
                logger.warning(f"node '{node.config.label or node.name}' failed but continuing")

        except Exception as e:
            step.status = NodeStatus.FAILED
            step.completed_at = time.time()
            step.execution_time = step.completed_at - step.started_at
            step.error = str(e)
            logger.error(f"node '{node.config.label or node.name}' execution exception: {e}")
            logger.warning(
                f"[{exec_id}] step end:   node={node.name} verdict=failed duration={step.execution_time:.3f}s "
                f"executor={executor.name} params={_sanitize_params(node_input)} error={step.error}"
            )

            if self._hook_manager:
                await self._hook_manager.fire(
                    HookEvent.NODE_ERROR,
                    {
                        "execution_id": exec_id,
                        "node_id": node_id,
                        "node_name": node.name,
                        "error": str(e),
                    },
                )

            if not node.config.continue_on_error:
                return (
                    [step],
                    "fail",
                    f"node '{node.config.label or node.name}' exception: {e}",
                )

        if degraded_desc:
            # exp/degrade-semantics: tag the degraded execution so runs page and
            # downstream condition checks can distinguish partial-input runs
            step.output_data = {**(step.output_data or {}), "_degraded": degraded_desc}
            step.summary = f"{step.summary or ''} (degraded: {degraded_desc})".strip()
            logger.info(f"[{exec_id}] node '{node.name}' completed degraded: {degraded_desc}")
        return [step], "continue", ""

    def cancel(self, execution_id: str) -> bool:
        """Cancel the running workflow."""
        if execution_id in self._cancel_flags:
            self._cancel_flags[execution_id] = True
            # W8: paused executions must also be woken, otherwise cancel still blocks at the pause checkpoint
            evt = self._pause_events.get(execution_id)
            if evt is not None:
                evt.set()
            # issue #527: cancel bridges the kernel HumanGate — a waiting wait() returns immediately
            self._hitl_gate_cancel(execution_id)
            return True
        return False

    def pause(self, execution_id: str) -> bool:
        """W8: pause execution — suspends at the next node checkpoint after the current node completes."""
        evt = self._pause_events.get(execution_id)
        if evt is None:
            return False
        evt.clear()
        logger.info(f"engine pausing execution: {execution_id}")
        return True

    def resume(self, execution_id: str) -> bool:
        """W8: resume a paused execution."""
        evt = self._pause_events.get(execution_id)
        if evt is None or evt.is_set():
            return False
        evt.set()
        # issue #527: resume bridging — cancel the current kernel question; the park point re-asks and keeps waiting for a decision
        # (#445 semantics: resume does not synthesize a denial, only re-suspends)
        self._hitl_gate_cancel(execution_id)
        logger.info(f"engine resuming execution: {execution_id}")
        return True

    def is_paused(self, execution_id: str) -> bool:
        evt = self._pause_events.get(execution_id)
        return evt is not None and not evt.is_set()

    async def _node_heartbeat(self, exec_id: str, node_id: str, node_name: str) -> None:
        # issue #257: long-node heartbeat — periodic PROGRESS events, distinguishing "running" from "hung"
        from .events import EventType

        started = time.time()
        while True:
            await asyncio.sleep(self._node_heartbeat_s)
            if self._event_emitter is None:
                return
            self._event_emitter.create_event(
                EventType.PROGRESS,
                execution_id=exec_id,
                node_id=node_id,
                node_name=node_name,
                data={"heartbeat": True, "elapsed_s": round(time.time() - started, 1)},
            )

    def hitl_pending(self, execution_id: str) -> bool:
        # issue #181: whether this run is currently parked at a HITL approval wait point
        return execution_id in self._hitl_waiting

    def hitl_waiting_node(self, execution_id: str) -> Optional[str]:
        # issue #508: awaiting_human branch state — the id of the node waiting for a human decision
        return self._hitl_waiting_node.get(execution_id)

    def set_run_hitl_pause(self, execution_id: str, enabled: bool) -> None:
        # issue #260: per-run HITL override — True/False explicitly overrides the engine-global gate semantics
        self._hitl_pause_overrides[execution_id] = bool(enabled)

    def hitl_deadline(self, execution_id: str) -> Optional[float]:
        # issue #187: wait deadline (epoch ts) for frontend countdown; None when not waiting
        return self._hitl_deadlines.get(execution_id)

    def resolve_hitl(
        self,
        execution_id: str,
        approved: bool,
        once: bool = False,
        params: Optional[Dict[str, Any]] = None,
        attribution: Optional[Dict[str, Any]] = None,
        batch: Optional[List[Dict[str, Any]]] = None,
        comments: Optional[str] = None,
        reply: Optional[Dict[str, Any]] = None,
    ) -> bool:
        # issue #303: attribution — decision source {approval_id, decided_by, decided_at},
        # landing on the gated node's blocked step after wake so the timeline can answer "who approved this"
        # issue #309: batch — per-item decision items[] ({task_issue, approved, reason?});
        # all-approved = approved semantics unchanged; mixed = run continues + batch_verdict merged into the gated node's input,
        # consumed by downstream nodes/status writers (partial completion visible)
        # issue #508: reply — structured human reply (approval card option/webhook payload),
        # merged into the gated node's input as human_reply on approval, passed downstream via gate output
        # issue #181: approval routing callback — records the decision and wakes the paused run; cancel shares the same event
        # issue #187: once=True one-shot approval — no rule persisted; skips the permission re-check after wake and lets this node through
        # issue #225: params — overrides from the approval card's "edit params", merged into the gated node's input on approval
        # issue #463: decisions are journaled before waking — decisions for crashed runs (evt not yet rebuilt) also persist and replay after recovery
        journal = self._approval_journal
        if journal is not None:
            try:
                journal.resolve_run(
                    execution_id,
                    bool(approved),
                    decided_by=str((attribution or {}).get("decided_by") or "api"),
                    comments=str(comments or ""),
                )
            except Exception as e:
                logger.warning(f"HITL journal resolve failed (ignored): exec={execution_id} err={e}")
        # issue #527: approval side goes through the kernel resolve() — single winner: under concurrent/duplicate confirms only the first takes effect,
        # later ones get resolved=False (answered/cancelled/expired) and no longer overwrite the existing decision.
        # The full decision content is archived as answer; the cowork decision dict remains the routing source of truth
        qid = self._hitl_questions.get(execution_id)
        if qid and self._hitl_gate is not None:
            answer = {
                "approved": bool(approved),
                "once": bool(once),
                "params": params if isinstance(params, dict) else {},
                "batch": batch if isinstance(batch, list) else [],
                "reply": reply if isinstance(reply, dict) else {},
                "comments": str(comments or ""),
                "attribution": attribution if isinstance(attribution, dict) else {},
            }
            try:
                res = self._hitl_gate.resolve(qid, answer)
            except Exception as e:
                logger.warning(f"HITL kernel resolve failed (degrade open): exec={execution_id} err={e}")
                res = {"resolved": True}
            if not res.get("resolved"):
                logger.warning(
                    f"HITL resolve single-winner rejected: exec={execution_id} "
                    f"question={qid} reason={res.get('reason')}"
                )
                return False
        evt = self._pause_events.get(execution_id)
        if evt is None:
            return False
        self._hitl_decisions[execution_id] = bool(approved)
        if approved and once:
            self._hitl_once_grants.add(execution_id)
        if approved and isinstance(params, dict) and params:
            self._hitl_param_overrides[execution_id] = dict(params)
        if isinstance(attribution, dict) and attribution:
            self._hitl_attributions[execution_id] = dict(attribution)
        if isinstance(batch, list) and batch:
            self._hitl_batch_verdicts[execution_id] = [dict(x) for x in batch if isinstance(x, dict)]
        # issue #508: stash structured human reply — merged into the gated node's input after wake
        if isinstance(reply, dict) and reply and approved:
            self._hitl_replies[execution_id] = dict(reply)
        # issue #328: deny review comments — merged into the producing node's input on revision-loop rerun
        if not approved and isinstance(comments, str) and comments.strip():
            self._hitl_comments[execution_id] = comments.strip()
        self._hitl_deadlines.pop(execution_id, None)
        evt.set()
        logger.info(
            f"HITL approval decision: exec={execution_id} approved={approved} once={once} "
            f"params_override={'yes' if isinstance(params, dict) and params else 'no'}"
        )
        return True

    def _register_revision(self, exec_id: str, workflow: Workflow, node: BaseNode) -> None:
        # issue #328: revision routing for approval denials — when on_reject=revise, register a fallback request:
        # explicit revision_node > auto-pick the nearest upstream non-gate producing node (the gate reviews upstream output) > self if no upstream;
        # beyond max_revisions (default 3), treat as a denial terminal state to prevent infinite loops
        params = node.config.params or {}
        if str(params.get("on_reject") or "").strip().lower() != "revise":
            return
        target = str(params.get("revision_node") or "").strip()
        if target:
            if target not in workflow.nodes:
                logger.warning(f"[{exec_id}] revise: revision_node does not exist, treating as denial terminal: {target}")
                return
        else:
            # Auto routing: with an upstream producing node → return to the nearest upstream (the review subject is upstream output, valid for any gate shape);
            # no upstream → rerun self. When upstream exists but self must rerun, point an explicit revision_node at the own node id
            target = ""
            for edge in workflow.edges:
                if edge.target_id == node.id:
                    src = workflow.nodes.get(edge.source_id)
                    if src is not None and src.name != "approval_gate":
                        target = edge.source_id
                        break
            if not target:
                target = node.id
        counts = self._revision_counts.setdefault(exec_id, {})
        try:
            max_rev = max(1, int(params.get("max_revisions", 3) or 3))
        except (TypeError, ValueError):
            max_rev = 3
        if counts.get(target, 0) >= max_rev:
            logger.warning(f"[{exec_id}] revise: node '{target}' reached the revision cap ({max_rev}), treating as denial terminal")
            return
        counts[target] = counts.get(target, 0) + 1
        comments = self._hitl_comments.pop(exec_id, "")
        self._pending_revisions[exec_id] = {"target": target, "comments": comments, "gate_node": node.id}
        logger.info(
            f"[{exec_id}] revise: approval denied, returning to node '{target}' for revision "
            f"(round {counts[target]}/{max_rev}, review comments {len(comments)} chars)"
        )

    def _fire_hitl_email(
        self, exec_id: str, node: BaseNode, check_input: Dict[str, Any], phase: str, outcome: str
    ) -> None:
        # issue #404: best-effort email notification — the callback retries internally and never raises; this extra wrap guarantees zero run impact
        cb = self.hitl_notifier
        if cb is None:
            return
        try:
            cb(exec_id, node.name, check_input, phase, outcome)
        except Exception as e:
            logger.warning(f"[{exec_id}] HITL email notify failed (ignored): {e}")

    def _build_kernel_gate(self):
        # issue #558 (#125): wire cowork's kernel CheckpointStore as HumanGate
        # checkpoint_ref when kernel checkpoints are on — pending HITL questions
        # mirror as `awaiting_human` checkpoint entries and survive restarts.
        # Unwired binder = plain gate (JSON queue only), zero behavior change.
        if self._hitl_gate is not None:
            return self._hitl_gate
        gate_dir = os.environ.get("FUSION_COWORK_HITL_GATE_DIR", "").strip()
        if not gate_dir:
            gate_dir = os.path.join(os.path.expanduser("~"), ".fusion-cowork", "hitl_gate")
        checkpoint_ref = self._kernel_checkpoints.store if self._kernel_checkpoints is not None else None
        self._hitl_gate = HumanGate(gate_dir, checkpoint_ref=checkpoint_ref)
        return self._hitl_gate

    def _hitl_gate_reuse(self, exec_id: str, node: BaseNode) -> str:
        # issue #563 (#125 companion): restart re-delivery — after a SIGKILL the
        # kernel HumanGate queue keeps the pending question (session=run id) on
        # disk; when the run is resumed and re-parks at the same gated node,
        # REBIND that original question instead of asking a duplicate (two
        # pending questions for one approval = resolve single-winner confusion).
        # Strict match: same session + still pending + same node payload.
        if self._hitl_gate is None:
            return ""
        try:
            for q in self._hitl_gate.pending(session_id=exec_id):
                payload = q.payload if isinstance(q.payload, dict) else {}
                if str(payload.get("node_id") or "") == str(node.id):
                    self._hitl_questions[exec_id] = q.id
                    logger.info(f"[{exec_id}] HITL kernel question re-delivered id={q.id} node={node.id}")
                    return q.id
        except Exception as e:
            logger.warning(f"[{exec_id}] kernel HumanGate reuse check failed (ask fresh): {e}")
        return ""

    def hitl_recover(self, execution_id: str) -> Optional[Dict[str, Any]]:
        # issue #563: restart surface — a crashed/parked run's pending kernel
        # question survives on disk; report it (question + awaiting_human
        # checkpoint marker verification) so ops/UI can see the run is waiting
        # on a human even though this engine process holds no in-memory state.
        # Read-only: does not mutate run state or the gate queue.
        if self._hitl_gate is None:
            self._build_kernel_gate()
        gate = self._hitl_gate
        if gate is None:
            return None
        try:
            pending = gate.pending(session_id=execution_id)
        except Exception as e:
            logger.warning(f"[{execution_id}] HITL recover scan failed: {e}")
            return None
        if not pending:
            return None
        q = pending[0]
        payload = q.payload if isinstance(q.payload, dict) else {}
        marker_verified: Optional[bool] = None
        if self._kernel_checkpoints is not None:
            try:
                state = self._kernel_checkpoints.store.latest(execution_id)
                awaiting = state.metadata.get("awaiting_human") if state is not None else None
                marker_verified = bool(
                    state is not None and state.permission_mode == AWAITING_HUMAN_MARKER and awaiting
                )
            except Exception as e:
                logger.warning(f"[{execution_id}] HITL recover marker check failed: {e}")
                marker_verified = False
        return {
            "question_id": q.id,
            "node_id": str(payload.get("node_id") or ""),
            "tool_name": str(payload.get("tool_name") or ""),
            "policy": str(getattr(q, "policy", "")),
            "marker_verified": marker_verified,
        }

    def _hitl_gate_ask(self, exec_id: str, node: BaseNode, check_input: Dict[str, Any], ttl_s: float) -> str:
        # issue #527: enqueue into the kernel HumanGate at the park point — block policy + lazy expiry at ttl;
        # the payload goes through the same masking as the approval card (_sanitize_approval_params), the on-disk queue holds no plaintext sensitive values
        if self._hitl_gate is None:
            self._build_kernel_gate()
        if self._hitl_gate is None:
            return ""
        try:
            hq = self._hitl_gate.ask(
                session_id=exec_id,
                question=f"node '{node.name}' awaits human approval (run {exec_id})",
                payload={
                    "node_id": node.id,
                    "tool_name": node.name,
                    "input_data": _sanitize_approval_params(check_input),
                },
                policy="block",
                requester="workflow_engine",
                ttl_s=float(ttl_s) if ttl_s and float(ttl_s) > 0 else None,
            )
        except Exception as e:
            logger.warning(f"[{exec_id}] kernel HumanGate ask failed (degrade to local wait): {e}")
            return ""
        self._hitl_questions[exec_id] = hq.id
        logger.info(f"[{exec_id}] HITL kernel question queued id={hq.id} ttl={ttl_s:g}s")
        return hq.id

    def _hitl_gate_cancel(self, exec_id: str) -> None:
        # issue #527: cancel/terminal bridging — the kernel wait() returns None immediately; the park point re-asks or exits based on it
        qid = self._hitl_questions.pop(exec_id, None)
        if not qid or self._hitl_gate is None:
            return
        try:
            self._hitl_gate.cancel(qid)
        except Exception as e:
            logger.debug(f"[{exec_id}] kernel HumanGate cancel failed (ignored): {e}")

    def _ensure_kernel_gate(self):
        # issue #554: same lazy-build path as _hitl_gate_ask — token issuance reuses the shared HumanGate
        if self._hitl_gate is None:
            self._build_kernel_gate()
        return self._hitl_gate

    def _record_destructive_grant(self, exec_id: str, node: BaseNode) -> None:
        # issue #554: ApprovalGate contract — idempotent issuance (same call_id gets the same token), redeem once,
        # replays rejected; the token is recorded run-side for executor verification, issuance failure does not affect the approved execution
        try:
            gate = self._ensure_kernel_gate()
            if self._approval_gate is None:
                self._approval_gate = build_approval_gate(gate, self._permission_manager)
            call_id = f"{exec_id}:{node.name}"
            token = self._approval_gate.issue_token(call_id)
            self._approval_tokens[exec_id] = token
            logger.info(f"[{exec_id}] destructive grant token issued call={call_id} node={node.name}")
        except Exception as e:
            logger.warning(f"[{exec_id}] destructive grant token issuance failed (approval stands): {e}")

    async def _hitl_await(self, exec_id: str, workflow: Workflow, node: BaseNode, check_input: Dict[str, Any]) -> bool:
        # issue #463: durable HITL — after crash restart the gate re-enters and an existing journaled decision replays directly
        # (before the local pending-empty check: after restart the PermissionManager queue is empty, decisions can only come from the journal)
        journal = self._approval_journal
        if journal is not None:
            prior = journal.decision_for(exec_id, node.id)
            if prior is not None:
                # issue #507: replay consumes — the decision is valid only for this round of gate evaluation; the revision
                # loop (issue #328) must go through a fresh approval on its next evaluation of the same node
                journal.consume_run_node(exec_id, node.id)
                logger.info(
                    f"[{exec_id}] HITL durable replay: node {node.id} journaled decision "
                    f"approved={bool(prior.get('approved'))} (by {prior.get('decided_by') or '-'}), skipping wait"
                )
                return bool(prior.get("approved"))
        # issue #181: no local pending approval (e.g. guard online L3) → return False to the original deny-fast, no hang
        finder = getattr(self._permission_manager, "find_pending_by_execution", None)
        if not callable(finder) or not finder(exec_id):
            return False
        evt = self._pause_events.get(exec_id)
        if evt is None:
            return False
        # Notify the approver (reuse the NOTIFICATION hook); the pending item itself is already in the PermissionManager queue
        # issue #191: set _hitl_waiting before emitting the event — persistence inside event callbacks can then see hitl_pending
        self._hitl_waiting.add(exec_id)
        self._hitl_waiting_node[exec_id] = node.id
        # issue #508: node-level timeout policy — timeout_policy.timeout_s overrides the engine global default
        policy = node.config.params.get("timeout_policy") if isinstance(node.config.params, dict) else None
        policy_timeout_s = 0.0
        if isinstance(policy, dict):
            try:
                policy_timeout_s = max(0.0, float(policy.get("timeout_s") or 0))
            except (TypeError, ValueError):
                policy_timeout_s = 0.0
        self._hitl_deadlines[exec_id] = time.time() + (policy_timeout_s or self._hitl_timeout)
        # issue #527: park point goes through the shared kernel HumanGate — ask(policy="block", ttl_s) enqueues,
        # approval side resolve() single winner; wait wake still bridges the W8 pause event (shared by resume/cancel;
        # #445/#187 semantics unchanged: resume does not synthesize a decision, only rebuilds the question and keeps waiting)
        question_id = self._hitl_gate_reuse(exec_id, node) or self._hitl_gate_ask(
            exec_id, node, check_input, policy_timeout_s or self._hitl_timeout
        )
        # issue #463: journal the gate-entry pending — SQLite-persisted, the approval queue can be rebuilt after a crash
        if journal is not None:
            entry = finder(exec_id) or {}
            try:
                journal.record_pending(
                    action_id=str(entry.get("action_id") or ""),
                    run_id=exec_id,
                    node_id=node.id,
                    node_name=node.name,
                    tool_name=str(entry.get("tool_name") or node.name),
                    workflow=str(entry.get("workflow") or ""),
                    params=entry.get("params") if isinstance(entry.get("params"), dict) else {},
                )
            except Exception as e:
                logger.warning(f"[{exec_id}] HITL journal record_pending failed (ignored): {e}")
        # issue #260: write a blocked step at gate entry — the run timeline stays visible while waiting for approval,
        # no more "running + 0 steps" apparent hang; the node's terminal step is appended normally afterwards
        gate_execution = self._executions.get(exec_id)
        if gate_execution is not None:
            gate_step = WorkflowStep(
                node_id=node.id,
                node_name=node.name,
                node_display_name=node.config.label or node.display_name,
                status=NodeStatus.BLOCKED,
                started_at=time.time(),
                input_data=check_input,
                executor="simple",
            )
            gate_step.completed_at = time.time()
            gate_step.execution_time = 0
            gate_step.summary = "HITL: waiting for human approval"
            gate_execution.steps.append(gate_step)
        if self._event_emitter:
            from .events import EventType

            self._event_emitter.create_event(
                EventType.NODE_DENIED,
                execution_id=exec_id,
                node_id=node.id,
                node_name=node.name,
                data={"error": "HITL: waiting for human approval", "hitl": True, "input_data": check_input},
            )
        if self._hook_manager:
            await self._hook_manager.fire(
                HookEvent.NOTIFICATION,
                {
                    "kind": "hitl_pending",
                    "execution_id": exec_id,
                    "node_id": node.id,
                    "node_name": node.name,
                    "message": f"node '{node.name}' paused awaiting human approval (run={exec_id})",
                },
            )
        logger.info(f"[{exec_id}] HITL: node '{node.name}' paused at the boundary, waiting for human approval")
        self._fire_hitl_email(exec_id, node, check_input, "pending", "")
        timed_out = False
        while True:
            # issue #453: resolve_hitl may arrive early during the NOTIFICATION hook / email await above —
            # the event is already set and the decision is in _hitl_decisions. Check the decision before clear: only clear
            # the event into bounded wait when there is no decision; with a decision, break directly keeping the event set
            # so the between-node W8 checkpoint (pause_evt.wait) is not falsely suspended. No await between clear and check —
            # atomic within the single-threaded event loop, race window closed
            if exec_id in self._hitl_decisions or self._cancel_flags.get(exec_id):
                break
            evt.clear()
            try:
                # issue #187: bounded wait — auto-deny on timeout, never an infinite hang nor a silent pass
                remaining = self._hitl_deadlines.get(exec_id, time.time() + self._hitl_timeout) - time.time()
                if remaining <= 0:
                    timed_out = True
                    break
                if question_id:
                    # issue #527: kernel wait() is the park point — resolve wake returns the reply;
                    # None = resume/cancel bridged cancel or ttl expiry — check decision/cancel then re-ask and keep waiting
                    reply = None
                    try:
                        # issue #579: kernel wait goes through a cowork-side task poll instead of a direct await —
                        # under py3.11 the inner wait_for can swallow CancelledError in the "cancel/complete" race
                        # (an already-done inner future returns result), letting the task escape cancellation and keep
                        # running, leaving the teardown gather pending forever (reproduced on CI py3.11). Polling puts
                        # cancellation on the cowork-side sleep with clean semantics; the inner task is cancelled explicitly, no orphans.
                        gate_task = asyncio.ensure_future(self._hitl_gate.wait(question_id, timeout=remaining))
                        try:
                            while not gate_task.done():
                                await asyncio.sleep(0.05)
                            reply = gate_task.result()
                        except asyncio.CancelledError:
                            gate_task.cancel()
                            raise
                    except Exception as e:
                        logger.warning(f"[{exec_id}] kernel HumanGate wait failed (degrade to evt wait): {e}")
                        question_id = ""
                    if question_id and not isinstance(reply, dict):
                        if exec_id in self._hitl_decisions or self._cancel_flags.get(exec_id):
                            break
                        question_id = self._hitl_gate_ask(exec_id, node, check_input, remaining)
                        continue
                    if isinstance(reply, dict) and exec_id not in self._hitl_decisions:
                        # issue #527: decision resolved directly on the kernel side (bypassing resolve_hitl) — adopt the answer;
                        # if not adopted, wait returns the same reply immediately each round and the loop spins. Same #453 invariant:
                        # keep the pause event set before break so the between-node W8 checkpoint is not falsely suspended
                        self._hitl_decisions[exec_id] = bool(reply.get("approved"))
                        if reply.get("once"):
                            self._hitl_once_grants.add(exec_id)
                        evt.set()
                        logger.info(f"[{exec_id}] HITL kernel reply adopted: approved={bool(reply.get('approved'))}")
                if not question_id:
                    # issue #579: polling instead of wait_for — under py3.11, wait_for in the race between "outer cancel"
                    # and "event set" (inner future just done) can swallow CancelledError and return the result directly,
                    # letting _run_node escape cancellation and keep running with the teardown gather pending forever
                    # (reproduced on CI py3.11); sleep polling has clean cancellation semantics.
                    deadline_ts = time.time() + remaining
                    while not evt.is_set():
                        if time.time() >= deadline_ts:
                            raise TimeoutError()
                        await asyncio.sleep(0.05)
            except TimeoutError:
                timed_out = True
                break
            # issue #445: pause/resume and HITL wait share the same event — resumed with no approval decision
            # (and no cancel) = manual resume, re-suspend and keep waiting for a decision, never synthesize a denial
            if exec_id in self._hitl_decisions or self._cancel_flags.get(exec_id):
                break
            logger.info(
                f"[{exec_id}] HITL: resumed with no approval decision, waiting again "
                f"({max(self._hitl_deadlines.get(exec_id, 0) - time.time(), 0):.0f}s remaining)"
            )
        self._hitl_waiting.discard(exec_id)
        self._hitl_waiting_node.pop(exec_id, None)
        self._hitl_deadlines.pop(exec_id, None)
        # issue #527: park exit cleanup — the kernel question is dequeued as the wait ends (ttl-expired on timeout, cancel as fallback)
        self._hitl_gate_cancel(exec_id)
        # issue #508: node-level on_timeout — "deny" takes the generic denial path (revision loop registers as usual),
        # default/other values keep the fail-fast terminal state (branch fails directly, no revision entry)
        policy = node.config.params.get("timeout_policy") if isinstance(node.config.params, dict) else None
        timeout_deny = bool(isinstance(policy, dict) and str(policy.get("on_timeout") or "") == "deny")
        if timed_out:
            # Clear stranded pending approvals in the permission queue, preventing unbounded queue growth
            discard = getattr(self._permission_manager, "discard_pending_by_execution", None)
            if callable(discard):
                removed = discard(exec_id)
                if removed:
                    logger.info(f"[{exec_id}] HITL: timeout cleared {len(removed)} stranded pending approvals")
            # issue #463: timeout auto-denial is journaled too — replay semantics match live decisions
            # issue #507: timeout decisions are consumed immediately as well — a revision-loop re-ask must go through a fresh approval
            if journal is not None:
                try:
                    journal.resolve_run(exec_id, False, decided_by="timeout")
                except Exception as e:
                    logger.warning(f"[{exec_id}] HITL journal timeout resolve failed (ignored): {e}")
                try:
                    journal.consume_run_node(exec_id, node.id)
                except Exception as e:
                    logger.warning(f"[{exec_id}] HITL journal timeout consume failed (ignored): {e}")
            logger.warning(f"[{exec_id}] HITL: node '{node.name}' approval wait timed out ({self._hitl_timeout:g}s), auto-denied")
            self._fire_hitl_email(exec_id, node, check_input, "resolved", "timeout_auto_denied")
            if self._hook_manager:
                await self._hook_manager.fire(
                    HookEvent.NOTIFICATION,
                    {
                        "kind": "hitl_timeout",
                        "execution_id": exec_id,
                        "node_id": node.id,
                        "node_name": node.name,
                        "message": f"node '{node.name}' approval wait timed out, auto-denied (run={exec_id})",
                    },
                )
            if not timeout_deny:
                return False
            # issue #508: on_timeout="deny" — synthesize a denial decision into the generic denial handling below
            # (on_reject=revise revision loop registers as usual); the journal already recorded and consumed.
            # The HITL wait borrowed and cleared the W8 pause event; a fail-fast terminal breaks directly with no impact,
            # but a deny revision loop must continue the main loop — the event must be restored, otherwise it hangs at the W8 checkpoint
            evt.set()
            approved = False
            timed_out = False
        else:
            approved = self._hitl_decisions.pop(exec_id, None)
        # issue #507: consume on decision — resolve_hitl already journaled (resolve_run); the engine adopts the decision here;
        # not consuming would let the revision loop (issue #328) replay the old decision on its next gate evaluation, denying without approval
        if journal is not None:
            try:
                journal.consume_run_node(exec_id, node.id)
            except Exception as e:
                logger.warning(f"[{exec_id}] HITL journal consume failed (ignored): {e}")
        # issue #303: decision attribution lands on the gate's blocked step — the timeline/step drawer can answer "who approved this"
        attribution = self._hitl_attributions.pop(exec_id, None)
        if attribution:
            for s in reversed(gate_execution.steps if gate_execution else []):
                if s.node_id == node.id and s.status == NodeStatus.BLOCKED:
                    s.output_data = {**(s.output_data or {}), "hitl_attribution": dict(attribution)}
                    who = str(attribution.get("decided_by") or "unknown")
                    aid = str(attribution.get("approval_id") or "")
                    # i18n: user-facing
                    s.summary = f"HITL: {'批准' if approved else '拒绝'} — by {who}" + (
                        f" (approval {aid})" if aid else ""
                    )
                    logger.info(f"[{exec_id}] HITL decision attribution recorded: node={node.id} by={who} approval={aid}")
                    break
        if self._cancel_flags.get(exec_id):
            logger.info(f"[{exec_id}] HITL: cancelled while waiting")
            return False
        self._fire_hitl_email(exec_id, node, check_input, "resolved", "approved" if approved is True else "denied")
        if approved is not True:
            # issue #328: denial → revision-loop registration (on_reject=revise opt-in); the main loop falls back and reruns based on it
            self._register_revision(exec_id, workflow, node)
            logger.info(f"[{exec_id}] HITL: node '{node.name}' approval denied")
            return False
        # Approval → the rule was persisted by the confirm route; re-check permissions in place (only the gated node reruns)
        # issue #187: one-shot approval — no rule persisted, a re-check would certainly deny; let this node through directly
        if exec_id in self._hitl_once_grants:
            self._hitl_once_grants.discard(exec_id)
            logger.info(f"[{exec_id}] HITL: node '{node.name}' one-shot approval, letting through (no rule persisted)")
            return True
        # issue #442: since #340, gate-tool (approval_gate etc.) approvals do not persist an approve rule —
        # an in-place permission re-check would certainly deny, turning an explicit approval into a permission denial.
        # A gate approval is the human decision itself; let this node through; non-gate nodes still re-check (confirm_guard persisted the rule)
        if node.name in HITL_GATE_TOOLS:
            logger.info(f"[{exec_id}] HITL: gate '{node.name}' approved, letting through (gate persists no rule, re-check exempt)")
            return True
        token = set_approval_context({"workflow": "", "node_id": node.id, "execution_id": exec_id})
        try:
            allowed = await self._permission_manager.check(node.name, "execute", check_input)
        finally:
            reset_approval_context(token)
        if allowed:
            logger.info(f"[{exec_id}] HITL: node '{node.name}' approved, resuming in place")
        else:
            logger.warning(f"[{exec_id}] HITL: approved but permission re-check still denies: {node.name}")
        return allowed

    def add_directive(self, execution_id: str, directive: Dict[str, Any]) -> bool:
        """W8/B1: queue a run redirect directive, taking effect at the next node boundary.

        action: set_params (override params of an unexecuted node) / insert_node (insert a temporary node) /
        skip_remaining (skip remaining nodes) / inject (issue #227 mid-run instruction injection —
        the next node boundary writes instruction into the target node's input, the timeline records a "user correction" marker) /
        override_output (issue #422 verdict override — overwrite an executed node's output so downstream interpolation gets the new value).
        """
        action = str((directive or {}).get("action", ""))
        if action not in (
            "set_params",
            "skip_remaining",
            "insert_node",
            "inject",
            "override_output",
            "remove_node",
            "retarget_edge",
        ):
            logger.warning(f"unknown run redirect directive: action={action} exec={execution_id}")
            return False
        # issue #579: delivery mode — steer (default, wait for the next node boundary) / interrupt
        # (void in-flight node attempts; directives still apply in enqueue order at the boundary then rerun the interrupted nodes)
        mode = str((directive or {}).get("mode") or "steer")
        if mode not in ("steer", "interrupt"):
            logger.warning(f"run redirect directive invalid mode: mode={mode} exec={execution_id}")
            return False
        if action == "inject" and not str((directive or {}).get("instruction") or "").strip():
            # issue #227: early-reject empty instructions — do not queue an injection destined to be a no-op (visible as API 409)
            logger.warning(f"injection directive instruction is empty: exec={execution_id}")
            return False
        if action == "override_output" and not isinstance((directive or {}).get("merge"), dict):
            # issue #422: verdict override — merge must be a dict (early-reject, visible as API 409)
            logger.warning(f"override_output merge invalid: exec={execution_id}")
            return False
        if action in ("remove_node", "retarget_edge"):
            # issue #465: wave-boundary dynamic changes — synchronous structural validation, structured rejection for invalid requests
            # (in-wave target mid_wave / already executed already_executed / nonexistent unknown_target / missing fields bad_directive)
            if not self._validate_mutation_directive(execution_id, action, directive or {}):
                return False
        if execution_id not in self._pause_events:
            logger.warning(f"run redirect directive target does not exist: exec={execution_id}")
            return False
        payload = dict(directive)
        self._directives.setdefault(execution_id, []).append(payload)
        logger.info(f"run redirect directive queued: exec={execution_id} action={action} mode={mode}")
        if mode == "interrupt":
            self._interrupt_events.setdefault(execution_id, asyncio.Event()).set()
        if self._trajectory_recorder is not None:
            try:
                self._trajectory_recorder.record_directive(execution_id, payload)
            except Exception as e:
                logger.error(f"run redirect directive trajectory write failed: {e}")
        return True

    def _validate_mutation_directive(self, execution_id: str, action: str, directive: Dict[str, Any]) -> bool:
        # issue #465: remove_node / retarget_edge structural validation — only wave-boundary semantics allowed:
        # target must not be in an executing wave (mid_wave), must not be already executed (already_executed),
        # node/edge must exist (unknown_target). Invalid requests are structurally rejected, not queued.
        wf = self._run_workflows.get(execution_id)
        if wf is None:
            self._reject_directive(execution_id, "not_running", "run is not executing, cannot structurally validate")
            return False
        running_wave = self._wave_running.get(execution_id, set())
        execution = self._executions.get(execution_id)
        executed = {
            s.node_id
            for s in (execution.steps if execution else [])
            if s.status in (NodeStatus.SUCCESS, NodeStatus.FAILED, NodeStatus.SKIPPED)
        }

        def _node_ok(nid: str, label: str) -> bool:
            if nid not in wf.nodes:
                self._reject_directive(execution_id, "unknown_target", f"{label} node does not exist: {nid}")
                return False
            if nid in running_wave:
                # Defense 4: wave semantics — structural changes to in-wave nodes rejected (resend at the next wave boundary)
                self._reject_directive(execution_id, "mid_wave", f"{label} node is in an executing wave: {nid}")
                return False
            if nid in executed:
                self._reject_directive(execution_id, "already_executed", f"{label} node already executed: {nid}")
                return False
            return True

        if action == "remove_node":
            node_id = str(directive.get("node_id") or "")
            if not node_id:
                self._reject_directive(execution_id, "bad_directive", "remove_node missing node_id")
                return False
            return _node_ok(node_id, "removal target")
        # retarget_edge: repoint edge source→target to new_target (target side only)
        source_id = str(directive.get("source_id") or "")
        target_id = str(directive.get("target_id") or "")
        new_target_id = str(directive.get("new_target_id") or "")
        if not source_id or not target_id or not new_target_id:
            self._reject_directive(execution_id, "bad_directive", "retarget_edge requires source_id/target_id/new_target_id")
            return False
        if not any(e.source_id == source_id and e.target_id == target_id for e in wf.edges):
            self._reject_directive(execution_id, "unknown_target", f"edge does not exist: {source_id}→{target_id}")
            return False
        if not _node_ok(target_id, "original target"):
            return False
        if not _node_ok(new_target_id, "new target"):
            return False
        return True

    async def _wait_wave(
        self, exec_id: str, wave: List[str], tasks: List[asyncio.Task]
    ) -> Tuple[List[Any], bool, Set[str]]:
        # issue #579: wave wait races the interrupt event — on a hit with in-flight nodes,
        # cancel the in-flight tasks and void their attempts (returns interrupted=True + the voided node set;
        # the caller records audit markers and does not mark them done — they rerun next wave with directives applied);
        # if everything completed by the hit, treat as normal completion (event consumed in place, next wave unaffected).
        # The race uses 50ms polling rather than mixed futures — under py3.11's portal loop, mixing Event.wait()
        # futures hangs forever (CI-reproduced); polling behaves identically on all versions.
        evt = self._interrupt_events.get(exec_id)
        fired = False
        try:
            while True:
                # Re-fetch each round — add_directive(interrupt) may register the event while waiting
                cur = self._interrupt_events.get(exec_id) or evt
                if cur is not None and cur.is_set():
                    fired = True
                    evt = cur
                    break
                done, pending = await asyncio.wait(tasks, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                if not pending:
                    break
        except asyncio.CancelledError:
            # gather semantics: cancel all in-flight subtasks when the waiter is cancelled, no orphans
            for t in tasks:
                if not t.done():
                    t.cancel()
            raise
        if fired and all(t.done() for t in tasks):
            evt.clear()
            self._interrupt_events.pop(exec_id, None)
            fired = False
        if not fired:
            results = []
            try:
                for t in tasks:
                    results.append(await t)
            except asyncio.CancelledError:
                for t2 in tasks:
                    if not t2.done():
                        t2.cancel()
                raise
            return results, False, set()
        # interrupt hit — void in-flight attempts (completed ones keep their real results)
        evt.clear()
        self._interrupt_events.pop(exec_id, None)
        results = []
        voided: Set[str] = set()
        for nid, t in zip(wave, tasks):
            if not t.done():
                voided.add(nid)
                t.cancel()
            try:
                results.append(await t)
            except BaseException:
                # CancelledError / node raised — attempt voided, not recorded as a result step
                results.append(([], "continue", ""))
        return results, True, voided

    def _apply_directives(
        self,
        exec_id: str,
        workflow: Workflow,
        order: List[str],
        cursor: int,
        node_results: Dict[str, NodeResult],
        passed_data: Dict[str, Dict[str, Any]],
        execution: WorkflowExecution,
    ) -> int:
        """Apply queued redirect directives, returning the adjusted next pending index (order[cursor])."""
        directives = self._directives.pop(exec_id, [])
        for directive in directives:
            action = str(directive.get("action", ""))
            if action == "set_params":
                target = str(directive.get("node_id") or (order[cursor] if cursor < len(order) else ""))
                node = workflow.nodes.get(target)
                if node is None:
                    logger.warning(f"run redirect: target node does not exist: exec={exec_id} node={target}")
                    continue
                if target in node_results:
                    logger.warning(f"run redirect: node already executed, param override ignored: exec={exec_id} node={target}")
                    continue
                params = directive.get("params") or {}
                if not isinstance(params, dict) or not params:
                    logger.warning(f"run redirect: params empty or invalid: exec={exec_id} node={target}")
                    continue
                node.config.params.update(params)
                logger.info(f"run redirect: param override: exec={exec_id} node={target} keys={list(params)}")
            elif action == "insert_node":
                spec = directive.get("node") or {}
                name = str(spec.get("name") or "")
                node = NodeRegistry.create(
                    name,
                    config=NodeConfig(label=str(spec.get("label") or ""), params=dict(spec.get("params") or {})),
                )
                if node is None:
                    logger.warning(f"run redirect: node insertion failed, unknown type: exec={exec_id} name={name}")
                    continue
                new_id = workflow.add_node(node)
                source = str(directive.get("after_node_id") or "")
                pos = cursor
                if source and source in workflow.nodes:
                    workflow.connect(source, new_id)
                    if source in order:
                        pos = max(pos, order.index(source) + 1)
                order.insert(pos, new_id)
                if isinstance(directive.get("input"), dict):
                    passed_data[new_id] = dict(directive["input"])
                logger.info(f"run redirect: node inserted: exec={exec_id} name={name} id={new_id} pos={pos}")
            elif action == "skip_remaining":
                remaining = list(order[cursor:])
                for rid in remaining:
                    rnode = workflow.nodes.get(rid)
                    if rnode is None:
                        continue
                    skip_step = WorkflowStep(
                        node_id=rid,
                        node_name=rnode.name,
                        node_display_name=rnode.config.label or rnode.display_name,
                        status=NodeStatus.SKIPPED,
                        started_at=time.time(),
                        input_data=passed_data.get(rid, {}),
                    )
                    skip_step.completed_at = time.time()
                    skip_step.summary = "run redirect: skipping remaining nodes"
                    execution.steps.append(skip_step)
                    self._notify_progress(execution, skip_step)
                del order[cursor:]
                logger.info(f"run redirect: skipping remaining nodes: exec={exec_id} count={len(remaining)}")
            elif action == "inject":
                # issue #227 (D3): mid-run instruction injection — at the node boundary the user instruction is merged into
                # the next pending node's input (node_input updates passed_data[target]); the corrected step continues
                # with the new context; the timeline records a "user correction" marker step (zero duration, not a node execution).
                instruction = str(directive.get("instruction") or "").strip()
                if not instruction:
                    logger.warning(f"run redirect: injection instruction is empty: exec={exec_id}")
                    continue
                target = str(directive.get("node_id") or (order[cursor] if cursor < len(order) else ""))
                tnode = workflow.nodes.get(target)
                if tnode is None:
                    logger.warning(f"run redirect: injection target node does not exist: exec={exec_id} node={target}")
                    continue
                if target in node_results:
                    logger.warning(f"run redirect: injection target already executed, directive ignored: exec={exec_id} node={target}")
                    continue
                bucket = passed_data.setdefault(target, {})
                bucket["user_instruction"] = instruction
                if isinstance(directive.get("merge"), dict) and directive["merge"]:
                    bucket.update(directive["merge"])
                marker = WorkflowStep(
                    node_id=target,
                    node_name=tnode.name,
                    node_display_name=tnode.config.label or tnode.display_name,
                    status=NodeStatus.SKIPPED,
                    started_at=time.time(),
                    # i18n: user-facing
                    summary=f"用户修正: {instruction[:200]}",
                    user_correction=True,
                )
                marker.completed_at = time.time()
                execution.steps.append(marker)
                self._notify_progress(execution, marker)
                logger.info(f"[{exec_id}] run redirect: instruction injected: node={target} instruction={instruction[:80]!r}")
            elif action == "override_output":
                # issue #422: verdict override — overwrite an executed node's output data (passed_data[target]),
                # so downstream ${step.<id>.<path>} interpolation picks up the new value; the timeline records a "user correction" marker step.
                # Only already-executed nodes may be overridden (recorded in passed_data); for unexecuted nodes use set_params.
                target = str(directive.get("node_id") or "")
                merge = directive.get("merge") or {}
                tnode = workflow.nodes.get(target)
                if tnode is None or target not in passed_data:
                    logger.warning(f"run redirect: override_output target missing or not executed: exec={exec_id} node={target}")
                    continue
                passed_data[target].update(merge)
                marker = WorkflowStep(
                    node_id=target,
                    node_name=tnode.name,
                    node_display_name=tnode.config.label or tnode.display_name,
                    status=NodeStatus.SKIPPED,
                    started_at=time.time(),
                    # i18n: user-facing
                    summary=f"用户修正: 覆盖输出 keys={sorted(merge.keys())}",
                    user_correction=True,
                )
                marker.completed_at = time.time()
                execution.steps.append(marker)
                self._notify_progress(execution, marker)
                logger.info(f"[{exec_id}] run redirect: output overridden: node={target} keys={sorted(merge.keys())}")
            elif action == "remove_node":
                # issue #465: wave-boundary removal of an unexecuted node — removed together with its related edges
                node_id = str(directive.get("node_id") or "")
                if node_id in node_results or node_id in self._wave_running.get(exec_id, set()):
                    self._reject_directive(exec_id, "already_executed", f"removal target already executed/executing: {node_id}")
                    continue
                node = workflow.nodes.get(node_id)
                if node is None:
                    self._reject_directive(exec_id, "unknown_target", f"removal target does not exist: {node_id}")
                    continue
                workflow.nodes.pop(node_id, None)
                workflow.edges = [e for e in workflow.edges if e.source_id != node_id and e.target_id != node_id]
                if node_id in order:
                    idx = order.index(node_id)
                    order.pop(idx)
                    if idx < cursor:
                        cursor -= 1
                logger.info(f"[{exec_id}] run redirect: node removed: node={node_id} name={node.name}")
            elif action == "retarget_edge":
                # issue #465: wave-boundary edge retarget — repoint source→target to new_target (downstream readiness judged live from workflow.edges)
                source_id = str(directive.get("source_id") or "")
                target_id = str(directive.get("target_id") or "")
                new_target_id = str(directive.get("new_target_id") or "")
                if target_id in node_results or target_id in self._wave_running.get(exec_id, set()):
                    self._reject_directive(exec_id, "already_executed", f"original target already executed/executing: {target_id}")
                    continue
                if new_target_id not in workflow.nodes or new_target_id in node_results:
                    self._reject_directive(exec_id, "unknown_target", f"new target missing or already executed: {new_target_id}")
                    continue
                edge = next((e for e in workflow.edges if e.source_id == source_id and e.target_id == target_id), None)
                if edge is None:
                    self._reject_directive(exec_id, "unknown_target", f"edge does not exist: {source_id}→{target_id}")
                    continue
                edge.target_id = new_target_id
                logger.info(f"[{exec_id}] run redirect: edge retargeted: {source_id}: {target_id} → {new_target_id}")
        return cursor

    def get_execution(self, execution_id: str) -> Optional[WorkflowExecution]:
        """Get an execution record."""
        return self._executions.get(execution_id)

    def list_executions(self, limit: int = 20) -> List[WorkflowExecution]:
        """List recent execution records."""
        sorted_execs = sorted(
            self._executions.values(),
            key=lambda e: e.started_at,
            reverse=True,
        )
        return sorted_execs[:limit]

    def clear_executions(self) -> None:
        """Clear execution records."""
        self._executions.clear()

    def _trim_executions(self) -> None:
        """R-1: evict the oldest terminal entries when _executions exceeds the cap, keeping running ones.

        Prefers the oldest started_at among terminal (completed/failed/cancelled) entries; if all are running, drops the oldest by the cap.
        """
        if len(self._executions) <= self._max_executions:
            return
        terminal = [e for e in self._executions.values() if e.status != WorkflowStatus.RUNNING]
        if terminal:
            terminal.sort(key=lambda e: e.started_at)
            victim = terminal[0]
            self._executions.pop(victim.id, None)
            self._cancel_flags.pop(victim.id, None)
            logger.debug(f"_executions evicted terminal entry: {victim.id} (status={victim.status})")
        else:
            oldest = min(self._executions.values(), key=lambda e: e.started_at)
            self._executions.pop(oldest.id, None)
            self._cancel_flags.pop(oldest.id, None)
            logger.debug(f"_executions all running, evicted oldest: {oldest.id}")
