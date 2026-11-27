"""issue #180: python_repl silently not executing multi-statement code — regression test for the read-stdin-once fix."""

import pytest

from fusion_cowork.engine.node import NodeStatus
from fusion_cowork.nodes.tools.tool_nodes import PythonREPLNode


def _make_node(code: str, variables=None):
    from fusion_cowork.engine.node import NodeConfig

    config = NodeConfig(params={"code": code, "timeout": 30, "variables": variables or {}})
    return PythonREPLNode(config=config)


@pytest.mark.asyncio
async def test_multi_statement_code_executes():
    node = _make_node("total = 0\nfor i in range(5):\n    total += i\nprint(json.dumps({'total': total}))")
    result = await node.execute({"code": node.config.params["code"]})
    assert result.status == NodeStatus.SUCCESS
    # In exec mode user print goes to stdout; when json.loads fails, the whole stdout falls back as the result
    assert '{"total": 10}' in result.data["result"]


@pytest.mark.asyncio
async def test_multi_statement_with_side_effect_runs():
    node = _make_node("vals = [1, 2, 3]\ndoubled = [v * 2 for v in vals]\nprint(doubled)")
    result = await node.execute({"code": node.config.params["code"]})
    assert result.status == NodeStatus.SUCCESS
    assert "[2, 4, 6]" in result.data["result"]


@pytest.mark.asyncio
async def test_single_expression_still_evals():
    node = _make_node("2 + 3 * 4")
    result = await node.execute({"code": node.config.params["code"]})
    assert result.status == NodeStatus.SUCCESS
    assert result.data["result"] == "14"
    assert result.data["type"] == "int"


@pytest.mark.asyncio
async def test_runtime_error_reported():
    node = _make_node("1 / 0")
    result = await node.execute({"code": node.config.params["code"]})
    assert result.status == NodeStatus.FAILED
    assert "division by zero" in result.error


@pytest.mark.asyncio
async def test_injected_variables_available_in_exec_mode():
    node = _make_node("out = base * 2\nprint(out)", variables={"base": 21})
    result = await node.execute({"code": node.config.params["code"]})
    assert result.status == NodeStatus.SUCCESS
    assert "42" in result.data["result"]


@pytest.mark.asyncio
async def test_user_syntax_error_reported_not_silent_success():
    node = _make_node("def broken(:\n    pass")
    result = await node.execute({"code": node.config.params["code"]})
    assert result.status == NodeStatus.FAILED
