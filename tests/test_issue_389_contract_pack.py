"""issue #389: pipeline node-boundary contract test pack — template placeholders x producer node output contracts.

- All built-in templates pass placeholder-contract validation + node registered-name resolution
- ${param.f} must exist in template params; ${step.<nid>} must reference an existing node
- ${result.*}/${step.<nid>.*} first segment must hit the producer node output contract
  (an n12-level field rename turns CI red and names the missing field)
- Shared fixtures used by both producer and consumer sides (agent-studio companion pack, same path)
"""

import pytest

from fusion_cowork.templates.contract import (
    NODE_OUTPUT_CONTRACTS,
    validate_template_placeholders,
    validate_template_structure,
)
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager


def _all_builtin_templates():
    # Contract test covers BUILTIN templates only — list_templates() also picks up
    # operator-local custom templates from ~/.fusion-cowork/templates/, which may
    # reference test-only or environment-specific nodes and are not CI's contract.
    tm = TemplateManager()
    builtin_ids = {b.get("id") for b in BUILTIN_TEMPLATES}
    out = []
    for t in tm.list_templates():
        tid = t.get("id") if isinstance(t, dict) else t
        if tid not in builtin_ids:
            continue
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
        assert any("缺少字段路径" in e for e in errors)  # i18n: user-facing


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
        # n12 prototype: template writes ${result.number}, producer supplies pr_number -> CI red, missing field named
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
        # Cross-repo shared fixture: path + version persisted on disk, agent-studio companion pack consumes the same file
        from pathlib import Path

        fixture = Path(__file__).resolve().parent.parent / "fusion_cowork" / "templates" / "contracts.json"
        assert fixture.exists(), "shared contract fixture missing"
        import json

        data = json.loads(fixture.read_text())
        assert data.get("version") >= 1
        assert data.get("consumers") and "agent-studio" in " ".join(data["consumers"])
        # Fixture matches the module registry (single source of truth held by the module; fixture is an exported snapshot)
        assert set(data["nodes"]) == set(NODE_OUTPUT_CONTRACTS)
