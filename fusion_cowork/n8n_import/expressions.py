"""n8n expression translation sublayer (issue #395 — the cost center).

n8n embeds JS expressions in parameter strings via {{ ... }}. Cowork data
passing uses ${step.<id>.<path>} / ${param.<name>} refs resolved by the
engine (issue #327). This module translates the supported expression subset
and REFUSES anything outside it — an untranslatable expression must surface
in the coverage report, never silently pass through as a literal.

Supported subset (documented contract, fixture-tested):
- {{ $json.a.b }} / {{ $json["a"]["b"] }}          -> ${step.<upstream>.a.b}
- {{ $input.item.json.a }}                          -> same as $json.a
- {{ $node["Name"].json.a }} / {{ $node.Name.json["a"] }}
                                                    -> ${step.<name-slug>.a}
- literal text around expressions is preserved (concatenation)

Anything else ($now, $if, ternaries, JS function calls, $item(0)...):
UnsupportedExpression naming the expression.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger("fusion_cowork.n8n_import")

_EXPR_RE = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)

_SEGMENT_QUOTED = re.compile(r"""^\[\s*['"]([^'"]+)['"]\s*\]""")
_SEGMENT_ATTR = re.compile(r"""^\.([A-Za-z_][A-Za-z0-9_]*)""")
_SEGMENT_BRACKET_NUM = re.compile(r"""^\[\s*(\d+)\s*\]""")

# prefixes that mean "the incoming item's json" in n8n
_JSON_ROOTS = ("$json", "$input.item.json", "$input.first.json", "$binary")


class UnsupportedExpression(Exception):
    def __init__(self, expr: str, reason: str):
        self.expr = expr.strip()
        self.reason = reason
        super().__init__(f"unsupported n8n expression '{{{{ {self.expr} }}}}': {reason}")


def slug_node_id(name: str) -> str:
    # n8n node display name -> cowork node id (lowercase, [a-z0-9_])
    slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return slug or "node"


def _parse_path(expr: str) -> (str, List[str]):
    """Split '<root>.a.b["c"]' into (root, [a, b, c]). Raises on junk."""
    expr = expr.strip()
    root = None
    for cand in sorted(_JSON_ROOTS, key=len, reverse=True):
        if expr == cand or expr.startswith(cand + ".") or expr.startswith(cand + "["):
            root = cand
            break
    if root is None:
        node_m = re.match(r"""^\$node\s*\[\s*['"]([^'"]+)['"]\s*\]|\$node\.([A-Za-z_][A-Za-z0-9_]*)""", expr)
        if not node_m:
            raise UnsupportedExpression(expr, "only $json / $input.item.json / $node['Name'] roots are supported")
        root = f"$node:{node_m.group(1) or node_m.group(2)}"
        expr = expr[node_m.end() :]
    segments: List[str] = []
    rest = expr[len(root) :] if not root.startswith("$node:") else expr
    while rest:
        m = _SEGMENT_ATTR.match(rest)
        if m:
            segments.append(m.group(1))
            rest = rest[m.end() :]
            continue
        m = _SEGMENT_QUOTED.match(rest)
        if m:
            segments.append(m.group(1))
            rest = rest[m.end() :]
            continue
        m = _SEGMENT_BRACKET_NUM.match(rest)
        if m:
            segments.append(m.group(1))
            rest = rest[m.end() :]
            continue
        raise UnsupportedExpression(
            expr if not root.startswith("$node:") else root[6:], f"cannot parse path segment at '{rest}'"
        )
    return root, segments


def translate_expression_value(value: str, upstream_id: str, node_ids: Optional[Dict[str, str]] = None) -> str:
    """Translate one n8n parameter string to a cowork template string.

    upstream_id: cowork node id of the node feeding $json (main input chain).
    node_ids: n8n node name -> cowork node id map (for $node[...] roots).
    Raises UnsupportedExpression for anything outside the subset.
    """
    node_ids = node_ids or {}

    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        root, segments = _parse_path(expr)
        if root.startswith("$node:"):
            name = root[6:]
            target = node_ids.get(name)
            if not target:
                raise UnsupportedExpression(expr, f"referenced node '{name}' not found in workflow")
            # n8n $node["X"].json.a — path already includes the .json hop
            base = f"${{step.{target}"
        else:
            if not upstream_id:
                raise UnsupportedExpression(expr, "$json has no upstream node (trigger output is not addressable)")
            if not segments:
                raise UnsupportedExpression(expr, "empty field path")
            # n8n $json.a IS the item json; cowork exposes an upstream HTTP
            # response body under the conventional "json" output key — insert it
            segments = ["json"] + segments
            base = f"${{step.{upstream_id}"
        if not segments:
            raise UnsupportedExpression(expr, "empty field path")
        return base + "." + ".".join(segments) + "}"

    out = _EXPR_RE.sub(repl, value)
    return out


def translate_params(params: Dict, upstream_id: str, node_ids: Optional[Dict[str, str]] = None) -> (Dict, List[str]):
    """Deep-translate a parameter dict. Returns (translated, unsupported_exprs)."""
    unsupported: List[str] = []
    node_ids = node_ids or {}

    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and "{{" in v:
            try:
                return translate_expression_value(v, upstream_id, node_ids)
            except UnsupportedExpression as e:
                if e.expr not in unsupported:
                    unsupported.append(e.expr)
                logger.warning("n8n import: %s", e)
                return v  # kept as literal — caller must treat node as warning/unsupported
        return v

    return walk(params), unsupported
