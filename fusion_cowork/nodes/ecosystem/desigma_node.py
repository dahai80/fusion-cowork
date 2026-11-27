"""issue #431: design_context node — export customer design docs via desigma-mcp (stdio) for coder redline implementation.

With zero design docs (empty workspace) it no-ops successfully — pipeline behavior
is identical to the legacy version; with design docs it exports the document
structure plus code exports for the specified frame, and design_section is
injected into the coder prompt as a full section. Child process spawn is gated
by the FUSION_COWORK_MCP_STDIO_ALLOW allowlist (mcp_stdio module); it is NOT in
HIGH_RISK_NODES — the no-op path never spawns, and an empty default allowlist
rejects all commands (fail-closed). Listing it as high-risk would instead make
runs without design docs pause on HITL, breaking the "zero-design-doc behavior
unchanged" redline.
"""

import json
import logging
from typing import Any, Dict, List

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

_REDLINE_TEXT = (
    "Implement the screens EXACTLY from this exported design (structure/tokens are authoritative). "
    "Deviate only where fd-design-lint flags a violation."
)


@register_node
class DesignContextNode(BaseNode):
    name = "design_context"
    # i18n: user-facing
    display_name = "设计上下文导出"
    category = NodeCategory.FUSION_ECOSYSTEM
    # i18n: user-facing
    description = "经 desigma-mcp (stdio) 导出客户设计稿; 无设计稿时空转 (issue #431)"
    icon = "🎨"
    # i18n: user-facing
    default_label = "设计上下文"

    inputs = [
        # i18n: user-facing
        {"key": "workspace", "label": "设计稿工作区路径", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "design_section", "label": "设计契约段落 (注入 coder prompt; 空串 = 无设计稿)", "type": "string"},
        # i18n: user-facing
        {"key": "design_present", "label": "是否携带设计稿", "type": "boolean"},
        # i18n: user-facing
        {"key": "exports", "label": "各语言代码导出 {language: code}", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "设计稿工作区路径 (.desigma 目录); 空 = 无设计稿, 空转成功",
                    "default": "",
                },
                "command": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "MCP stdio 服务器命令 (basename 须在 FUSION_COWORK_MCP_STDIO_ALLOW 白名单)",
                    "default": "desigma-mcp",
                },
                "args": {
                    "type": "array",
                    # i18n: user-facing
                    "description": "MCP 服务器命令行参数 (缺省 --workspace <workspace>)",
                    "default": [],
                },
                "frame_id": {
                    "type": "number",
                    # i18n: user-facing
                    "description": "导出的 frame 节点 id; 0 = 取 list_nodes 首个节点",
                    "default": 0,
                },
                "languages": {
                    "type": "array",
                    # i18n: user-facing
                    "description": "导出语言 (desigma generate_code 枚举子集)",
                    "default": ["react", "tailwind"],
                },
                # i18n: user-facing
                "timeout": {"type": "number", "description": "单请求超时 (秒)", "default": 60},
            },
        }

    def _spawn(self, command: str, args: List[Any], calls: List[Dict[str, Any]], timeout: float):
        # test seam: monkeypatch injects a fake stdio session
        from fusion_cowork.nodes.ecosystem.mcp_stdio import stdio_tool_calls

        return stdio_tool_calls(command, args, calls, timeout=timeout)

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        workspace = str(inputs.get("workspace") or params.get("workspace") or "").strip()
        if not workspace:
            logger.info("design_context: no design doc (empty workspace) — no-op, pipeline behavior unchanged")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"design_section": "", "design_present": False, "exports": {}},
                summary="no design doc — skipping export",
            )

        command = str(params.get("command") or "desigma-mcp").strip()
        from fusion_cowork.nodes.ecosystem.mcp_stdio import STDIO_ALLOW_ENV, stdio_allowed, stdio_allowlist

        if not stdio_allowed(command):
            allowed = ", ".join(stdio_allowlist()) or "(empty — reject all)"
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"command {command} is not in the {STDIO_ALLOW_ENV} allowlist (current: {allowed})",
                summary="stdio command not authorized",
            )

        args = list(params.get("args") or [])
        if not args:
            args = ["--workspace", workspace]
        timeout = float(params.get("timeout", 60) or 60)
        languages = [str(lang) for lang in (params.get("languages") or ["react", "tailwind"])]
        frame_id = int(params.get("frame_id") or 0)

        try:
            # phase 1: document structure + node list (pick the first node from the list when frame_id=0)
            phase1 = await self._spawn(
                command,
                args,
                [
                    {"tool": "desigma_get_document", "arguments": {"session": "file"}},
                    {"tool": "desigma_list_nodes", "arguments": {"limit": 50}},
                ],
                timeout,
            )
            doc_text = phase1[0]["text"] if len(phase1) > 0 else ""
            list_text = phase1[1]["text"] if len(phase1) > 1 else ""

            exports: Dict[str, str] = {}
            if frame_id <= 0:
                frame_id = self._first_frame_id(list_text)
            if frame_id > 0:
                code_calls = [
                    {"tool": "desigma_generate_code", "arguments": {"node_id": frame_id, "language": lang}}
                    for lang in languages
                ]
                phase2 = await self._spawn(command, args, code_calls, timeout)
                for lang, item in zip(languages, phase2):
                    exports[lang] = item["text"]
        except Exception as e:
            logger.warning(f"design_context: desigma-mcp export failed workspace={workspace}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"desigma-mcp export failed: {e}", summary="design export failed")

        section_lines = [
            "",
            "## Design contract (customer design doc, exported via desigma-mcp)",
            _REDLINE_TEXT,
            "",
            "### Document structure",
            doc_text,
        ]
        for lang, code in exports.items():
            section_lines.append(f"### Export ({lang})")
            section_lines.append(code)
        design_section = "\n".join(section_lines)
        logger.info(
            f"design_context: export finished workspace={workspace} frame={frame_id} languages={list(exports.keys())} chars={len(design_section)}"
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"design_section": design_section, "design_present": True, "exports": exports},
            summary=f"design export finished (frame={frame_id}, {len(exports)} languages)",
        )

    @staticmethod
    def _first_frame_id(list_text: str) -> int:
        # list_nodes returns JSON text — best-effort parse of the first node id; return 0 on parse failure (document structure only)
        try:
            data = json.loads(list_text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("design_context: list_nodes output is not JSON — skipping code export, injecting document structure only")
            return 0
        nodes = data if isinstance(data, list) else (data.get("nodes") or data.get("items") or [])
        for node in nodes:
            if isinstance(node, dict) and node.get("id") is not None:
                return int(node["id"])
        logger.warning("design_context: list_nodes returned no nodes — skipping code export, injecting document structure only")
        return 0
