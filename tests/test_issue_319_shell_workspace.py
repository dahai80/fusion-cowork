"""issue #319: shell_exec HITL run faked success + workspace-root escape.

1. shell operators (>, &&) with shell=false become literal argv via shlex.split —
   echo "succeeds", no file written. Fix: loud failure listing the operator.
2. working_directory param from LLM workflows was ignored (node reads workdir) —
   alias honored now.
3. FUSION_COWORK_WORKSPACE_ROOT confinement: empty workdir defaults to the root,
   any workdir must resolve inside the root (~ escapes, rejected), relative paths
   resolve against the root. Unset env keeps legacy behavior.
"""

from __future__ import annotations

import asyncio

from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


def _shell(params: dict):
    return NodeRegistry.create("shell_exec", node_id="sh", config=NodeConfig(params=params))


def test_metachar_command_rejected_without_shell():
    node = _shell({"command": "echo hello > f.txt && cat f.txt"})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.FAILED
    assert "shell operator" in (result.error or "")


def test_metachar_command_runs_with_shell_true(tmp_path):
    node = _shell(
        {
            "command": f"echo vps-e2e > {tmp_path}/hello.txt && cat {tmp_path}/hello.txt",
            "shell": True,
        }
    )
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS, result.error
    assert "vps-e2e" in result.data["stdout"]
    assert (tmp_path / "hello.txt").exists()


def test_working_directory_alias_honored(tmp_path):
    node = _shell({"command": "pwd", "working_directory": str(tmp_path)})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.data["stdout"].strip() == str(tmp_path)


def test_workspace_root_default_workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    node = _shell({"command": "pwd"})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.data["stdout"].strip() == str(tmp_path / "ws")


def test_workspace_root_confines_tilde_escape(monkeypatch, tmp_path):
    # exact #319 scenario: approval panel showed working_directory=~ — must not escape
    monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    node = _shell({"command": "pwd", "working_directory": "~"})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.FAILED
    assert "workspace root" in (result.error or "")


def test_workspace_root_relative_workdir_resolved_inside(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    node = _shell({"command": "pwd", "workdir": "sub/dir"})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.data["stdout"].strip() == str(tmp_path / "ws" / "sub" / "dir")


def test_workspace_root_dotdot_escape_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    node = _shell({"command": "pwd", "workdir": "../outside"})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.FAILED
    assert "escapes workspace root" in (result.error or "")


def test_no_env_keeps_legacy_workdir(monkeypatch, tmp_path):
    monkeypatch.delenv("FUSION_COWORK_WORKSPACE_ROOT", raising=False)
    node = _shell({"command": "pwd", "workdir": str(tmp_path)})
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.data["stdout"].strip() == str(tmp_path)
