from pathlib import Path

import pytest

from fusion_cowork.skills.packages import (
    SkillPackageRegistry,
    _params_from_schema,
    get_package_registry,
    parse_markdown,
    reset_package_registry,
)

SHARED_PACK = """---
name: contract-review
command: /contract-review
description: Local contract review
version: "1.0"
allowed-tools: [fetch_url, file_input]
parameters:
  type: object
  required: [contract_path]
  properties:
    contract_path:
      type: string
      title: Contract file
      description: path to the contract
    strictness:
      type: string
      enum: [low, medium, high]
      default: medium
    max_issues:
      type: integer
      default: 10
    deep:
      type: boolean
      default: false
    tags:
      type: array
output_template: |
  ## Findings
---

Review the contract and produce findings.
"""

COWORK_PACK = """---
name: legacy-pack
handler: noop
params:
  - name: target
    type: string
    required: true
---

Do the thing.
"""


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_package_registry()
    yield
    reset_package_registry()


class TestSharedSpecParsing:
    def test_studio_style_frontmatter_parses(self):
        pkg = parse_markdown(SHARED_PACK, source="shared.md")
        assert pkg.name == "/contract-review"
        assert pkg.command == "/contract-review"
        assert pkg.version == "1.0"
        assert pkg.allowed_tools == ["fetch_url", "file_input"]
        assert "Review the contract" in pkg.instructions
        assert "## Findings" in pkg.output_template

    def test_parameters_schema_maps_to_param_specs(self):
        pkg = parse_markdown(SHARED_PACK)
        by_name = {p.name: p for p in pkg.params}
        assert set(by_name) == {"contract_path", "strictness", "max_issues", "deep", "tags"}
        assert by_name["contract_path"].required is True
        assert by_name["contract_path"].type == "string"
        assert by_name["strictness"].options == ["low", "medium", "high"]
        assert by_name["strictness"].default == "medium"
        assert by_name["max_issues"].type == "int"
        assert by_name["deep"].type == "bool"
        assert by_name["tags"].type == "list"
        assert by_name["contract_path"].label == "Contract file"

    def test_cowork_params_list_takes_precedence(self):
        pkg = parse_markdown(COWORK_PACK)
        assert [p.name for p in pkg.params] == ["target"]
        assert pkg.params[0].required is True

    def test_hyphen_aliases_accepted(self):
        text = SHARED_PACK.replace("allowed-tools:", "allowed_tools:").replace("output_template:", "output-template:")
        pkg = parse_markdown(text)
        assert pkg.allowed_tools == ["fetch_url", "file_input"]
        assert "## Findings" in pkg.output_template

    def test_command_only_pack_gets_slash_name(self):
        text = SHARED_PACK.replace("name: contract-review\n", "").replace(
            "command: /contract-review", "slash: contract-review"
        )
        pkg = parse_markdown(text)
        assert pkg.name == "/contract-review"
        assert pkg.command == "/contract-review"

    def test_to_dict_roundtrip_fields(self):
        pkg = parse_markdown(SHARED_PACK)
        data = pkg.to_dict()
        assert data["version"] == "1.0"
        assert data["allowed_tools"] == ["fetch_url", "file_input"]
        assert data["command"] == "/contract-review"
        assert data["params"][0]["name"] == "contract_path"


class TestSchemaParamsCoerceAndRun:
    async def test_schema_params_coerce_values(self):
        registry = SkillPackageRegistry()
        registry.register(parse_markdown(SHARED_PACK))
        result = await registry.run(
            "/contract-review",
            {"contract_path": "~/c.md", "max_issues": "5", "deep": "yes", "tags": "a, b", "strictness": "high"},
        )
        assert "error" not in result or result.get("error") != "参数校验失败"

    async def test_missing_required_param_rejected(self):
        registry = SkillPackageRegistry()
        registry.register(parse_markdown(SHARED_PACK))
        result = await registry.run("/contract-review", {})
        assert result.get("error") == "参数校验失败"

    async def test_enum_out_of_range_rejected(self):
        registry = SkillPackageRegistry()
        registry.register(parse_markdown(SHARED_PACK))
        result = await registry.run("/contract-review", {"contract_path": "c.md", "strictness": "banana"})
        assert result.get("error") == "参数校验失败"


class TestBuiltinContractReviewSharedFormat:
    def test_builtin_pack_is_shared_format(self):
        path = Path(__file__).parent.parent / "fusion_cowork" / "skills" / "packages" / "contract-review.md"
        pkg = parse_markdown(path.read_text(encoding="utf-8"), source=str(path))
        assert pkg.name == "/contract-review"
        assert pkg.version
        assert pkg.allowed_tools
        assert pkg.handler == "contract_review"
        by_name = {p.name: p for p in pkg.params}
        assert by_name["contract_path"].required is True
        assert by_name["playbook_query"].default == "合同审查要点"

    async def test_builtin_registry_discovers_and_runs(self):
        registry = get_package_registry(reload=True)
        pkg = registry.get("/contract-review")
        assert pkg is not None
        data = pkg.to_dict()
        assert data["params"], "结构化表单 schema 必须从 parameters 派生"
        assert data["allowed_tools"]


class TestSharedInstallDirs:
    def test_extra_skills_dir_discovered(self, tmp_path, monkeypatch):
        pack = tmp_path / "shared-review.md"
        pack.write_text(SHARED_PACK, encoding="utf-8")
        monkeypatch.setenv("FUSION_COWORK_SKILLS_DIRS", str(tmp_path))
        registry = get_package_registry(reload=True)
        assert registry.get("/contract-review") is not None
        assert registry.get("/contract-review").source == str(pack)

    def test_colon_separated_multi_dirs(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        (a / "pack-a.md").write_text(COWORK_PACK, encoding="utf-8")
        (b / "pack-b.md").write_text(COWORK_PACK.replace("legacy-pack", "legacy-pack-b"), encoding="utf-8")
        monkeypatch.setenv("FUSION_COWORK_SKILLS_DIRS", f"{a}:{b}")
        registry = get_package_registry(reload=True)
        assert registry.get("/legacy-pack") is not None
        assert registry.get("/legacy-pack-b") is not None


def test_params_from_schema_bad_input():
    assert _params_from_schema(None) == []
    assert _params_from_schema({"properties": "nope"}) == []
    assert _params_from_schema({}) == []
