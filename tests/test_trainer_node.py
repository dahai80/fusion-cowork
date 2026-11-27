"""TrainerNode unit tests — fusion-trainer CLI subprocess wrapper node.

Only exercises fail-visible paths such as param validation / bin resolution /
method whitelist; never actually launches fusion-trainer (no dependency on real
models, per the offline-test constraint).
"""

from __future__ import annotations

import pytest

import fusion_cowork.nodes.ecosystem  # noqa: F401  triggers @register_node
from fusion_cowork import NODE_NAME_ALIASES
from fusion_cowork.engine.node import NodeCategory, NodeConfig, NodeRegistry, NodeStatus
from fusion_cowork.nodes.ecosystem.trainer_node import (
    TrainerNode,
    _build_rlsl_args,
    _build_sft_args,
    _resolve_trainer_bin,
)

_MISSING_BIN = "/nonexistent/fusion-trainer-bin-for-test"


@pytest.fixture(autouse=True)
def _force_missing_bin(monkeypatch):
    # Point at a nonexistent bin to guarantee fail-visible without really spawning
    monkeypatch.setenv("FUSION_TRAINER_BIN", _MISSING_BIN)


def test_node_registered():
    node = NodeRegistry.get("fusion_trainer")
    assert node is TrainerNode
    assert node.category == NodeCategory.FUSION_ECOSYSTEM
    assert node.name == "fusion_trainer"


def test_resolve_bin_honors_env(monkeypatch):
    import shutil as _shutil

    monkeypatch.setenv("FUSION_TRAINER_BIN", "/tmp/custom-ft")
    assert _resolve_trainer_bin() == "/tmp/custom-ft"
    # With the env removed, resolution falls through to shutil.which("fusion-trainer") — the path
    # varies by environment (CI lacks the bin), so assert against the which result instead of
    # hardcoding this machine's venv absolute path
    monkeypatch.delenv("FUSION_TRAINER_BIN", raising=False)
    resolved = _resolve_trainer_bin()
    found_in_path = _shutil.which("fusion-trainer")
    if found_in_path:
        assert resolved == found_in_path, (
            f"删 env 后应解析自 PATH: 期望 {found_in_path}, 实际 {resolved}"  # i18n: user-facing
        )  # i18n: user-facing
    else:
        # PATH has no fusion-trainer (e.g. CI ubuntu) → expect an empty string, fail-visible (no fabricated path)
        assert resolved == "", f"PATH 无 fusion-trainer 应回空串, 实际 {resolved!r}"  # i18n: user-facing


def test_aliases_registered():
    assert NODE_NAME_ALIASES.get("微调") == "fusion_trainer"  # i18n: user-facing
    assert NODE_NAME_ALIASES.get("模型微调") == "fusion_trainer"  # i18n: user-facing
    assert NODE_NAME_ALIASES.get("训练模型") == "fusion_trainer"  # i18n: user-facing


def test_build_sft_args_minimal():
    args = _build_sft_args("/data/d.jsonl", "qwen2.5-7b-4bit", "", "")
    assert args == ["sft", "--dataset", "/data/d.jsonl", "--model", "qwen2.5-7b-4bit"]


def test_build_sft_args_full():
    args = _build_sft_args("/data/d.jsonl", "m", "/c.yaml", "/out")
    assert args == [
        "sft",
        "--dataset",
        "/data/d.jsonl",
        "--model",
        "m",
        "--config",
        "/c.yaml",
        "--output",
        "/out",
    ]


def test_build_rlsl_args():
    args = _build_rlsl_args("grpo", "/data/d.jsonl", "m", "/c.yaml")
    assert args == [
        "rlsl",
        "--method",
        "grpo",
        "--dataset",
        "/data/d.jsonl",
        "--model",
        "m",
        "--config",
        "/c.yaml",
    ]


@pytest.mark.asyncio
async def test_missing_dataset_fails():
    node = TrainerNode(node_id="t1", config=NodeConfig(params={"model": "m"}))
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "dataset" in result.error


@pytest.mark.asyncio
async def test_missing_model_fails():
    node = TrainerNode(node_id="t1", config=NodeConfig(params={"dataset": "/d.jsonl"}))
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "model" in result.error


@pytest.mark.asyncio
async def test_unsupported_method_fails():
    node = TrainerNode(
        node_id="t1",
        config=NodeConfig(params={"dataset": "/d.jsonl", "model": "m", "method": "dpo"}),
    )
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "dpo" in result.error
    assert "fusion-mlx#399" in result.error


@pytest.mark.asyncio
async def test_missing_bin_fails_visibly():
    # With a nonexistent bin, the node must fail visibly and never spawn a subprocess
    node = TrainerNode(
        node_id="t1",
        config=NodeConfig(params={"dataset": "/d.jsonl", "model": "m", "method": "sft"}),
    )
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "fusion-trainer CLI 未找到" in result.error  # i18n: user-facing
    assert _MISSING_BIN in result.error


@pytest.mark.asyncio
async def test_grpo_missing_bin_fails_visibly():
    node = TrainerNode(
        node_id="t1",
        config=NodeConfig(params={"dataset": "/d.jsonl", "model": "m", "method": "grpo"}),
    )
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "fusion-trainer CLI 未找到" in result.error  # i18n: user-facing


def test_params_schema_required_fields():
    node = TrainerNode(node_id="t1")
    schema = node.get_params_schema()
    assert "dataset" in schema["required"]
    assert "model" in schema["required"]
    assert schema["properties"]["method"]["default"] == "sft"
