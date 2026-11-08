"""n8n export analyzer + coverage report (issue #395 acceptance core #1).

Classifies every node in an n8n export before import:
- mappable              -> imports as a runnable cowork node
- mappable_with_warning -> imports, but behavior differs (warning listed)
- unsupported           -> NEVER imported; listed with a suggested replacement

The whole-flow import proceeds only after the user sees the report.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .expressions import UnsupportedExpression, slug_node_id

logger = logging.getLogger("fusion_cowork.n8n_import")

# n8n node type -> (verdict, cowork node name, warning/suggestion)
# verdicts: mappable | mappable_with_warning | unsupported
NODE_MAPPINGS: Dict[str, Tuple[str, str, str]] = {
    "n8n-nodes-base.manualTrigger": ("mappable", "", "manual trigger becomes the workflow start"),
    "n8n-nodes-base.scheduleTrigger": (
        "mappable_with_warning",
        "",
        "cron schedule is preserved in metadata; wire it to fusion-cowork TaskScheduler separately",
    ),
    "n8n-nodes-base.webhook": (
        "mappable_with_warning",
        "",
        "webhook ingestion is not wired in v1 — the workflow runs via manual/scheduled start",
    ),
    "n8n-nodes-base.httpRequest": ("mappable", "http_request", ""),
    "n8n-nodes-base.set": ("mappable", "python_repl", ""),
    "n8n-nodes-base.if": (
        "mappable_with_warning",
        "python_repl",
        "cowork executes topologically serial — IF maps to a condition-evaluating python_repl step, not branching; "
        "expression operands translate to ${step...} refs (false-branch edges are dropped)",
    ),
    "n8n-nodes-base.merge": ("mappable", "merge", ""),
    "n8n-nodes-base.switch": (
        "unsupported",
        "",
        "multi-branch routing has no cowork equivalent — split into separate workflows or use if + filter",
    ),
    "n8n-nodes-base.wait": (
        "unsupported",
        "",
        "timed waits are not portable — use a scheduled workflow or upstream retry/backoff",
    ),
    "n8n-nodes-base.code": (
        "unsupported",
        "",
        "arbitrary JS has no safe v1 mapping — rewrite as a Set node or python_repl in n8n before export",
    ),
    "n8n-nodes-base.function": (
        "unsupported",
        "",
        "arbitrary JS has no safe v1 mapping — rewrite as a Set node or python_repl in n8n before export",
    ),
    "n8n-nodes-base.stickyNote": (
        "mappable",
        "",
        "canvas documentation only — no execution node is emitted",
    ),
    "n8n-nodes-base.splitInBatches": (
        "unsupported",
        "",
        "batch-loop branching has no serial v1 equivalent — flatten the loop into a single pass or split workflows",
    ),
    "n8n-nodes-base.aggregate": (
        "unsupported",
        "",
        "item aggregation has no v1 mapping — collect upstream items in a python_repl step before export",
    ),
    "n8n-nodes-base.convertToFile": (
        "unsupported",
        "",
        "binary file conversion is not supported in v1 — produce the file payload in a python_repl step",
    ),
    "n8n-nodes-base.n8n": (
        "unsupported",
        "",
        "sub-workflow execution is not wired in v1 — inline the sub-workflow's nodes before export",
    ),
    "@n8n/n8n-nodes-langchain.vectorStoreQdrant": (
        "unsupported",
        "",
        "vector-store nodes are not supported in v1 — use the fusion-rag knowledge layer instead",
    ),
    "@n8n/n8n-nodes-langchain.embeddingsOllama": (
        "unsupported",
        "",
        "embedding nodes are not supported in v1 — use the fusion-rag knowledge layer instead",
    ),
}

# matches both "n8n-nodes-langchain.*" and the "@n8n/n8n-nodes-langchain.*" package form
_LLM_TYPE_RE = re.compile(r"^(?:@n8n/)?n8n-nodes-langchain\.(openAi|lmChatOpenAi|agent|chainLlm|llm)")


def _classify_type(node_type: str) -> Tuple[str, str, str]:
    if node_type in NODE_MAPPINGS:
        verdict, target, note = NODE_MAPPINGS[node_type]
        return verdict, target, note
    if _LLM_TYPE_RE.match(node_type):
        return (
            "mappable_with_warning",
            "http_request",
            "LLM node maps to http_request POST {base_url}/chat/completions — model/credentials must be pinned manually",
        )
    return ("unsupported", "", "no v1 mapping — declare it here or extend NODE_MAPPINGS with a reviewed entry")


@dataclass
class N8nNodeVerdict:
    name: str
    type: str
    verdict: str
    target: str = ""
    note: str = ""
    expression_issues: List[str] = field(default_factory=list)


@dataclass
class N8nImportReport:
    workflow_name: str
    verdicts: List[N8nNodeVerdict] = field(default_factory=list)
    cron: Optional[str] = None

    @property
    def runnable(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict != "unsupported")

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def unsupported(self) -> List[N8nNodeVerdict]:
        return [v for v in self.verdicts if v.verdict == "unsupported"]

    @property
    def warnings(self) -> List[N8nNodeVerdict]:
        return [v for v in self.verdicts if v.verdict == "mappable_with_warning" or v.expression_issues]

    def report_text(self) -> str:
        lines = [f"n8n import coverage: {self.runnable}/{self.total} nodes runnable (workflow '{self.workflow_name}')"]
        if self.warnings:
            lines.append("warnings:")
            for v in self.warnings:
                note = v.note or "expression issues"
                lines.append(f"  - {v.name} ({v.type}): {note}")
                for e in v.expression_issues:
                    lines.append(f"      expression: {{{{ {e} }}}}")
        if self.unsupported:
            lines.append(f"unsupported ({len(self.unsupported)}) — never silently dropped:")
            for v in self.unsupported:
                lines.append(f"  - {v.name} ({v.type}): {v.note}")
        if self.cron:
            lines.append(f"schedule: cron '{self.cron}' (metadata only in v1)")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "runnable": self.runnable,
            "total": self.total,
            "cron": self.cron,
            "nodes": [
                {
                    "name": v.name,
                    "type": v.type,
                    "verdict": v.verdict,
                    "target": v.target,
                    "note": v.note,
                    "expression_issues": v.expression_issues,
                }
                for v in self.verdicts
            ],
        }


def _parse_cron(node: Dict[str, Any]) -> Optional[str]:
    params = node.get("parameters") or {}
    rule = params.get("rule") or {}
    cron = rule.get("cron") if isinstance(rule, dict) else None
    return str(cron).strip() or None


def analyze_n8n_workflow(data: Dict[str, Any]) -> N8nImportReport:
    """Parse an n8n export JSON dict and classify every node."""
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("n8n export JSON missing 'nodes' list")
    report = N8nImportReport(workflow_name=str(data.get("name", "untitled")))
    # full name->slug map so cross-node $node["Other"] refs in the expression
    # probe resolve against the real id space (no false-positive warnings)
    probe_ids = {str(n.get("name", "")): slug_node_id(str(n.get("name", ""))) for n in nodes if isinstance(n, dict)}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type", ""))
        verdict, target, note = _classify_type(ntype)
        v = N8nNodeVerdict(name=str(node.get("name", "")), type=ntype, verdict=verdict, target=target, note=note)
        if "scheduleTrigger" in ntype:
            report.cron = _parse_cron(node) or report.cron
        # expression scan: any {{ }} that cannot translate marks the node
        for key, val in (node.get("parameters") or {}).items():
            if isinstance(val, str) and "{{" in val:
                from .expressions import translate_expression_value

                try:
                    translate_expression_value(val, "probe_upstream", probe_ids)
                except UnsupportedExpression as e:
                    v.expression_issues.append(e.expr)
        report.verdicts.append(v)
    logger.info("n8n analyze '%s': %d/%d runnable", report.workflow_name, report.runnable, report.total)
    return report


def load_n8n_export(path: str) -> Dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("n8n export must be a JSON object")
    return data
