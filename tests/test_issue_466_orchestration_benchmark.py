"""issue #466 (W8): 编排质量基准 — 确定性场景过真实 WorkflowEngine, score=100 为 CI 门禁。

覆盖: 全场景满分、单场景过滤、探针节点幂等注册、CLI 命令。
"""

from __future__ import annotations

from fusion_cowork.benchmark.orchestration import (
    OrchestrationBenchmark,
    _ensure_probe_nodes,
)

ALL_SCENARIOS = [
    "linear_chain",
    "diamond_fanout",
    "retry_on_failure",
    "wave_parallel",
    "sub_workflow_nesting",
    "type_coercion",
]


def test_full_run_score_100():
    result = OrchestrationBenchmark().run()
    assert result["benchmark"] == "orchestration"
    assert result["total"] == len(ALL_SCENARIOS)
    assert result["failed"] == 0, [r for r in result["results"] if not r["ok"]]
    assert result["score"] == 100.0


def test_each_scenario_passes_individually():
    bench = OrchestrationBenchmark()
    for name in ALL_SCENARIOS:
        result = bench.run([name])
        assert result["passed"] == 1, (name, result["results"])
        assert result["results"][0]["ok"], (name, result["results"][0]["detail"])


def test_probe_registration_idempotent():
    _ensure_probe_nodes()
    _ensure_probe_nodes()
    from fusion_cowork.engine.node import NodeRegistry

    for probe in ("bench_echo", "bench_count", "bench_flaky", "bench_gate", "bench_slow"):
        assert NodeRegistry.get(probe) is not None


def test_cli_orchestration_command():
    from click.testing import CliRunner

    from fusion_cowork.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["benchmark", "orchestration"])
    assert res.exit_code == 0, res.output
    assert '"score": 100.0' in res.output


def test_cli_single_scenario_filter():
    from click.testing import CliRunner

    from fusion_cowork.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["benchmark", "orchestration", "-s", "linear_chain"])
    assert res.exit_code == 0, res.output
    assert '"total": 1' in res.output
