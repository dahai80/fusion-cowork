# issue #524: kernel SandboxRunner/CodeExecutor protocol support.
# Shared helpers so cowork executors (PythonREPLNode / ShellExecNode /
# PluginSandbox / PythonREPLCodeExecutor) speak the fusion-harness
# execution-protocol layer with one uniform result shape. Protocol
# conformance is additive — none of the underlying executors change
# behavior; see tests/test_issue_524_kernel_sandbox_conformance.py.

import logging
from typing import Any, Dict, Optional

from fusion_harness.sandbox import ExecutionTier, SandboxPolicy, SandboxRunner

logger = logging.getLogger(__name__)

# Uniform result shape for SandboxRunner.run_sandboxed implementations.
# Mirrors the agent-studio code-sandbox conformance contract:
# success / stdout / stderr / return_code always present; blocked=True
# marks fail-closed refusals (unknown tool, malformed args) so the
# caller can distinguish "ran and failed" from "never ran".


def sandbox_result(
    success: bool,
    stdout: str = "",
    stderr: str = "",
    return_code: Optional[int] = None,
    blocked: bool = False,
    error: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "return_code": return_code,
        "blocked": blocked,
        "error": error,
    }
    if extra:
        result.update(extra)
    return result


# Fail-closed refusal: the tool was never executed.
def blocked_result(tool_name: str, runner: str, reason: str) -> Dict[str, Any]:
    logger.warning("kernel sandbox runner=%s refused tool=%s: %s", runner, tool_name, reason)
    return sandbox_result(False, blocked=True, error=reason)


def kernel_sandbox_policy(
    runner: SandboxRunner,
    tool_tiers: Optional[Dict[str, ExecutionTier]] = None,
    risk_threshold: float = 0.9,
) -> SandboxPolicy:
    return SandboxPolicy(runner=runner, tool_tiers=tool_tiers, risk_threshold=risk_threshold)


__all__ = [
    "ExecutionTier",
    "SandboxPolicy",
    "SandboxRunner",
    "blocked_result",
    "kernel_sandbox_policy",
    "sandbox_result",
]
