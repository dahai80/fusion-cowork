"""issue #343: 插值加固 — ${result.*} 信封回退 + 未命中引用明确报错。

github_api 等节点输出为 {"result": {...}} 信封, 下游 `${result.number}`
速记此前只取顶层键, 字面量漏到 int() 二次崩溃。修复: 顶层未命中时下钻
result 信封子树; 插值后仍残留 result./step./param. 引用的参数直接
"unresolved interpolation" 报错, 不再留字面量进节点。
"""

from __future__ import annotations

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


@register_node
class _IssueCreateNode(BaseNode):
    name = "issue_create_343"
    display_name = "Issue Create 343"
    category = NodeCategory.TOOL
    description = "测试用: github_api 形态输出 (result 信封)"
    icon = "🎫"
    default_label = "IssueCreate"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"result": {"number": 7, "repo_path": "/ws/probe", "pr_url": "https://github.com/o/r/pull/9"}},
            summary="ok",
        )


@register_node
class _IssueCreateTopNode(_IssueCreateNode):
    name = "issue_create_343_top"
    display_name = "Issue Create Top 343"
    description = "测试用: 顶层键 + result 信封双层输出"

    async def execute(self, inputs: dict) -> NodeResult:
        r = await super().execute(inputs)
        r.data = {"repo_path": "/ws/top", **r.data}
        return r


@register_node
class _NoisyNode(BaseNode):
    name = "noisy_343"
    display_name = "Noisy 343"
    category = NodeCategory.TOOL
    description = "测试用: 输出里带占位符字符串"
    icon = "📛"
    default_label = "Noisy"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"body": "literal ${result.nope} kept"}, summary="ok")


@register_node
class _ConsumeNode(BaseNode):
    name = "consume_343"
    display_name = "Consume 343"
    category = NodeCategory.TOOL
    description = "测试用: 记录收到的参数"
    icon = "📥"
    default_label = "Consume"

    seen: list = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).seen.append({"inputs": dict(inputs), "params": dict(self.config.params or {})})
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


def _wf(producer_name: str, consumer_params: dict) -> Workflow:
    wf = Workflow(name="interp-343")
    n1 = NodeRegistry.create(producer_name)
    n1.id = "n1"
    n2 = NodeRegistry.create("consume_343")
    n2.id = "n2"
    n2.config = NodeConfig(params=consumer_params)
    wf.add_node(n1)
    wf.add_node(n2)
    wf.connect("n1", "n2")
    return wf


class TestResultEnvelopeFallback:
    async def test_result_number_resolves_through_envelope(self):
        # 回归主场景 (issue 请求 3): issue_create 信封输出链 claude_dispatch 形态消费 —
        # `"issue_number": "${result.number}"` 收到 int 7, 不是占位符
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343", {"issue_number": "${result.number}"})
        execution = await eng.execute(wf, execution_id="run-343-number")
        assert execution.status.value == "success"
        got = _ConsumeNode.seen[-1]["params"]["issue_number"]
        assert got == 7
        assert isinstance(got, int)

    async def test_top_level_key_wins_over_envelope(self):
        # 顶层键优先 — 信封回退只在顶层未命中时生效, 不改变既有语义
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343_top", {"cwd": "${result.repo_path}"})
        execution = await eng.execute(wf, execution_id="run-343-top")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["params"]["cwd"] == "/ws/top"

    async def test_embedded_ref_with_envelope(self):
        # 嵌入串形态同样走信封回退
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343", {"cwd": "${result.repo_path}/CLAUDE.md", "note": "PR: ${result.pr_url}"})
        execution = await eng.execute(wf, execution_id="run-343-embedded")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["params"]["cwd"] == "/ws/probe/CLAUDE.md"
        assert _ConsumeNode.seen[-1]["params"]["note"] == "PR: https://github.com/o/r/pull/9"

    async def test_explicit_step_result_form(self):
        # 显式 ${step.<id>.result.*} 形态 (issue 请求 2 的备选写法) 本就可用
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343", {"issue_number": "${step.n1.result.number}"})
        execution = await eng.execute(wf, execution_id="run-343-explicit")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["params"]["issue_number"] == 7


class TestUnresolvedRefLoudFail:
    async def test_unresolved_result_ref_fails_clear(self):
        # 未命中引用不再留字面量进节点 — 明确 unresolved interpolation 报错
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343", {"issue_number": "${result.nope}"})
        execution = await eng.execute(wf, execution_id="run-343-miss")
        assert _ConsumeNode.seen == []
        assert execution.status.value == "failed"
        assert "unresolved interpolation" in (execution.error or "")
        assert "${result.nope}" in execution.error

    async def test_param_ref_missing_at_assembly_fails_clear(self):
        # ${param.*} 装配期缺参残留到运行期 — 同样明确报错 (原为 float()/int() 二次崩溃)
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343", {"timeout": "${param.dispatch_timeout}"})
        execution = await eng.execute(wf, execution_id="run-343-param")
        assert _ConsumeNode.seen == []
        assert "unresolved interpolation" in (execution.error or "")
        assert "${param.dispatch_timeout}" in execution.error

    async def test_input_side_refs_stay_lenient(self):
        # 输入侧 (节点产出数据) 保持宽松: 字面量保留 + WARNING, run 不因上游数据里的
        # 占位符字符串失败 — 明确报错只针对作者声明的节点参数
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = Workflow(name="interp-343-lenient")
        n1 = NodeRegistry.create("noisy_343")
        n1.id = "n1"
        n2 = NodeRegistry.create("consume_343")
        n2.id = "n2"
        n2.config = NodeConfig(params={})
        wf.add_node(n1)
        wf.add_node(n2)
        wf.connect("n1", "n2")
        execution = await eng.execute(wf, execution_id="run-343-lenient")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["inputs"]["body"] == "literal ${result.nope} kept"

    async def test_run_id_placeholder_untouched(self):
        # ${run_id} 无引擎前缀 — 不是本机制引用, 不误报不替换
        _ConsumeNode.seen = []
        eng = WorkflowEngine()
        wf = _wf("issue_create_343", {"body": "evidence archived (run ${run_id})"})
        execution = await eng.execute(wf, execution_id="run-343-runid")
        assert execution.status.value == "success"
        assert _ConsumeNode.seen[-1]["params"]["body"] == "evidence archived (run ${run_id})"
