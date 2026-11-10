"""issue #431: design_context 节点 — 经 desigma-mcp (stdio) 导出客户设计稿供 coder 实现红线。

零设计稿 (workspace 空) 时空转成功 — 管线行为与旧版完全一致; 有设计稿时导出
文档结构 + 指定 frame 的代码导出, design_section 以完整段落注入 coder prompt。
子进程 spawn 由 FUSION_COWORK_MCP_STDIO_ALLOW 白名单统一把关 (mcp_stdio 模块);
不进 HIGH_RISK_NODES — 空转路径不 spawn, 白名单缺省空时命令一律拒绝 (fail-closed),
进高危清单反而让无设计稿运行也被 HITL 暂停, 破坏 "零设计稿行为不变" 红线。
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
    display_name = "设计上下文导出"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "经 desigma-mcp (stdio) 导出客户设计稿; 无设计稿时空转 (issue #431)"
    icon = "🎨"
    default_label = "设计上下文"

    inputs = [
        {"key": "workspace", "label": "设计稿工作区路径", "type": "string"},
    ]
    outputs = [
        {"key": "design_section", "label": "设计契约段落 (注入 coder prompt; 空串 = 无设计稿)", "type": "string"},
        {"key": "design_present", "label": "是否携带设计稿", "type": "boolean"},
        {"key": "exports", "label": "各语言代码导出 {language: code}", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "设计稿工作区路径 (.desigma 目录); 空 = 无设计稿, 空转成功",
                    "default": "",
                },
                "command": {
                    "type": "string",
                    "description": "MCP stdio 服务器命令 (basename 须在 FUSION_COWORK_MCP_STDIO_ALLOW 白名单)",
                    "default": "desigma-mcp",
                },
                "args": {
                    "type": "array",
                    "description": "MCP 服务器命令行参数 (缺省 --workspace <workspace>)",
                    "default": [],
                },
                "frame_id": {
                    "type": "number",
                    "description": "导出的 frame 节点 id; 0 = 取 list_nodes 首个节点",
                    "default": 0,
                },
                "languages": {
                    "type": "array",
                    "description": "导出语言 (desigma generate_code 枚举子集)",
                    "default": ["react", "tailwind"],
                },
                "timeout": {"type": "number", "description": "单请求超时 (秒)", "default": 60},
            },
        }

    def _spawn(self, command: str, args: List[Any], calls: List[Dict[str, Any]], timeout: float):
        # 测试接缝: monkeypatch 注入伪 stdio 会话
        from fusion_cowork.nodes.ecosystem.mcp_stdio import stdio_tool_calls

        return stdio_tool_calls(command, args, calls, timeout=timeout)

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        workspace = str(inputs.get("workspace") or params.get("workspace") or "").strip()
        if not workspace:
            logger.info("design_context: 无设计稿 (workspace 空) — 空转, 管线行为不变")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"design_section": "", "design_present": False, "exports": {}},
                summary="无设计稿 — 跳过导出",
            )

        command = str(params.get("command") or "desigma-mcp").strip()
        from fusion_cowork.nodes.ecosystem.mcp_stdio import STDIO_ALLOW_ENV, stdio_allowed, stdio_allowlist

        if not stdio_allowed(command):
            allowed = ", ".join(stdio_allowlist()) or "(空 — 一律拒绝)"
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"命令 {command} 不在 {STDIO_ALLOW_ENV} 白名单内 (当前: {allowed})",
                summary="stdio 命令未授权",
            )

        args = list(params.get("args") or [])
        if not args:
            args = ["--workspace", workspace]
        timeout = float(params.get("timeout", 60) or 60)
        languages = [str(lang) for lang in (params.get("languages") or ["react", "tailwind"])]
        frame_id = int(params.get("frame_id") or 0)

        try:
            # 第一阶段: 文档结构 + 节点列表 (frame_id=0 时从列表挑首个节点)
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
            logger.warning(f"design_context: desigma-mcp 导出失败 workspace={workspace}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"desigma-mcp 导出失败: {e}", summary="设计导出失败")

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
            f"design_context: 导出完成 workspace={workspace} frame={frame_id} languages={list(exports.keys())} chars={len(design_section)}"
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"design_section": design_section, "design_present": True, "exports": exports},
            summary=f"设计导出完成 (frame={frame_id}, {len(exports)} 种语言)",
        )

    @staticmethod
    def _first_frame_id(list_text: str) -> int:
        # list_nodes 返回 JSON 文本 — 尽力解析首个节点 id; 解析失败返回 0 (只带文档结构)
        try:
            data = json.loads(list_text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("design_context: list_nodes 输出非 JSON — 跳过代码导出, 仅注入文档结构")
            return 0
        nodes = data if isinstance(data, list) else (data.get("nodes") or data.get("items") or [])
        for node in nodes:
            if isinstance(node, dict) and node.get("id") is not None:
                return int(node["id"])
        logger.warning("design_context: list_nodes 无节点 — 跳过代码导出, 仅注入文档结构")
        return 0
