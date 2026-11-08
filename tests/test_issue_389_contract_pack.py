"""issue #389: pipeline 节点边界契约测试包 — 模板占位符 × 产出节点输出契约。

- 全部内置模板通过占位符-契约校验 + 节点注册名解析
- ${param.f} 必须在模板参数中; ${step.<nid>} 必须引用存在节点
- ${result.*}/${step.<nid>.*} 首段命中产出节点输出契约 (n12 级字段改名 → CI 红并点名缺失字段)
- 共享 fixture 供生产/消费两侧使用 (agent-studio 伴随包同路径)
"""

import pytest

from fusion_cowork.templates.contract import (
    NODE_OUTPUT_CONTRACTS,
    validate_template_placeholders,
    validate_template_structure,
)
from fusion_cowork.templates.template_manager import TemplateManager


def _all_builtin_templates():
    tm = TemplateManager()
    out = []
    for t in tm.list_templates():
        tid = t.get("id") if isinstance(t, dict) else t
        tpl = tm.get_template(tid)
        if tpl:
            out.append(tpl)
    return out


class TestBuiltinTemplatesPass:
    @pytest.mark.parametrize("tpl", _all_builtin_templates(), ids=lambda t: t["id"])
    def test_placeholders_match_contracts(self, tpl):
        errors = validate_template_placeholders(tpl)
        assert errors == [], f"template {tpl['id']} contract violations: {errors}"

    @pytest.mark.parametrize("tpl", _all_builtin_templates(), ids=lambda t: t["id"])
    def test_node_names_registered(self, tpl):
        errors = validate_template_structure(tpl)
        assert errors == [], f"template {tpl['id']} unknown nodes: {errors}"


class TestParamContract:
    def test_unknown_param_fails_with_available_list(self):
        tpl = {
            "params": {"type": "object", "properties": {"repo": {"type": "string"}}},
            "workflow": {
                "nodes": [{"id": "n1", "name": "github_api", "config": {"params": {"repo": "${param.repos}"}}}],
                "edges": [],
            },
        }
        errors = validate_template_placeholders(tpl)
        assert any("param.repos" in e and "repo" in e for e in errors)

    def test_known_param_passes(self):
        tpl = {
            "params": {"type": "object", "properties": {"repo": {"type": "string"}}},
            "workflow": {
                "nodes": [{"id": "n1", "name": "github_api", "config": {"params": {"repo": "${param.repo}"}}}],
                "edges": [],
            },
        }
        assert validate_template_placeholders(tpl) == []

    def test_step_reference_unknown_node(self):
        tpl = {
            "params": {"type": "object", "properties": {}},
            "workflow": {
                "nodes": [{"id": "n1", "name": "github_api", "config": {"params": {"x": "${step.ghost.pr_url}"}}}],
                "edges": [],
            },
        }
        errors = validate_template_placeholders(tpl)
        assert any("ghost" in e for e in errors)

    def test_empty_placeholder_path_fails(self):
        tpl = {
            "params": {"type": "object", "properties": {}},
            "workflow": {
                "nodes": [{"id": "n1", "name": "github_api", "config": {"params": {"x": "${result}"}}}],
                "edges": [],
            },
        }
        errors = validate_template_placeholders(tpl)
        assert any("缺少字段路径" in e for e in errors)


class TestOutputContract:
    def _tpl(self, producer_params, edge=True):
        nodes = [
            {"id": "n1", "name": "github_api", "config": {"params": producer_params}},
            {"id": "n2", "name": "http_request", "config": {"params": {"url": "${result.pr_url}"}}},
        ]
        return {
            "params": {"type": "object", "properties": {}},
            "workflow": {
                "nodes": nodes,
                "edges": ([{"source_id": "n1", "target_id": "n2"}] if edge else []),
            },
        }

    def test_linked_pr_envelope_key_passes(self):
        tpl = self._tpl({"op": "linked_pr", "repo": "o/r", "issue_number": 1})
        assert validate_template_placeholders(tpl) == []

    def test_n12_class_rename_fails_with_named_field(self):
        # n12 原型: 模板写 ${result.number}, 生产者给的是 pr_number → CI 红, 点名缺失字段
        tpl = self._tpl({"op": "linked_pr", "repo": "o/r", "issue_number": 1})
        tpl["workflow"]["nodes"][1]["config"]["params"]["url"] = "${result.number}"
        errors = validate_template_placeholders(tpl)
        assert len(errors) == 1
        assert "'number'" in errors[0] and "github_api" in errors[0]
        assert "pr_number" in errors[0]  # available-field list included

    def test_top_level_key_passes(self):
        tpl = self._tpl({"op": "linked_pr", "repo": "o/r", "issue_number": 1})
        tpl["workflow"]["nodes"][1]["config"]["params"]["url"] = "${result.ci_status}"
        assert validate_template_placeholders(tpl) == []

    def test_free_form_op_envelope_allows_any(self):
        tpl = self._tpl({"op": "issue_create", "repo": "o/r", "title": "t"})
        tpl["workflow"]["nodes"][1]["config"]["params"]["url"] = "${result.whatever_field}"
        assert validate_template_placeholders(tpl) == []

    def test_step_reference_typed_against_producer(self):
        tpl = {
            "params": {"type": "object", "properties": {}},
            "workflow": {
                "nodes": [
                    {"id": "n1", "name": "studio_run", "config": {"params": {"graph_id": "g"}}},
                    {"id": "n2", "name": "http_request", "config": {"params": {"url": "${step.n1.run_id}"}}},
                ],
                "edges": [{"source_id": "n1", "target_id": "n2"}],
            },
        }
        assert validate_template_placeholders(tpl) == []
        tpl["workflow"]["nodes"][1]["config"]["params"]["url"] = "${step.n1.execution_id}"
        errors = validate_template_placeholders(tpl)
        assert any("execution_id" in e and "studio_run" in e for e in errors)

    def test_undeclared_producer_passes_structural_only(self):
        tpl = {
            "params": {"type": "object", "properties": {}},
            "workflow": {
                "nodes": [
                    {"id": "n1", "name": "file_input", "config": {"params": {}}},
                    {"id": "n2", "name": "http_request", "config": {"params": {"url": "${result.anything}"}}},
                ],
                "edges": [{"source_id": "n1", "target_id": "n2"}],
            },
        }
        assert validate_template_placeholders(tpl) == []


class TestSharedFixtures:
    def test_fixture_file_exists_and_declares_contract_consumers(self):
        # 跨仓共享 fixture: 路径 + 版本落盘, agent-studio 伴随包消费同一文件
        from pathlib import Path

        fixture = Path(__file__).resolve().parent.parent / "fusion_cowork" / "templates" / "contracts.json"
        assert fixture.exists(), "shared contract fixture missing"
        import json

        data = json.loads(fixture.read_text())
        assert data.get("version") >= 1
        assert data.get("consumers") and "agent-studio" in " ".join(data["consumers"])
        # fixture 与模块注册表一致 (单一事实来源由模块持有, fixture 是导出快照)
        assert set(data["nodes"]) == set(NODE_OUTPUT_CONTRACTS)
