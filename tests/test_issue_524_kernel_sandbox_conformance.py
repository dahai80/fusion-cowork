# issue #524: kernel SandboxRunner/CodeExecutor protocol conformance.
# Each cowork executor passes the kernel protocol contract: tier
# reporting, policy validation, fail-closed error shapes. Existing
# tool-node behavior unchanged — conformance is additive.

import pytest
from fusion_harness.code_mode import CodeModeTurn
from fusion_harness.sandbox import ExecutionTier, SandboxPolicy, SandboxRunner

from fusion_cowork.nodes.tools.tool_nodes import (
    PythonREPLCodeExecutor,
    PythonREPLNode,
    ShellExecNode,
)
from fusion_cowork.plugins.sandbox import PluginSandbox


def _shell_runner() -> ShellExecNode:
    return ShellExecNode()


def _repl_runner() -> PythonREPLNode:
    return PythonREPLNode()


def _plugin_runner() -> PluginSandbox:
    return PluginSandbox()


def _repl_executor() -> PythonREPLCodeExecutor:
    return PythonREPLCodeExecutor(timeout=20)


class TestSandboxRunnerConformance:
    @pytest.mark.parametrize("make_runner", [_shell_runner, _repl_runner, _plugin_runner])
    def test_runtime_checkable_protocol(self, make_runner):
        assert isinstance(make_runner(), SandboxRunner)

    @pytest.mark.parametrize("make_runner", [_shell_runner, _repl_runner, _plugin_runner])
    def test_tier_reported(self, make_runner):
        assert make_runner().execution_tier in (ExecutionTier.PROCESS, ExecutionTier.SANDBOX)

    @pytest.mark.parametrize("make_runner", [_shell_runner, _repl_runner, _plugin_runner])
    def test_policy_described(self, make_runner):
        policy = make_runner().describe_sandbox_policy()
        assert policy["tier"] in ("process", "sandbox")
        assert isinstance(policy["checks"], list) and policy["checks"]

    @pytest.mark.asyncio
    async def test_shell_run_sandboxed_payload(self):
        result = await _shell_runner().run_sandboxed("shell_exec", {"command": "echo shell_ok"})
        assert result["success"] is True
        assert "shell_ok" in result["stdout"]
        assert result["return_code"] == 0

    @pytest.mark.asyncio
    async def test_shell_run_sandboxed_bash_alias(self):
        result = await _shell_runner().run_sandboxed("bash", {"cmd": "echo alias_ok"})
        assert result["success"] is True
        assert "alias_ok" in result["stdout"]

    @pytest.mark.asyncio
    async def test_repl_run_sandboxed_payload(self):
        result = await _repl_runner().run_sandboxed("python_repl", {"code": "1 + 1"})
        assert result["success"] is True
        assert result["result"] == "2"

    @pytest.mark.asyncio
    async def test_repl_run_sandboxed_code_execute_alias(self):
        result = await _repl_runner().run_sandboxed("code_execute", {"code": "40 + 2"})
        assert result["success"] is True
        assert result["result"] == "42"

    @pytest.mark.asyncio
    async def test_plugin_run_sandboxed_argv_payload(self):
        result = await _plugin_runner().run_sandboxed("plugin_run", {"command": ["/bin/echo", "plugin_ok"]})
        assert result["success"] is True
        assert "plugin_ok" in result["stdout"]
        assert result["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_plugin_run_sandboxed_string_command(self):
        result = await _plugin_runner().run_sandboxed("plugin_run", {"command": "/bin/echo string_ok"})
        assert result["success"] is True
        assert "string_ok" in result["stdout"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_runner,args",
        [
            (_shell_runner, {"command": "echo hi"}),
            (_repl_runner, {"code": "1"}),
            (_plugin_runner, {"command": ["/bin/echo", "hi"]}),
        ],
    )
    async def test_run_sandboxed_fail_closed_unknown_tool(self, make_runner, args):
        result = await make_runner().run_sandboxed("mystery_tool", args)
        assert result["success"] is False
        assert result.get("blocked") is True
        assert "not sandboxable" in result["stderr"] or "not sandboxable" in result["error"]

    @pytest.mark.asyncio
    async def test_run_sandboxed_fail_closed_malformed_args(self):
        shell = await _shell_runner().run_sandboxed("shell_exec", {})
        repl = await _repl_runner().run_sandboxed("python_repl", {"code": "   "})
        plugin = await _plugin_runner().run_sandboxed("plugin_run", {})
        for result in (shell, repl, plugin):
            assert result["success"] is False
            assert result["blocked"] is True

    @pytest.mark.asyncio
    async def test_shell_run_sandboxed_static_check_blocks(self):
        result = await _shell_runner().run_sandboxed("shell_exec", {"command": "rm -rf /"})
        assert result["success"] is False
        assert result["blocked"] is True

    @pytest.mark.asyncio
    async def test_repl_run_sandboxed_danger_import_blocks(self):
        result = await _repl_runner().run_sandboxed("python_repl", {"code": "import subprocess"})
        assert result["success"] is False
        assert result["blocked"] is True

    @pytest.mark.parametrize("make_runner", [_shell_runner, _repl_runner, _plugin_runner])
    def test_policy_routes_via_runner_not_blocked(self, make_runner):
        decision = SandboxPolicy(runner=make_runner()).decide("any_tool", {}, (0.95, "destructive"))
        assert decision.tier is ExecutionTier.SANDBOX

    @pytest.mark.parametrize("make_runner", [_shell_runner, _repl_runner, _plugin_runner])
    def test_policy_without_runner_blocks_destructive(self, make_runner):
        decision = SandboxPolicy(runner=None).decide("any_tool", {}, (0.95, "destructive"))
        assert decision.tier is ExecutionTier.BLOCKED

    def test_plugin_tier_is_sandbox(self):
        assert _plugin_runner().execution_tier is ExecutionTier.SANDBOX

    def test_kernel_sandbox_policy_helper(self):
        from fusion_cowork.security.kernel_sandbox import kernel_sandbox_policy

        policy = kernel_sandbox_policy(_plugin_runner())
        assert policy.runner is not None
        assert policy.decide("x", {}, (0.95, "destructive")).tier is ExecutionTier.SANDBOX


class TestCodeExecutorConformance:
    def test_print_script_returns_none_result(self):
        # user stdout is captured by the wrapper, never pollutes the JSON channel
        assert _repl_executor().execute("print(21 * 2)", {"seed": 7}) is None

    def test_result_variable_round_trips(self):
        executor = _repl_executor()
        assert executor.execute("result = 21 * 2", {}) == 42

    def test_namespace_injected_as_globals(self):
        executor = _repl_executor()
        assert executor.execute("result = n * 2", {"n": 4}) == 8

    def test_no_result_variable_returns_none(self):
        assert _repl_executor().execute("x = 1", {}) is None

    def test_error_raises_for_self_repair_loop(self):
        executor = _repl_executor()
        with pytest.raises(RuntimeError, match="boom"):
            executor.execute("raise ValueError('boom')", {})

    def test_safety_check_failure_raises(self):
        executor = _repl_executor()
        with pytest.raises(RuntimeError, match="Safety check failed"):
            executor.execute("import subprocess\nresult = 1", {})

    def test_bad_namespace_identifier_raises(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            _repl_executor().execute("result = 1", {"__dunder__": 1})

    def test_non_json_namespace_raises(self):
        with pytest.raises(ValueError, match="JSON-serializable"):
            _repl_executor().execute("result = 1", {"obj": object()})

    def test_empty_script_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _repl_executor().execute("   ", {})

    def test_timeout_raises(self):
        executor = PythonREPLCodeExecutor(timeout=2)
        with pytest.raises(RuntimeError, match="timed out"):
            executor.execute("import time\ntime.sleep(10)\nresult = 1", {})

    def test_tier_reported(self):
        assert _repl_executor().execution_tier is ExecutionTier.PROCESS
        assert _repl_executor().describe_sandbox_policy()["tier"] == "process"

    @pytest.mark.asyncio
    async def test_code_mode_turn_end_to_end(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        turn = CodeModeTurn(tools, _repl_executor())
        text = "```python\nresult = total * 2\n```"
        result = turn.run(text, {"total": 100})
        assert result.ok is True
        assert result.value == 200
