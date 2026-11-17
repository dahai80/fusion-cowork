"""issue #509: NL 生成器幻觉路径确定性剔除。

LLM 可能为文件节点编造用户从未给出的本地路径, 首跑必败
(如 ~/Documents/支出记录)。生成后做确定性净化: 形如绝对/家目录/相对
路径的参数值, 未出现在用户输入或上下文中 → 置空留待用户填写, 并写入
workflow_def["warnings"]; 用户/上下文明确给出的路径与 URL 保持原样。
"""

from __future__ import annotations

import json
from typing import List

import pytest

from fusion_cowork.ai.mlx_client import LLMResponse
from fusion_cowork.ai.nl_parser import NLWorkflowGenerator, _sanitize_generated_paths


class _MockMLX:
    def __init__(self, content: str):
        self.content = content
        self.models: List[dict] = [{"id": "Qwen3.8-27B-4bit", "engine_type": "text_generation"}]

    async def list_models(self):
        return self.models

    async def chat(self, model: str, messages, temperature: float = 0.2, max_tokens: int = 4096):
        return LLMResponse(content=self.content, finish_reason="stop")


def _wf_json(paths: dict) -> str:
    nodes = [
        {"id": "n1", "name": "file_input", "config": {"params": {"path": paths.get("input", "")}}},
        {"id": "n2", "name": "file_output", "config": {"params": {"path": paths.get("output", "")}}},
    ]
    return json.dumps(
        {"name": "记账助手", "nodes": nodes, "edges": [{"source_id": "n1", "target_id": "n2"}]}, ensure_ascii=False
    )


class TestSanitizeGeneratedPaths:
    def test_hallucinated_path_blanked_with_warning(self):
        wf = {"nodes": [{"id": "n1", "config": {"params": {"path": "~/Documents/支出记录"}}}]}
        _sanitize_generated_paths(wf, "生成一个AI记账助手工作流", None)
        assert wf["nodes"][0]["config"]["params"]["path"] == ""
        assert len(wf["warnings"]) == 1
        assert "~/Documents/支出记录" in wf["warnings"][0]

    def test_user_provided_path_kept(self):
        wf = {"nodes": [{"id": "n1", "config": {"params": {"path": "~/Documents/账单"}}}]}
        _sanitize_generated_paths(wf, "处理 ~/Documents/账单 里的支出", None)
        assert wf["nodes"][0]["config"]["params"]["path"] == "~/Documents/账单"
        assert "warnings" not in wf

    def test_context_provided_path_kept(self):
        wf = {"nodes": [{"id": "n1", "config": {"params": {"path": "/data/inbox"}}}]}
        _sanitize_generated_paths(wf, "整理目录", {"cwd_files": ["/data/inbox"]})
        assert wf["nodes"][0]["config"]["params"]["path"] == "/data/inbox"
        assert "warnings" not in wf

    def test_url_untouched(self):
        wf = {"nodes": [{"id": "n1", "config": {"params": {"path": "https://example.com/data.csv"}}}]}
        _sanitize_generated_paths(wf, "抓取数据", None)
        assert wf["nodes"][0]["config"]["params"]["path"] == "https://example.com/data.csv"

    def test_relative_dot_paths_also_checked(self):
        wf = {"nodes": [{"id": "n1", "config": {"params": {"path": "./invented/dir"}}}]}
        _sanitize_generated_paths(wf, "做点什么", None)
        assert wf["nodes"][0]["config"]["params"]["path"] == ""

    def test_non_path_params_untouched(self):
        wf = {"nodes": [{"id": "n1", "config": {"params": {"days_threshold": 30, "action": "clean"}}}]}
        _sanitize_generated_paths(wf, "清理", None)
        assert wf["nodes"][0]["config"]["params"] == {"days_threshold": 30, "action": "clean"}
        assert "warnings" not in wf


class TestGeneratorEndToEnd:
    @pytest.mark.asyncio
    async def test_generate_sanitizes_hallucinated_path(self):
        content = _wf_json({"input": "~/Documents/支出记录"})
        gen = NLWorkflowGenerator(_MockMLX(content))  # type: ignore[arg-type]
        wf = await gen.generate("生成一个AI记账助手工作流：读取本月支出数据，AI 自动分类汇总")
        assert wf["nodes"][0]["config"]["params"]["path"] == ""
        assert wf["warnings"]

    @pytest.mark.asyncio
    async def test_generate_keeps_user_path(self):
        content = _wf_json({"input": "~/Documents/账单"})
        gen = NLWorkflowGenerator(_MockMLX(content))  # type: ignore[arg-type]
        wf = await gen.generate("处理 ~/Documents/账单 里的支出数据")
        assert wf["nodes"][0]["config"]["params"]["path"] == "~/Documents/账单"
        assert "warnings" not in wf

    @pytest.mark.asyncio
    async def test_generate_parse_error_path_untouched(self):
        gen = NLWorkflowGenerator(_MockMLX("不是 JSON"))  # type: ignore[arg-type]
        wf = await gen.generate("随便", raise_on_parse_error=False)
        assert wf.get("error")
        assert wf["nodes"] == []

    def test_system_prompt_has_path_rule(self):
        from fusion_cowork.ai.nl_parser import SYSTEM_PROMPT

        assert "严禁编造" in SYSTEM_PROMPT
