"""Fusion-Cowork CLI 入口 — 命令行界面。

提供完整的命令行操作：
- 模板列表/搜索/运行
- 自然语言生成工作流
- 工作流执行
- 定时任务管理
- AI 服务状态
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List

import click

from . import NODE_NAME_ALIASES, __app_name__, __version__
from .ai import FusionMLXClient, NLWorkflowGenerator
from .engine import (
    EventEmitter,
    HookManager,
    NodeConfig,
    NodeRegistry,
    PermissionManager,
    SessionStore,
    TaskScheduler,
    TaskStatus,
    Workflow,
    WorkflowEngine,
    WorkflowStatus,
)

# LO-3: nodes.browser/input_nodes 顶层 import 耦合 — 移入用到的命令函数内延迟导入,
# 防 nodes.browser 将来拉缺失可选依赖致 import fusion_cowork.cli 全坏 (每个子命令挂)
from .templates import TemplateManager
from .utils.logger import setup_logger

logger = logging.getLogger(__name__)

# 全局实例 — 延迟初始化避免测试污染
_engine = None
_scheduler = None
_template_mgr = None
_mlx_client = None
_runtime = None


def _build_runtime():
    global _runtime
    if _runtime is None:
        permission_manager = PermissionManager()
        hook_manager = HookManager()
        session_store = SessionStore()
        event_emitter = EventEmitter()
        space_store = None
        try:
            from .space import SpaceStore

            space_store = SpaceStore()
        except Exception as e:
            logger.warning(f"SpaceStore 构建失败 (desk.space.* 将不可用): {e}")
        _runtime = {
            "permission_manager": permission_manager,
            "hook_manager": hook_manager,
            "session_store": session_store,
            "event_emitter": event_emitter,
            "space_store": space_store,
        }
        logger.info("运行时管理器已构建: permission/hook/session/event/space")
    return _runtime


def _get_engine() -> WorkflowEngine:
    global _engine
    if _engine is None:
        rt = _build_runtime()
        _engine = WorkflowEngine(
            permission_manager=rt["permission_manager"],
            hook_manager=rt["hook_manager"],
            session_store=rt["session_store"],
            event_emitter=rt["event_emitter"],
        )
    return _engine


def _get_scheduler() -> TaskScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler


def _get_template_mgr() -> TemplateManager:
    global _template_mgr
    if _template_mgr is None:
        _template_mgr = TemplateManager()
    return _template_mgr


def _get_mlx_client() -> FusionMLXClient:
    global _mlx_client
    if _mlx_client is None:
        _mlx_client = FusionMLXClient()
    return _mlx_client


def _cleanup_mlx_client():
    global _mlx_client
    if _mlx_client is None:
        return
    try:
        # LO-1: asyncio.get_event_loop() py3.12 弃用且无 running loop 时报错;
        # atexit 跑在主线程无 loop, 用 asyncio.run 起新 loop 跑 close coroutine
        try:
            asyncio.get_running_loop()
            # 有 running loop (不应出现在 atexit, 但防御): 派 task 不阻塞
            asyncio.ensure_future(_mlx_client.close())
        except RuntimeError:
            asyncio.run(_mlx_client.close())
    except Exception as e:
        logger.debug(f"清理 mlx_client 失败: {e}")
    finally:
        _mlx_client = None


atexit.register(_cleanup_mlx_client)


class RichConsole:
    """简易终端输出（无需 rich 依赖的降级方案）。"""

    @staticmethod
    def echo(msg: str = "", fg: str = "", bold: bool = False) -> None:
        click.echo(msg)

    @staticmethod
    def print_header(title: str) -> None:
        width = 60
        click.echo()
        click.echo("=" * width)
        click.echo(f"  {title}")
        click.echo("=" * width)

    @staticmethod
    def print_success(msg: str) -> None:
        click.echo(f"✅ {msg}")

    @staticmethod
    def print_error(msg: str) -> None:
        click.echo(f"❌ {msg}")

    @staticmethod
    def print_info(msg: str) -> None:
        click.echo(f"ℹ️  {msg}")

    @staticmethod
    def print_warning(msg: str) -> None:
        click.echo(f"⚠️  {msg}")

    @staticmethod
    def print_result(msg: str) -> None:
        click.echo(msg)

    @staticmethod
    def print_table(headers: List[str], rows: List[List[str]]) -> None:
        """打印简单表格。"""
        # 计算列宽
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], len(str(cell)))

        # 表头
        header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
        click.echo(header_line)
        click.echo("-" * len(header_line))

        # 数据行
        for row in rows:
            click.echo("  ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)))


console = RichConsole()


# ── CLI 主命令组 ──


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="详细输出")
@click.option("--log-file", default="", help="日志文件路径")
@click.option(
    "--max-budget-usd",
    "max_budget_usd",
    default=0.0,
    type=float,
    help="Token 预算上限 (USD), 超限中止 LLM 调用 (0=不限)",
)
@click.version_option(version=__version__, prog_name=__app_name__)
def cli(verbose: bool, log_file: str, max_budget_usd: float):
    """Fusion-Cowork — macOS 原生、纯本地离线、零代码桌面智能自动化平台。

    让 Mac 自己干活，本地 AI 全自动桌面办公。
    """
    level = logging.DEBUG if verbose else logging.INFO
    setup_logger(level=level, log_file=log_file, verbose=verbose)

    if max_budget_usd > 0:
        from .ai import get_budget_tracker

        get_budget_tracker(max_budget_usd=max_budget_usd, enforce=True)
        logger.info(f"已启用 Token 预算上限: ${max_budget_usd:.4f}")

    # 注册工具名称别名（吸纳自 Squish tool_name_map.py 模式）
    _register_node_aliases()


def _register_node_aliases():
    """注册节点别名映射，支持用户友好的中文名称查找。

    整合自 Squish tool_name_map.py 的 VSCODE_TO_BACKEND 映射模式。
    """
    for alias, target in NODE_NAME_ALIASES.items():
        NodeRegistry.register_alias(alias, target)


# ── 模板命令 ──


@cli.group()
def template():
    """模板管理：列出、搜索、运行自动化模板。"""
    pass


@template.command("list")
@click.option("--category", "-c", default="", help="按分类筛选")
@click.option("--tag", "-t", default="", help="按标签筛选")
@click.option("--search", "-s", default="", help="搜索关键词")
def list_templates(category: str, tag: str, search: str):
    """列出可用模板。"""
    if search:
        templates = _get_template_mgr().search_templates(search)
    else:
        templates = _get_template_mgr().list_templates(category=category, tag=tag)

    if not templates:
        console.print_warning("没有找到匹配的模板")
        return

    console.print_header(f"模板列表 ({len(templates)} 个)")

    # 按分类分组
    categories = {}
    for tpl in templates:
        cat = tpl.get("category", "其他")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(tpl)

    for cat, cat_templates in categories.items():
        click.echo(f"\n📁 {cat}:")
        rows = []
        for tpl in cat_templates:
            ai_tag = " 🤖" if tpl.get("needs_ai") else ""
            rows.append(
                [
                    tpl.get("id", ""),
                    f"{tpl.get('icon', '📄')} {tpl.get('name', '')}{ai_tag}",
                    tpl.get("difficulty", ""),
                    tpl.get("estimated_time", ""),
                ]
            )
        console.print_table(["ID", "名称", "难度", "预计时间"], rows)

    click.echo("\n💡 使用 'fusion-cowork template run <id>' 运行模板")
    click.echo("💡 使用 'fusion-cowork template show <id>' 查看详情")


@template.command("show")
@click.argument("template_id")
def show_template(template_id: str):
    """查看模板详情。"""
    tpl = _get_template_mgr().get_template(template_id)
    if not tpl:
        console.print_error(f"模板不存在: {template_id}")
        return

    console.print_header(f"📄 {tpl.get('name', '')}")
    click.echo(f"  描述: {tpl.get('description', '')}")
    click.echo(f"  分类: {tpl.get('category', '')}")
    click.echo(f"  标签: {', '.join(tpl.get('tags', []))}")
    click.echo(f"  难度: {tpl.get('difficulty', '')}")
    click.echo(f"  预计时间: {tpl.get('estimated_time', '')}")
    if tpl.get("needs_ai"):
        click.echo("  🤖 需要 AI 模型 (fusion-mlx)")

    # 显示工作流节点
    workflow = tpl.get("workflow", {})
    nodes = workflow.get("nodes", [])
    edges = workflow.get("edges", [])

    click.echo(f"\n  工作流: {workflow.get('name', '')}")
    click.echo(f"  📋 节点 ({len(nodes)} 个):")
    for node in nodes:
        params = node.get("config", {}).get("params", {})
        param_str = ", ".join(f"{k}={v}" for k, v in params.items() if not isinstance(v, (dict, list)))
        click.echo(f"    ├─ {node.get('id')}: {node.get('name')} ({param_str[:80]})")
    click.echo(f"  🔗 连接 ({len(edges)} 条)")
    for edge in edges:
        click.echo(f"    ├─ {edge.get('source_id')} → {edge.get('target_id')}")

    click.echo(f"\n💡 运行: fusion-cowork template run {template_id}")


@template.command("run")
@click.argument("template_id")
@click.option("--dry-run", "-n", is_flag=True, help="预览模式")
@click.option("--params", "-p", default="", help="覆盖参数 JSON")
@click.option("--resume", "session_id", default="", help="断点续跑: 指定 session_id, 跳过已完成步骤从断点续跑")
def run_template(template_id: str, dry_run: bool, params: str, session_id: str):
    """运行模板。

    --resume <session_id>: 从指定会话断点续跑 (跳过已完成节点)。
    """
    asyncio.run(_async_run_template(template_id, dry_run, params, session_id))


async def _async_run_template(template_id: str, dry_run: bool, params_json: str, resume_session_id: str = ""):
    console.print_header(f"🚀 运行模板: {template_id}")

    # 加载模板
    wf = _get_template_mgr().template_to_workflow(template_id)
    if not wf:
        console.print_error(f"模板不存在: {template_id}")
        return

    # 覆盖参数
    if params_json:
        try:
            overrides = json.loads(params_json)
            for node_id, node_params in overrides.items():
                if node_id in wf.nodes:
                    wf.nodes[node_id].config.params.update(node_params)
                    console.print_info(f"已覆盖节点 {node_id} 参数")
        except json.JSONDecodeError as e:
            console.print_error(f"参数 JSON 解析失败: {e}")
            return

    # 校验
    errors = wf.validate()
    if errors:
        console.print_error("工作流校验失败:")
        for e in errors:
            console.print_error(f"  - {e}")
        return

    # 显示工作流
    click.echo(f"\n工作流: {wf.name}")
    for nid, node in wf.nodes.items():
        console.print_info(f"  {node.icon} {node.config.label or node.display_name} ({node.name})")
    click.echo()

    if dry_run:
        console.print_success("预览模式，未实际执行")
        click.echo(f"  JSON: {wf.to_json()}")
        return

    # 断点续跑: 加载已完成步骤快照 (P1-2)
    resume_steps = None
    if resume_session_id:
        from fusion_cowork.engine.session import SessionStore

        store = SessionStore()
        payload = store.resume(resume_session_id)
        if not payload:
            console.print_error(f"恢复失败, 会话不存在: {resume_session_id}")
            return
        resume_steps = payload.get("steps_snapshot") or []
        console.print_info(f"断点续跑: 从会话 {resume_session_id} 恢复 {len(resume_steps)} 步")

    # 执行工作流
    console.print_info("正在执行...")
    execution = await _get_engine().execute(wf, resume_steps=resume_steps)

    # 输出结果
    if execution.status == WorkflowStatus.SUCCESS:
        console.print_success(f"✅ 执行成功! ({execution.total_time:.2f}s)")
    elif execution.status == WorkflowStatus.FAILED:
        console.print_error(f"❌ 执行失败: {execution.error}")
    elif execution.status == WorkflowStatus.CANCELLED:
        console.print_warning("⏹️ 已取消")
    else:
        console.print_info(f"状态: {execution.status.value}")

    # 显示步骤摘要
    click.echo()
    for step in execution.steps:
        status_icon = {
            "success": "✅",
            "failed": "❌",
            "running": "⏳",
            "pending": "⏸️",
            "skipped": "⏭️",
        }.get(step.status.value, "❓")
        click.echo(f"  {status_icon} {step.node_display_name} ({step.execution_time:.2f}s)")
        if step.error:
            click.echo(f"     └─ {step.error}")
        if step.summary:
            click.echo(f"     └─ {step.summary}")

    if execution.result_summary:
        click.echo(f"\n  📊 {execution.result_summary}")


# ── AI 命令 ──


@cli.group()
def ai():
    """AI 能力：自然语言生成流程、AI 服务管理。"""
    pass


@ai.command("generate")
@click.argument("prompt")
@click.option("--model", "-m", default="", help="fusion-mlx 模型名称")
def generate_workflow(prompt: str, model: str):
    """用自然语言描述生成自动化工作流。

    示例: fusion-cowork ai generate "帮我把桌面所有 PDF 按主题分类归档"
    """
    asyncio.run(_async_generate(prompt, model))


@ai.command("cost")
def ai_cost():
    """查看当前会话 Token 预算与成本记账。"""
    from .ai import get_budget_tracker

    budget = get_budget_tracker()
    click.echo(json.dumps(budget.to_dict(), indent=2, ensure_ascii=False))


async def _async_generate(prompt: str, model: str):
    console.print_header("🤖 AI 生成工作流")

    # 检查 fusion-mlx 是否可用
    health = await _get_mlx_client().health()
    if not health:
        console.print_warning("⚠️  fusion-mlx 未运行，将使用本地规则引擎")
        console.print_info("   启动: fusion-mlx 或 MLX 服务")
        console.print_info("   将使用模板匹配模式")

    # 生成工作流
    generator = NLWorkflowGenerator(_get_mlx_client(), model=model)
    result = await generator.generate(prompt)

    error = result.get("error")
    if error:
        console.print_error(f"生成失败: {error}")
        if result.get("raw_output"):
            click.echo(f"\n原始输出:\n{result['raw_output']}")
        return

    # 显示生成结果
    click.echo(f"\n📋 工作流: {result.get('name', '')}")
    click.echo(f"   描述: {result.get('description', '')}")
    click.echo(f"   节点 ({len(result.get('nodes', []))} 个):")

    for node in result.get("nodes", []):
        params = node.get("config", {}).get("params", {})
        param_str = ", ".join(f"{k}={v}" for k, v in params.items() if not isinstance(v, (dict, list)))
        click.echo(f"     ├─ {node.get('id')}: {node.get('name')} ({param_str[:80]})")

    for edge in result.get("edges", []):
        click.echo(f"     └─ {edge.get('source_id')} → {edge.get('target_id')}")

    click.echo("\n💡 运行: fusion-cowork workflow run <保存的工作流文件>")


@ai.command("status")
def ai_status():
    """检查 fusion-mlx 和 AI 服务状态。"""
    asyncio.run(_async_ai_status())


async def _async_ai_status():
    console.print_header("🔌 AI 服务状态")

    # 检查 fusion-mlx
    mlx_ok = await _get_mlx_client().health()
    if mlx_ok:
        console.print_success("✅ fusion-mlx: 运行中")
        try:
            models = await _get_mlx_client().list_models()
            if models:
                click.echo(f"   可用模型: {', '.join(m.get('id', m.get('model', '?')) for m in models[:5])}")
        except Exception:
            pass
    else:
        console.print_warning("⚠️  fusion-mlx: 未运行")
        click.echo("   启动: 请启动 fusion-mlx 服务 (引擎 11434, 网关 11432)")

    # 检查融合 KB
    from .ai import KBClient

    kb = KBClient()
    kb_ok = await kb.health()
    if kb_ok:
        console.print_success("✅ Fusion-KB: 运行中")
    else:
        console.print_info("ℹ️  Fusion-KB: 未运行 (可选)")
    await kb.close()


# ── 工作流命令 ──


@cli.group()
def workflow():
    """工作流管理：执行、查看、管理。"""
    pass


@workflow.command("run")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.option("--dry-run", "-n", is_flag=True, help="预览模式")
@click.option("--resume", "session_id", default="", help="断点续跑: 指定 session_id, 跳过已完成步骤从断点续跑")
def run_workflow_file(workflow_file: str, dry_run: bool, session_id: str):
    """从 JSON 文件加载并执行工作流。

    --resume <session_id>: 从指定会话断点续跑 (跳过已完成节点)。
    """
    asyncio.run(_async_run_workflow_file(workflow_file, dry_run, session_id))


async def _async_run_workflow_file(workflow_file: str, dry_run: bool, resume_session_id: str = ""):
    console.print_header(f"🚀 执行工作流: {workflow_file}")

    try:
        with open(workflow_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.print_error(f"加载工作流文件失败: {e}")
        return

    wf = Workflow.from_dict(data)

    errors = wf.validate()
    if errors:
        console.print_error("工作流校验失败:")
        for e in errors:
            console.print_error(f"  - {e}")
        return

    click.echo(f"\n工作流: {wf.name}")
    for nid, node in wf.nodes.items():
        console.print_info(f"  {node.icon} {node.config.label or node.display_name}")

    if dry_run:
        console.print_success("预览模式，未实际执行")
        return

    # 断点续跑: 加载已完成步骤快照 (P1-2)
    resume_steps = None
    if resume_session_id:
        from fusion_cowork.engine.session import SessionStore

        store = SessionStore()
        payload = store.resume(resume_session_id)
        if not payload:
            console.print_error(f"恢复失败, 会话不存在: {resume_session_id}")
            return
        resume_steps = payload.get("steps_snapshot") or []
        console.print_info(f"断点续跑: 从会话 {resume_session_id} 恢复 {len(resume_steps)} 步")

    console.print_info("正在执行...")
    execution = await _get_engine().execute(wf, resume_steps=resume_steps)

    if execution.status == WorkflowStatus.SUCCESS:
        console.print_success(f"✅ 执行成功! ({execution.total_time:.2f}s)")
    elif execution.status == WorkflowStatus.FAILED:
        console.print_error(f"❌ 执行失败: {execution.error}")

    for step in execution.steps:
        status_icon = {
            "success": "✅",
            "failed": "❌",
            "running": "⏳",
            "pending": "⏸️",
            "skipped": "⏭️",
        }.get(step.status.value, "❓")
        click.echo(f"  {status_icon} {step.node_display_name} ({step.execution_time:.2f}s)")
        if step.error:
            click.echo(f"     └─ {step.error}")
        if step.summary:
            click.echo(f"     └─ {step.summary}")

    if execution.result_summary:
        click.echo(f"\n  📊 {execution.result_summary}")


@workflow.command("list")
def list_workflows():
    """列出最近执行的工作流。"""
    executions = _get_engine().list_executions(limit=20)
    if not executions:
        console.print_info("暂无工作流执行记录")
        return

    console.print_header("最近执行记录")
    rows = []
    for exec_ in executions:
        status_icon = {
            "success": "✅",
            "failed": "❌",
            "running": "⏳",
            "pending": "⏸️",
            "cancelled": "⏹️",
            "partial": "⚠️",
        }.get(exec_.status.value, "❓")
        rows.append(
            [
                exec_.id[:12],
                exec_.workflow_name[:30],
                f"{status_icon} {exec_.status.value}",
                f"{exec_.total_time:.1f}s",
                time.strftime("%H:%M:%S", time.localtime(exec_.started_at)),
            ]
        )
    console.print_table(["ID", "名称", "状态", "耗时", "时间"], rows)


# ── 定时任务命令 ──


@cli.group()
def schedule():
    """定时任务管理：创建、列出、管理定时自动化任务。"""
    pass


@schedule.command("list")
def list_schedules():
    """列出所有定时任务。"""
    tasks = _get_scheduler().list_tasks()
    if not tasks:
        console.print_info("暂无定时任务")
        console.print_info("使用 'fusion-cowork schedule add' 添加定时任务")
        return

    console.print_header("定时任务列表")
    rows = []
    for task in tasks:
        status_icon = "✅" if task.status == TaskStatus.ACTIVE else "⏸️"
        next_run = time.strftime("%m-%d %H:%M", time.localtime(task.next_run)) if task.next_run else "-"
        rows.append(
            [
                task.id[:12],
                task.name[:25],
                f"{status_icon} {task.status.value}",
                f"{task.run_count}次",
                next_run,
            ]
        )
    console.print_table(["ID", "名称", "状态", "执行", "下次运行"], rows)


@schedule.command("add")
@click.option("--name", "-n", required=True, help="任务名称")
@click.option("--cron", "-c", required=True, help="Cron 表达式 (如: 0 21 * * *)")
@click.option("--template", "-t", required=True, help="要运行的模板 ID")
@click.option("--description", "-d", default="", help="任务描述")
def add_schedule(name: str, cron: str, template: str, description: str):
    """添加定时任务。"""
    task_id = _get_scheduler().add_cron_task(
        name=name,
        workflow_id=template,
        cron_expression=cron,
        executor=lambda: console.print_info(f"执行定时任务: {name}"),
        description=description or f"定时执行模板 {template}",
    )
    console.print_success(f"✅ 已添加定时任务: {name} ({task_id[:12]})")
    console.print_info(f"   Cron: {cron}")
    console.print_info(f"   模板: {template}")


@schedule.command("remove")
@click.argument("task_id")
def remove_schedule(task_id: str):
    """删除定时任务。"""
    if _get_scheduler().remove_task(task_id):
        console.print_success(f"已删除任务: {task_id}")
    else:
        console.print_error(f"任务不存在: {task_id}")


@schedule.command("start")
def start_scheduler():
    """启动调度器。"""
    _get_scheduler().start()
    console.print_success("✅ 调度器已启动")


@schedule.command("stop")
def stop_scheduler():
    """停止调度器。"""
    _get_scheduler().shutdown()
    console.print_info("⏹️ 调度器已停止")


# ── 系统命令 ──


@cli.group()
def system():
    """系统工具：清理、监控、信息。"""
    pass


@system.command("info")
def system_info():
    """显示系统信息。"""
    import platform

    console.print_header("🖥️  Fusion-Cowork 系统信息")
    click.echo(f"  版本: {__version__}")
    click.echo(f"  Python: {sys.version.split()[0]}")
    click.echo(f"  平台: {platform.system()} {platform.machine()}")
    click.echo(f"  Apple Silicon: {platform.machine() == 'arm64'}")
    click.echo(f"  工作目录: {os.getcwd()}")
    click.echo(f"  数据目录: {Path.home() / '.fusion-cowork'}")

    # 注册节点统计
    nodes = NodeRegistry.list()
    click.echo(f"  注册节点: {len(nodes)} 个")
    cats = {}
    for n in nodes:
        cat = n.get("category", "other")
        cats[cat] = cats.get(cat, 0) + 1
    for cat, count in cats.items():
        click.echo(f"    ├─ {cat}: {count} 个")

    # 模板统计
    templates = _get_template_mgr().list_templates()
    click.echo(f"  内置模板: {len(templates)} 个")


@system.command("scope")
@click.argument("folders", required=False)
@click.option("--enforce/--no-enforce", default=None, help="是否启用沙箱拦截")
def system_scope(folders, enforce):
    """查看或设置授权工作文件夹 (Scoped Folder) 沙箱。

    \b
    fusion-cowork system scope                          # 查看当前配置
    fusion-cowork system scope ~/Cowork_Workspace       # 设置授权文件夹
    fusion-cowork system scope ~/a,~/b --enforce        # 多文件夹 + 启用拦截
    """
    from .config_center import ConfigCenter
    from .security import get_scoped_folder_manager, reset_scoped_folder_manager

    cc = ConfigCenter.get_instance()
    if folders is None and enforce is None:
        cur = cc.get("workspace.scoped_folder", "")
        enf = cc.get("workspace.enforce_scope", False)
        console.print_info(f"授权文件夹: {cur or '(未设置)'}")
        console.print_info(f"沙箱拦截: {'启用' if enf else '关闭'}")
        return
    if folders is not None:
        cc.set("workspace.scoped_folder", folders)
        console.print_result(f"授权文件夹已设置: {folders}")
    if enforce is not None:
        cc.set("workspace.enforce_scope", enforce)
        console.print_result(f"沙箱拦截已{'启用' if enforce else '关闭'}")
    reset_scoped_folder_manager()
    mgr = get_scoped_folder_manager()
    console.print_info(f"生效: enforce={mgr.enforce} scopes={[str(s) for s in mgr.scopes]}")


@system.command("clean")
@click.option("--dry-run", "-n", is_flag=True, default=True, help="预览模式")
@click.option("--force", "-f", is_flag=True, help="强制清理")
def system_clean(dry_run: bool, force: bool):
    """快速清理桌面和下载文件夹。"""
    if force:
        dry_run = False
    asyncio.run(_async_system_clean(dry_run))


async def _async_system_clean(dry_run: bool):
    console.print_header(f"🧹 系统清理 ({'预览' if dry_run else '执行'})")

    # 桌面清理
    from .nodes.macos import DesktopCleanNode

    desktop = DesktopCleanNode(
        config=type(
            "obj",
            (object,),
            {
                "params": {
                    "organize_by_type": True,
                    "skip_hidden": True,
                    "dry_run": dry_run,
                }
            },
        )()
    )
    result = await desktop.execute({})
    console.print_info(f"  桌面: {result.summary}")

    # 下载整理
    from .nodes.macos import DownloadOrganizerNode

    download = DownloadOrganizerNode(
        config=type(
            "obj",
            (object,),
            {
                "params": {
                    "organize_by_type": True,
                    "deduplicate": True,
                    "dry_run": dry_run,
                }
            },
        )()
    )
    result = await download.execute({})
    console.print_info(f"  下载: {result.summary}")

    if not dry_run:
        console.print_success("✅ 清理完成")
    else:
        console.print_info("💡 使用 --force 或 -f 执行实际清理")


# ── 浏览器命令 ──


@cli.group()
def browser():
    """内嵌浏览器管理：启动、打开、自动化。"""
    pass


@browser.command("start")
@click.option("--build", "-b", is_flag=True, help="先构建再启动")
def browser_start(build: bool):
    """启动 Fusion 内嵌浏览器。"""
    asyncio.run(_async_browser_start(build))


async def _async_browser_start(build: bool):
    from .nodes.browser import BrowserManager

    console.print_header("🌐 Fusion 内嵌浏览器")

    if build:
        console.print_info("正在构建...")
        if BrowserManager.build():
            console.print_success("构建成功")
        else:
            console.print_error("构建失败")
            return

    if BrowserManager.launch():
        console.print_success("✅ 浏览器已启动")
        console.print_info("   支持 fusion:// 私有协议:")
        console.print_info("   ├─ fusion://start/ — 起始页")
        console.print_info("   ├─ fusion://kb/ — 知识库管理")
        console.print_info("   ├─ fusion://model/ — 模型管理")
        console.print_info("   └─ fusion://automation/ — 自动化工作流")
    else:
        console.print_error("启动失败，请先构建: fusion-cowork browser build")


@browser.command("build")
def browser_build():
    """构建 Fusion 内嵌浏览器。"""
    asyncio.run(_async_browser_build())


async def _async_browser_build():
    from .nodes.browser import BrowserManager

    console.print_header("🔨 构建 Fusion 内嵌浏览器")
    console.print_info("正在编译 Swift 原生浏览器...")
    if BrowserManager.build():
        console.print_success("✅ 构建成功")
    else:
        console.print_error("❌ 构建失败")


@browser.command("open")
@click.argument("url")
def browser_open(url: str):
    """在浏览器中打开 URL。"""
    asyncio.run(_async_browser_open(url))


async def _async_browser_open(url: str):
    from .nodes.browser import BrowserClient

    client = BrowserClient()
    try:
        _result = await client.open_url(url)
        console.print_success(f"已打开: {url}")
    except Exception as e:
        console.print_error(f"打开失败: {e}")
        console.print_info("请先启动浏览器: fusion-cowork browser start")
    finally:
        await client.close()


@browser.command("status")
def browser_status():
    """检查浏览器状态。"""
    from .nodes.browser import BrowserClient

    client = BrowserClient()
    if client.is_running():
        console.print_success("✅ 浏览器正在运行")
    else:
        console.print_warning("⚠️ 浏览器未运行")
        console.print_info("启动: fusion-cowork browser start")


@browser.command("extract")
@click.argument("url", required=False)
@click.option("--to-file", "-o", default="", help="保存到文件")
def browser_extract(url: str = "", to_file: str = ""):
    """提取网页文本内容。"""
    asyncio.run(_async_browser_extract(url, to_file))


async def _async_browser_extract(url: str, to_file: str):
    from .engine import NodeConfig, NodeStatus
    from .nodes.browser import BrowserExtractNode

    node = BrowserExtractNode(config=NodeConfig(params={"url": url} if url else {}))
    result = await node.execute({"url": url} if url else {})

    if result.status == NodeStatus.SUCCESS:
        text = result.data.get("text", "")
        length = result.data.get("text_length", 0)
        console.print_success(f"提取完成: {length} 字符")
        if to_file:
            Path(to_file).write_text(text, encoding="utf-8")
            console.print_info(f"已保存到: {to_file}")
        else:
            click.echo()
            click.echo(text[:2000])
            if len(text) > 2000:
                click.echo(f"\n... (共 {len(text)} 字符，使用 --to-file 保存完整内容)")
    else:
        console.print_error(f"提取失败: {result.error}")


# ── MCP 服务 ──


@cli.group("mcp")
def mcp():
    """MCP 服务管理 — 对接 Claude Desktop/Code。"""
    pass


@mcp.command("serve")
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "http", "streamable"]),
    default="stdio",
    help="传输模式 (stdio/http/streamable)",
)
@click.option("--host", "-h", default="127.0.0.1", help="HTTP 监听地址")
@click.option("--port", "-p", default=11438, type=int, help="HTTP 监听端口")
def mcp_serve(transport: str, host: str, port: int):
    """启动 MCP 服务 — 供 Claude Code / Claude Desktop 调用。"""
    from .server.mcp_server import MCPServer

    rt = _build_runtime()
    server = MCPServer(
        host=host,
        port=port,
        permission_manager=rt["permission_manager"],
        hook_manager=rt["hook_manager"],
    )
    emitter = rt["event_emitter"]
    if transport == "stdio":
        console.print_info("MCP 服务启动 (stdio 模式) — 等待 Claude Code 连接...")
        asyncio.run(server.serve_stdio())
    elif transport == "streamable":
        console.print_info(f"MCP 服务启动 (Streamable HTTP 2025-03-26 模式) — {host}:{port}")
        asyncio.run(server.serve_streamable_http(event_emitter=emitter))
    else:
        console.print_info(f"MCP 服务启动 (HTTP 模式) — {host}:{port}")
        asyncio.run(server.serve_http(event_emitter=emitter))


# ── Desk RPC 服务 ──


@cli.group("desk")
def desk():
    """Desk RPC 服务管理 — 对接 Fusion-Studio GUI。"""
    pass


@desk.command("rpc")
@click.option("--sock", "-s", default="/tmp/fusion-cowork.sock", help="UDS 路径")
@click.option("--http-port", default=11438, show_default=True, help="HTTP 端口 (/rpc /health /mcp /sse); 0=不启动 HTTP")
def desk_rpc(sock: str, http_port: int):
    """启动 Desk RPC 服务 — 供 Fusion-Studio 调用。

    默认同时启动 UDS (desk.*) + HTTP (/rpc /health /mcp /sse) 双通道。
    HTTP 通道承载 fusion-studio PluginBridge 的 plugins/* 集成面板 (issue #48)。
    --http-port 0 可禁用 HTTP (仅 UDS)。
    """
    from .server.desk_rpc import DeskRPCServer

    rt = _build_runtime()
    rpc = DeskRPCServer(
        sock_path=sock,
        event_emitter=rt["event_emitter"],
        session_store=rt["session_store"],
        permission_manager=rt["permission_manager"],
        hook_manager=rt["hook_manager"],
        space_store=rt["space_store"],
    )

    async def _run():
        import signal

        await rpc.start()
        console.print_success(f"Desk RPC 服务已启动 (UDS): {sock}")

        http_task = None
        if http_port > 0:
            http_task = asyncio.create_task(_serve_http(http_port))

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _request_stop():
            console.print_info("收到停止信号, 开始优雅关停...")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except (NotImplementedError, RuntimeError):
                pass

        console.print_info("等待 Fusion-Studio 连接... (Ctrl+C 停止)")
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            if http_task and not http_task.done():
                http_task.cancel()
                try:
                    await http_task
                except (asyncio.CancelledError, Exception):
                    pass
            await rpc.stop()
            console.print_info("Desk RPC 服务已停止")

    async def _serve_http(port: int):
        try:
            from .nodes import import_all_nodes
            from .server.mcp_server import MCPServer

            import_all_nodes()
            rt = _build_runtime()
            bind_host = os.environ.get("FUSION_BIND_HOST", "127.0.0.1")
            mcp = MCPServer(
                host=bind_host,
                port=port,
                permission_manager=rt["permission_manager"],
                hook_manager=rt["hook_manager"],
            )
            console.print_success(f"Desk HTTP 服务已启动: {bind_host}:{port} (/rpc /health /mcp /sse)")
            await mcp.serve_http(event_emitter=rt["event_emitter"])
        except ImportError:
            console.print_warning("HTTP 通道未启动 (缺 [web] 依赖): pip install fusion-cowork[web]; 仅 UDS 可用")
        except Exception as e:
            console.print_warning(f"HTTP 通道启动失败 ({e}); 仅 UDS 可用")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print_info("Desk RPC 服务已停止")


# ── Session 会话管理 ──


@cli.group("session")
def session():
    """会话管理 — 查询/恢复/分叉工作流执行会话。"""
    pass


@session.command("list")
@click.option("--status", "-s", default=None, help="按状态过滤 (created/running/completed/failed)")
@click.option("--limit", "-n", default=20, help="返回数量")
def session_list(status, limit):
    """列出会话。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    sessions = store.list_sessions(status=status, limit=limit)
    if not sessions:
        console.print_info("没有会话记录")
        return
    for s in sessions:
        console.print_result(f"{s.id}  {s.status:10s}  {s.workflow_name:20s}  {s.created_at:.0f}")


@session.command("show")
@click.argument("session_id")
def session_show(session_id):
    """查看会话详情。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    s = store.get(session_id)
    if not s:
        console.print_error(f"会话不存在: {session_id}")
        return
    console.print_result(json.dumps(store.to_dict(s), indent=2, ensure_ascii=False))


@session.command("resumable")
@click.option("--limit", "-n", default=20, help="返回数量")
def session_resumable(limit):
    """列出可恢复的会话 (paused/failed/running)。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    sessions = store.list_resumable(limit=limit)
    if not sessions:
        console.print_info("没有可恢复的会话")
        return
    for s in sessions:
        console.print_result(f"{s.id}  {s.status:10s}  {s.workflow_name:20s}  steps={len(s.steps_snapshot)}")


@session.command("resume")
@click.argument("session_id")
def session_resume(session_id):
    """恢复会话 — 加载快照供重放/续跑。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    payload = store.resume(session_id)
    if not payload:
        console.print_error(f"恢复失败: {session_id}")
        return
    console.print_result(json.dumps(payload, indent=2, ensure_ascii=False))


@session.command("fork")
@click.argument("session_id")
@click.option("--from-step", "-f", default=0, type=int, help="从第几步开始分叉 (0=全部)")
def session_fork(session_id, from_step):
    """分叉会话。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    forked = store.fork(session_id, from_step=from_step)
    if not forked:
        console.print_error(f"分叉失败: {session_id}")
        return
    console.print_result(f"已分叉: {forked.id} (from {session_id}, step={from_step})")


@session.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="确认删除该会话?")
def session_delete(session_id):
    """删除会话。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    if store.delete(session_id):
        console.print_result(f"已删除: {session_id}")
    else:
        console.print_error(f"删除失败: {session_id}")


@session.command("cleanup")
@click.option("--days", "-d", default=30, type=int, help="清理多少天前的过期会话")
def session_cleanup(days):
    """清理过期会话。"""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    count = store.cleanup_expired(expire_days=days)
    console.print_result(f"已清理 {count} 条过期会话")


# ── Permission 权限管理 ──


@cli.group("permission")
def permission():
    """权限管理 — 查看/审批/拒绝工具权限。"""
    pass


@permission.command("level")
@click.argument("level", type=click.Choice(["manual", "auto", "plan", "bypass"]))
def permission_level(level):
    """设置权限级别。"""
    from fusion_cowork.engine.permission import PermissionLevel, PermissionManager

    pm = PermissionManager()
    pm.level = PermissionLevel(level)
    pm.save()
    console.print_result(f"权限级别已设为: {level}")


@permission.command("approve")
@click.argument("tool_name")
@click.option("--scope", "-s", default="*", help="权限范围 (如 'command:git *')")
def permission_approve(tool_name, scope):
    """批准工具权限。"""
    from fusion_cowork.engine.permission import PermissionManager

    pm = PermissionManager()
    pm.load()
    pm.approve(tool_name, scope=scope)
    pm.save()
    console.print_result(f"已批准: {tool_name} (scope={scope})")


@permission.command("deny")
@click.argument("tool_name")
@click.option("--scope", "-s", default="*", help="权限范围")
def permission_deny(tool_name, scope):
    """拒绝工具权限。"""
    from fusion_cowork.engine.permission import PermissionManager

    pm = PermissionManager()
    pm.load()
    pm.deny(tool_name, scope=scope)
    pm.save()
    console.print_result(f"已拒绝: {tool_name} (scope={scope})")


@permission.command("list")
def permission_list():
    """列出权限规则。"""
    from fusion_cowork.engine.permission import PermissionManager

    pm = PermissionManager()
    pm.load()
    data = pm.to_dict()
    console.print_result(json.dumps(data, indent=2, ensure_ascii=False))


# ── 插件管理命令 ──


@cli.group("plugin")
def plugin():
    """插件管理 — 发现/安装/加载/卸载插件。"""
    pass


@plugin.command("list")
def plugin_list():
    """列出已发现和已加载的插件。"""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    discovered = loader.discover()
    if not discovered:
        console.print_info("没有发现插件")
        console.print_info("插件目录: ~/.fusion-cowork/plugins/")
        return
    console.print_header(f"插件列表 ({len(discovered)} 个)")
    rows = []
    for name, manifest in discovered.items():
        loaded = "✅" if name in loader._loaded else "⏸️"
        nodes = ", ".join(manifest.nodes[:3])
        if len(manifest.nodes) > 3:
            nodes += f" +{len(manifest.nodes) - 3}"
        rows.append([name, manifest.version, loaded, manifest.author or "-", nodes])
    console.print_table(["名称", "版本", "状态", "作者", "节点"], rows)


@plugin.command("install")
@click.argument("path")
def plugin_install(path: str):
    """从目录、zip 或 URL (http(s)://...zip) 安装插件。"""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        ok = loader.install(path)
        if ok:
            console.print_success(f"已安装插件: {path}")
        else:
            console.print_error(f"安装失败: {path}")
    except Exception as e:
        console.print_error(f"安装失败: {e}")


@plugin.command("uninstall")
@click.argument("name")
@click.confirmation_option(prompt="确认卸载该插件?")
def plugin_uninstall(name: str):
    """卸载插件。"""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        loader.uninstall(name)
        console.print_success(f"已卸载插件: {name}")
    except Exception as e:
        console.print_error(f"卸载失败: {e}")


@plugin.command("import-claude-desktop")
@click.option(
    "--config",
    "config_path",
    default="",
    help="claude_desktop_config.json 路径 (默认 ~/Library/Application Support/Claude/)",
)
def plugin_import_claude_desktop(config_path: str):
    """从 Claude Desktop 配置导入 MCP server 为插件。"""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        imported = loader.import_from_claude_desktop(config_path)
        if imported:
            console.print_success(f"已从 Claude Desktop 导入 {len(imported)} 个 MCP server:")
            for name in imported:
                console.print_info(f"  - {name}")
        else:
            console.print_warning("未发现可导入的 MCP server (配置不存在或 mcpServers 为空)")
    except Exception as e:
        console.print_error(f"导入失败: {e}")


@plugin.command("load")
@click.argument("name")
def plugin_load(name: str):
    """加载插件（注册节点）。"""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        loader.load(name)
        console.print_success(f"已加载插件: {name}")
    except Exception as e:
        console.print_error(f"加载失败: {e}")


@plugin.command("unload")
@click.argument("name")
def plugin_unload(name: str):
    """卸载插件（取消注册节点）。"""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        loader.unload(name)
        console.print_success(f"已卸载插件节点: {name}")
    except Exception as e:
        console.print_error(f"卸载失败: {e}")


# ── 技能命令 ──


@cli.group("skill")
def skill():
    """技能管理 — 列出/搜索/执行技能。"""
    pass


def _get_skill_registry():
    from .skills import SkillRegistry, register_builtin_skills, register_user_packs

    registry = SkillRegistry()
    register_builtin_skills(registry)
    try:
        register_user_packs(registry)
    except Exception as e:
        console.print_warning(f"用户 skill 包加载失败: {e}")
    return registry


@skill.command("list")
def skill_list():
    """列出可用技能。"""
    registry = _get_skill_registry()
    skills = registry.list_skills()
    if not skills:
        console.print_info("没有可用技能")
        return
    console.print_header(f"技能列表 ({len(skills)} 个)")
    rows = []
    for s in skills:
        aliases = ", ".join(s.aliases) if s.aliases else "-"
        rows.append([s.name, s.description[:40], s.category or "-", aliases])
    console.print_table(["名称", "描述", "分类", "别名"], rows)


@skill.command("run")
@click.argument("name")
@click.option("--params", "-p", default="{}", help="参数 JSON")
def skill_run(name: str, params: str):
    """执行技能。"""
    registry = _get_skill_registry()
    try:
        kwargs = json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        console.print_error(f"参数 JSON 解析失败: {e}")
        return
    try:
        result = asyncio.run(registry.execute(name, **kwargs))
        console.print_success(f"技能执行成功: {name}")
        if result:
            click.echo(
                json.dumps(result, indent=2, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
            )
    except Exception as e:
        console.print_error(f"技能执行失败: {e}")


@skill.command("search")
@click.argument("query")
def skill_search(query: str):
    """搜索技能。"""
    registry = _get_skill_registry()
    results = registry.search(query)
    if not results:
        console.print_info(f"未找到匹配 '{query}' 的技能")
        return
    for s in results:
        click.echo(f"  {s.name}: {s.description}")


@skill.command("create")
@click.option("--name", required=True, help="技能名 (如 /my-skill)")
@click.option("--description", "-d", default="", help="技能描述")
@click.option("--category", "-c", default="custom", help="分类")
@click.option("--type", "stype", type=click.Choice(["workflow", "node", "script"]), default="node", help="handler 类型")
@click.option("--template-id", default="", help="workflow 类型: 模板 id")
@click.option("--node", default="", help="node 类型: 节点名")
@click.option("--params", default="{}", help="node 类型: 参数 JSON")
@click.option("--script", default="", help="script 类型: 脚本文件名 (相对包目录)")
@click.option("--aliases", default="", help="别名, 逗号分隔")
def skill_create(name, description, category, stype, template_id, node, params, script, aliases):
    """创建用户自写 skill 包 (持久化到 ~/.fusion-cowork/skills)。"""
    from .skills import SkillPack, save_skill_pack

    try:
        params_dict = json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        console.print_error(f"参数 JSON 解析失败: {e}")
        return
    pack = SkillPack(
        name=name,
        description=description,
        category=category,
        type=stype,
        template_id=template_id,
        node=node,
        params=params_dict,
        script=script,
        aliases=[a.strip() for a in aliases.split(",") if a.strip()],
    )
    try:
        pdir = save_skill_pack(pack)
        console.print_success(f"skill 包已创建: {name} -> {pdir}")
    except Exception as e:
        console.print_error(f"skill 包创建失败: {e}")


@skill.command("list-packs")
def skill_list_packs():
    """列出磁盘上的用户 skill 包。"""
    from .skills import list_skill_packs

    packs = list_skill_packs()
    if not packs:
        console.print_info("没有用户 skill 包")
        return
    console.print_header(f"用户 skill 包 ({len(packs)} 个)")
    rows = []
    for p in packs:
        rows.append([p.name, p.type, p.description[:40], ", ".join(p.aliases) or "-"])
    console.print_table(["名称", "类型", "描述", "别名"], rows)


@skill.command("delete-pack")
@click.argument("name")
def skill_delete_pack(name: str):
    """删除用户 skill 包。"""
    from .skills import delete_skill_pack

    if delete_skill_pack(name):
        console.print_success(f"skill 包已删除: {name}")
    else:
        console.print_error(f"skill 包不存在: {name}")


# ── CDP 命令 ──


@cli.group("cdp")
def cdp():
    """Chrome DevTools Protocol — 远程控制 Chrome 浏览器。"""
    pass


@cdp.command("navigate")
@click.argument("url")
@click.option("--host", default="127.0.0.1", help="Chrome 远程调试地址")
@click.option("--port", default=9222, type=int, help="Chrome 远程调试端口")
def cdp_navigate(url: str, host: str, port: int):
    """导航到指定 URL。"""
    from .engine import NodeConfig
    from .nodes.browser import CDPNavigateNode

    node = CDPNavigateNode(config=NodeConfig(params={"url": url, "host": host, "port": port}))
    result = asyncio.run(node.execute({"url": url}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.summary)


@cdp.command("snapshot")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_snapshot(host: str, port: int):
    """获取页面 a11y 快照。"""
    from .engine import NodeConfig
    from .nodes.browser import CDPSnapshotNode

    node = CDPSnapshotNode(config=NodeConfig(params={"host": host, "port": port}))
    result = asyncio.run(node.execute({}))
    if result.status.value == "success":
        console.print_success(result.summary)
        if result.data.get("tree"):
            click.echo(json.dumps(result.data["tree"], indent=2, ensure_ascii=False)[:3000])
    else:
        console.print_error(result.summary)


@cdp.command("click")
@click.argument("backend_node_id", type=int)
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_click(backend_node_id: int, host: str, port: int):
    """点击页面元素 (backendNodeId)。"""
    from .engine import NodeConfig
    from .nodes.browser import CDPClickNode

    node = CDPClickNode(config=NodeConfig(params={"backend_node_id": backend_node_id, "host": host, "port": port}))
    result = asyncio.run(node.execute({"backend_node_id": backend_node_id}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.summary)


@cdp.command("fill")
@click.option("--selector", "-s", required=True, help="CSS 选择器")
@click.option("--value", "-v", required=True, help="填写值")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_fill(selector: str, value: str, host: str, port: int):
    """填写表单字段。"""
    from .engine import NodeConfig
    from .nodes.browser import CDPFillNode

    node = CDPFillNode(config=NodeConfig(params={"selector": selector, "value": value, "host": host, "port": port}))
    result = asyncio.run(node.execute({"selector": selector, "value": value}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.summary)


@cdp.command("screenshot")
@click.option("--save", "-o", default="", help="保存路径")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_screenshot(save: str, host: str, port: int):
    """截取页面截图。"""
    from .engine import NodeConfig
    from .nodes.browser import CDPScreenshotNode

    node = CDPScreenshotNode(config=NodeConfig(params={"save_path": save, "host": host, "port": port}))
    result = asyncio.run(node.execute({"save_path": save}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.summary)


@cdp.command("evaluate")
@click.argument("script")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_evaluate(script: str, host: str, port: int):
    """在页面中执行 JavaScript。"""
    from .engine import NodeConfig
    from .nodes.browser import CDPEvaluateNode

    node = CDPEvaluateNode(config=NodeConfig(params={"script": script, "host": host, "port": port}))
    result = asyncio.run(node.execute({"script": script}))
    if result.status.value == "success":
        console.print_success(result.summary)
        if result.data.get("result") is not None:
            click.echo(json.dumps(result.data["result"], indent=2, ensure_ascii=False)[:2000])
    else:
        console.print_error(result.summary)


# ── Computer Use 命令 ──


@cli.group("computer-use")
def computer_use():
    """Computer Use — 鼠标键盘控制 + AI 闭环。"""
    pass


@computer_use.command("move")
@click.argument("x", type=int)
@click.argument("y", type=int)
@click.option("--duration", "-d", default=0, type=float, help="移动持续时间 (秒)")
def cu_move(x: int, y: int, duration: float):
    """移动鼠标到指定坐标。"""
    from .nodes.macos.input_nodes import MouseMoveNode

    node = MouseMoveNode(config=NodeConfig(params={"x": x, "y": y, "duration": duration}))
    result = asyncio.run(node.execute({"x": x, "y": y}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("click")
@click.option("--x", type=int, default=None, help="X 坐标")
@click.option("--y", type=int, default=None, help="Y 坐标")
@click.option("--button", "-b", type=click.Choice(["left", "right", "middle"]), default="left")
@click.option("--count", "-c", type=int, default=1, help="点击次数")
def cu_click(x, y, button, count):
    """鼠标点击。"""
    from .nodes.macos.input_nodes import MouseClickNode

    params = {"button": button, "click_count": count}
    if x is not None and y is not None:
        params["x"] = x
        params["y"] = y
    node = MouseClickNode(config=NodeConfig(params=params))
    result = asyncio.run(node.execute(params))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("type")
@click.argument("text")
@click.option("--delay", "-d", default=0, type=float, help="字符间延迟 (秒)")
def cu_type(text: str, delay: float):
    """键盘输入文本。"""
    from .nodes.macos.input_nodes import KeyboardTypeNode

    node = KeyboardTypeNode(config=NodeConfig(params={"text": text, "delay": delay}))
    result = asyncio.run(node.execute({"text": text}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("shortcut")
@click.argument("key")
@click.option("--modifiers", "-m", multiple=True, help="修饰键 (cmd/shift/ctrl/alt)")
def cu_shortcut(key: str, modifiers):
    """键盘快捷键。"""
    from .nodes.macos.input_nodes import KeyboardShortcutNode

    node = KeyboardShortcutNode(config=NodeConfig(params={"key": key, "modifiers": list(modifiers)}))
    result = asyncio.run(node.execute({"key": key}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("run")
@click.argument("task")
@click.option("--max-steps", default=10, type=int, help="最大循环步数")
@click.option("--step-delay", default=1.0, type=float, help="每步间隔 (秒)")
@click.option("--model", default="default", help="fusion-mlx 模型名")
def cu_run(task: str, max_steps: int, step_delay: float, model: str):
    """执行 Computer Use 闭环任务。"""
    from .nodes.macos.input_nodes import ComputerUseLoopNode

    node = ComputerUseLoopNode(
        config=NodeConfig(
            params={
                "task": task,
                "max_steps": max_steps,
                "step_delay": step_delay,
                "model": model,
            }
        )
    )
    result = asyncio.run(node.execute({"task": task}))
    if result.status.value == "success":
        console.print_success(result.summary)
        if result.data and result.data.get("actions"):
            for a in result.data["actions"]:
                click.echo(f"  step {a.get('step', '?')}: {a.get('action', '?')}")
    else:
        console.print_error(result.error or result.summary)


# ── 远程控制命令 ──


@cli.group("remote")
def remote():
    """远程控制 — WebSocket 接入 fusion-cowork 会话。"""
    pass


@remote.command("serve")
@click.option("--host", "-h", default="127.0.0.1", help="监听地址")
@click.option("--port", "-p", default=11439, type=int, help="监听端口")
@click.option("--token", "-t", default="", help="认证令牌 (空则无认证)")
@click.option("--tls-cert", default="", help="TLS 证书路径 (PEM), 启用 wss://")
@click.option("--tls-key", default="", help="TLS 私钥路径 (PEM)")
def remote_serve(host: str, port: int, token: str, tls_cert: str, tls_key: str):
    """启动远程控制服务 (可选 TLS)。"""
    from .server.remote import RemoteControlServer

    server = RemoteControlServer(host=host, port=port, token=token or None, tls_cert=tls_cert, tls_key=tls_key)

    async def _run():
        await server.start()
        scheme = "wss" if (tls_cert and tls_key) else "ws"
        console.print_success(f"远程控制服务已启动: {scheme}://{host}:{port}/control")
        if token:
            _t = str(token)
            _shown = f"{_t[:4]}***{_t[-4:]}" if len(_t) > 8 else "(已设置)"
            console.print_info(f"认证令牌: {_shown}")
        if tls_cert and tls_key:
            console.print_info(f"TLS 已启用: cert={tls_cert}")
        console.print_info("等待远程连接... (Ctrl+C 停止)")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print_info("远程控制服务已停止")


@remote.command("connect")
@click.argument("url", default="ws://127.0.0.1:11439/control")
@click.option("--token", "-t", default="", help="认证令牌")
def remote_connect(url: str, token: str):
    """连接到远程 fusion-cowork 实例。"""
    from .server.remote import RemoteControlClient

    client = RemoteControlClient(token=token or None)

    async def _run():
        await client.connect(url)
        console.print_success(f"已连接: {url}")
        try:
            status = await client.get_status()
            click.echo(json.dumps(status, indent=2, ensure_ascii=False))
        finally:
            await client.close()

    try:
        asyncio.run(_run())
    except Exception as e:
        console.print_error(f"连接失败: {e}")


@remote.command("submit")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.option("--url", default="ws://127.0.0.1:11439/control", help="远程服务地址")
@click.option("--token", "-t", default="", help="认证令牌")
def remote_submit(workflow_file: str, url: str, token: str):
    """提交工作流到远程 fusion-cowork 执行。"""
    from .server.remote import RemoteControlClient

    client = RemoteControlClient(token=token or None)

    async def _run():
        await client.connect(url)
        try:
            with open(workflow_file, encoding="utf-8") as f:
                workflow = json.load(f)
            task_id = await client.submit_workflow(workflow)
            console.print_success(f"已提交工作流: {task_id}")
            click.echo(f"任务 ID: {task_id}")
        finally:
            await client.close()

    try:
        asyncio.run(_run())
    except Exception as e:
        console.print_error(f"提交失败: {e}")


# ── Worktree git 隔离命令 ──


@cli.group("worktree")
def worktree():
    """Worktree — git 工作树隔离执行。"""
    pass


@worktree.command("create")
@click.argument("name")
@click.option("--branch", "-b", default="", help="checkout 已有分支 (空则新建 worktree/<name>)")
@click.option("--repo-root", "-r", default="", help="git 仓库根 (空则自动检测)")
def worktree_create(name: str, branch: str, repo_root: str):
    """创建隔离工作树。"""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    wt = mgr.create(name, branch=branch)
    if not wt:
        console.print_error(f"worktree 创建失败: {name}")
        return
    console.print_success(f"worktree 已创建: {name}")
    click.echo(json.dumps(wt.to_dict(), indent=2, ensure_ascii=False))


@worktree.command("list")
@click.option("--repo-root", "-r", default="", help="git 仓库根 (空则自动检测)")
def worktree_list(repo_root: str):
    """列出所有工作树。"""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    wts = mgr.list_worktrees()
    console.print_header(f"Worktree 列表 ({len(wts)} 个)")
    rows = [[w.path, w.branch, w.head[:8]] for w in wts]
    console.print_table(["路径", "分支", "HEAD"], rows)


@worktree.command("exec")
@click.argument("name")
@click.argument("command")
@click.option("--repo-root", "-r", default="", help="git 仓库根 (空则自动检测)")
@click.option("--timeout", "-t", default=30, type=int, help="超时 (秒)")
def worktree_exec(name: str, command: str, repo_root: str, timeout: int):
    """在隔离工作树内执行命令。"""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    # 按名称匹配登记现有 worktree
    if not mgr.get(name):
        for w in mgr.list_worktrees():
            if Path(w.path).name == name:
                mgr._worktrees[name] = w
                break

    async def _run():
        return await mgr.exec_in(name, command, timeout=timeout)

    try:
        result = asyncio.run(_run())
        if "error" in result:
            console.print_error(result["error"])
            return
        console.print_success(f"rc={result.get('return_code')} @ {result.get('path')}")
        if result.get("stdout"):
            click.echo(result["stdout"], nl=False)
        if result.get("stderr"):
            click.echo(result["stderr"], nl=False, err=True)
    except Exception as e:
        console.print_error(f"执行失败: {e}")


@worktree.command("remove")
@click.argument("name")
@click.option("--repo-root", "-r", default="", help="git 仓库根 (空则自动检测)")
@click.option("--force", "-f", is_flag=True, help="强制删除")
def worktree_remove(name: str, repo_root: str, force: bool):
    """删除隔离工作树。"""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    if not mgr.get(name):
        for w in mgr.list_worktrees():
            if Path(w.path).name == name:
                mgr._worktrees[name] = w
                break
    if mgr.remove(name, force=force):
        console.print_success(f"worktree 已删除: {name}")
    else:
        console.print_error(f"worktree 删除失败: {name}")


# ── LSP 代码智能命令 ──


@cli.group("lsp")
def lsp():
    """LSP — 代码智能 (定义/引用/悬停/补全)。"""
    pass


def _lsp_query_common(action: str, path: str, line: int, character: int, root: str):
    from .code import query as lsp_query

    try:
        result = asyncio.run(lsp_query(action, path, line, character, root=root))
    except Exception as e:
        console.print_error(f"LSP 查询失败: {e}")
        return
    if "error" in result:
        console.print_error(result["error"])
        return
    console.print_success(f"LSP {action} 结果:")
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@lsp.command("definition")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace 根 (空则取文件目录)")
def lsp_definition(path: str, line: int, character: int, root: str):
    """查询定义位置。"""
    _lsp_query_common("definition", path, line, character, root)


@lsp.command("references")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace 根 (空则取文件目录)")
def lsp_references(path: str, line: int, character: int, root: str):
    """查询引用位置。"""
    _lsp_query_common("references", path, line, character, root)


@lsp.command("hover")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace 根 (空则取文件目录)")
def lsp_hover(path: str, line: int, character: int, root: str):
    """查询悬停信息。"""
    _lsp_query_common("hover", path, line, character, root)


@lsp.command("completion")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace 根 (空则取文件目录)")
def lsp_completion(path: str, line: int, character: int, root: str):
    """查询补全项。"""
    _lsp_query_common("completion", path, line, character, root)


# ── 移动推送命令 ──


@cli.group("push")
def push():
    """移动推送 — Bark/ntfy 跨平台通知 (未配置降级本地)。"""
    pass


@push.command("send")
@click.argument("title")
@click.argument("message")
@click.option("--provider", "-p", type=click.Choice(["auto", "bark", "ntfy", "local"]), default="auto", help="推送渠道")
@click.option("--url", "-u", default="", help="Bark/ntfy server URL")
@click.option("--token", "-t", default="", help="Bark device key / ntfy topic")
@click.option("--sound", default="", help="提示音 (Bark)")
@click.option("--priority", default="", help="优先级 (ntfy 1-5)")
@click.option("--group", default="", help="分组")
def push_send(title: str, message: str, provider: str, url: str, token: str, sound: str, priority: str, group: str):
    """发送移动推送通知。"""
    from .notification import push as do_push

    result = asyncio.run(
        do_push(title, message, provider=provider, url=url, token=token, sound=sound, priority=priority, group=group)
    )
    if result.success:
        tag = " (降级本地)" if result.degraded else ""
        console.print_success(f"✅ 推送成功 via {result.provider}{tag}")
    else:
        console.print_error(f"❌ 推送失败: {result.error}")
    click.echo(
        json.dumps(
            {
                "success": result.success,
                "provider": result.provider,
                "degraded": result.degraded,
                "response": result.response,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@push.command("config")
@click.option("--bark-url", default=None, help="Bark server URL (如 https://api.day.app)")
@click.option("--ntfy-url", default=None, help="ntfy server URL (如 https://ntfy.sh)")
@click.option("--ntfy-token", default=None, help="ntfy topic / token")
@click.option("--sound", default=None, help="默认提示音")
@click.option("--priority", default=None, help="默认优先级")
def push_config(bark_url, ntfy_url, ntfy_token, sound, priority):
    """查看或设置推送渠道配置 (持久化到 ConfigCenter)。"""
    from .config_center import ConfigCenter

    cc = ConfigCenter.get_instance()
    if all(v is None for v in (bark_url, ntfy_url, ntfy_token, sound, priority)):
        console.print_info(f"Bark URL: {cc.get('push.bark_url', '(未设置)')}")
        console.print_info(f"ntfy URL: {cc.get('push.ntfy_url', '(未设置)')}")
        # Stage 2: token 脱敏显示 (仅前4后4, 中间 *)
        _ntfy = cc.get("push.ntfy_token", "") or ""
        _shown = f"{_ntfy[:4]}***{_ntfy[-4:]}" if len(_ntfy) > 8 else ("(已设置)" if _ntfy else "(未设置)")
        console.print_info(f"ntfy Token: {_shown}")
        console.print_info(f"提示音: {cc.get('push.sound', '(未设置)')}")
        console.print_info(f"优先级: {cc.get('push.priority', '(未设置)')}")
        return
    if bark_url is not None:
        cc.set("push.bark_url", bark_url)
        console.print_result(f"Bark URL 已设置: {bark_url}")
    if ntfy_url is not None:
        cc.set("push.ntfy_url", ntfy_url)
        console.print_result(f"ntfy URL 已设置: {ntfy_url}")
    if ntfy_token is not None:
        cc.set("push.ntfy_token", ntfy_token)
        _nt = str(ntfy_token)
        _nshown = f"{_nt[:4]}***{_nt[-4:]}" if len(_nt) > 8 else "(已设置)"
        console.print_result(f"ntfy Token 已设置: {_nshown}")
    if sound is not None:
        cc.set("push.sound", sound)
    if priority is not None:
        cc.set("push.priority", priority)


# ── 深度研究命令 ──


@cli.group("research")
def research():
    """深度研究 — 多 Agent 研究 (规划→搜索→合成)。"""
    pass


@research.command("run")
@click.argument("question")
@click.option("--depth", "-d", type=int, default=3, help="子问题数量 (规划深度)")
@click.option("--max-sources", "-s", type=int, default=3, help="每子问题最大来源数")
@click.option("--model", "-m", default="", help="fusion-mlx 模型 (空则自动)")
@click.option("--output", "-o", default="", help="输出 Markdown 文件路径 (空则打印)")
def research_run(question: str, depth: int, max_sources: int, model: str, output: str):
    """运行深度研究, 产出带引用的研究报告。"""
    from .research import run_deep_research

    console.print_header(f"🔍 深度研究: {question}")
    report = asyncio.run(
        run_deep_research(question, depth=depth, max_sources=max_sources, model=model, mlx_client=_get_mlx_client())
    )
    if report.error and not report.findings:
        console.print_error(f"❌ 研究失败: {report.error}")
        return
    if report.degraded:
        console.print_warning("⚠️  LLM 不可用, 使用降级模式 (原始发现汇总)")
    console.print_info(f"子问题: {len(report.sub_questions)} | 来源: {report.sources_used}")

    md = _format_research_report(report)
    if output:
        Path(output).write_text(md, encoding="utf-8")
        console.print_success(f"✅ 报告已写入: {output}")
    else:
        click.echo(md)


def _format_research_report(report) -> str:
    lines = [f"# 深度研究报告: {report.question}", ""]
    if report.sub_questions:
        lines.append("## 研究子问题")
        for i, sq in enumerate(report.sub_questions, 1):
            lines.append(f"{i}. {sq.question}")
            if sq.rationale:
                lines.append(f"   _{sq.rationale}_")
        lines.append("")
    lines.append("## 研究报告")
    lines.append("")
    lines.append(report.synthesis)
    lines.append("")
    if report.findings:
        lines.append("## 引用来源")
        for i, f in enumerate(report.findings, 1):
            lines.append(f"[{i}] {f.title} — {f.url}")
    return "\n".join(lines)


# ── 多 Agent 代码审查命令 ──


@cli.group("review")
def review():
    """UltraReview — 多 Agent 代码审查 (安全/正确性/风格/测试)。"""
    pass


@review.command("run")
@click.argument("paths", nargs=-1)
@click.option("--model", "-m", default="", help="fusion-mlx 模型 (空则自动)")
@click.option("--lens", "-l", multiple=True, help="审查视角 (security/correctness/style/tests, 可多次)")
@click.option("--diff", "use_diff", is_flag=True, help="审查 git diff 变更文件 (忽略 paths)")
@click.option("--output", "-o", default="", help="输出 Markdown 文件路径 (空则打印)")
def review_run(paths, model: str, lens, use_diff: bool, output: str):
    """运行多视角代码审查。"""
    from .review import run_ultra_review
    from .review.ultra_review import collect_git_diff_files

    targets = list(paths)
    if use_diff:
        targets = collect_git_diff_files()
        if not targets:
            console.print_warning("⚠️  git diff 无变更 .py 文件")
            return
        console.print_info(f"审查 git diff 变更: {len(targets)} 个文件")
    if not targets:
        console.print_error("❌ 未指定审查目标 (paths 或 --diff)")
        return

    lenses = list(lens) if lens else None
    console.print_header(f"🔎 UltraReview: {len(targets)} 个目标")
    report = asyncio.run(run_ultra_review(targets, model=model, mlx_client=_get_mlx_client(), lenses=lenses))
    if report.error and not report.files_reviewed:
        console.print_error(f"❌ 审查失败: {report.error}")
        return
    if report.degraded:
        console.print_warning("⚠️  LLM 不可用, 使用降级模式 (文件清单)")
    console.print_info(f"审查文件: {len(report.files_reviewed)} | 发现: {len(report.findings)}")

    md = _format_review_report(report)
    if output:
        Path(output).write_text(md, encoding="utf-8")
        console.print_success(f"✅ 报告已写入: {output}")
    else:
        click.echo(md)


def _format_review_report(report) -> str:
    lines = ["# UltraReview 代码审查报告", "", f"目标: {report.target}", ""]
    if report.files_reviewed:
        lines.append("## 审查文件")
        for p in report.files_reviewed:
            lines.append(f"- {p}")
        lines.append("")
    if report.findings:
        lines.append("## 发现问题")
        for i, f in enumerate(report.findings, 1):
            lines.append(f"{i}. **[{f.severity.upper()}]** {f.file}:{f.line} — {f.message}  ")
            lines.append(f"   _视角: {f.lens} | 类别: {f.category}_")
        lines.append("")
    lines.append("## 审查总结")
    lines.append("")
    lines.append(report.summary)
    return "\n".join(lines)


# ── Schema 结构化输出命令 ──


@cli.group("agent-loop")
def agent_loop():
    """交互式对话 Agent Loop — 白话自主拆解多步, 逐步观察决策行动。"""
    pass


@agent_loop.command("run")
@click.argument("prompt")
@click.option("--model", "-m", default="", help="fusion-mlx 模型 (空则自动)")
@click.option("--max-steps", default=20, help="最大循环步数")
def agent_loop_run(prompt: str, model: str, max_steps: int):
    """运行交互式 agent loop (单轮)。"""
    from .agent_loop import AgentLoop
    from .nodes import import_all_nodes

    import_all_nodes()

    loop = AgentLoop(mlx_client=_get_mlx_client(), model=model, max_steps=max_steps, on_turn=_print_agent_turn)
    console.print_header(f"🤖 Agent Loop (max {max_steps} 步)")
    result = asyncio.run(loop.run(prompt))
    if result.degraded:
        console.print_warning("⚠️  LLM 不可用, 降级模式")
    if result.interrupted:
        console.print_warning("⚠️  被用户叫停")
    console.print_info(f"完成={result.completed} 步数={len(result.turns)}")


def _print_agent_turn(turn):
    if turn.role == "user" and turn.content.startswith("[补充]"):
        console.print_info(turn.content)
        return
    if turn.role == "user":
        return
    if turn.action is None:
        console.print_info(turn.content)
        return
    console.print_info(f"[{turn.action.type}] {turn.action.message}")
    if turn.action.node:
        click.echo(f"    节点: {turn.action.node} | 参数: {json.dumps(turn.action.params, ensure_ascii=False)}")
    if turn.observation:
        click.echo(f"    观察: {turn.observation[:200]}")


@agent_loop.command("chat")
@click.option("--model", "-m", default="", help="fusion-mlx 模型 (空则自动)")
@click.option("--max-steps", default=20, help="单轮最大循环步数")
def agent_loop_chat(model: str, max_steps: int):
    """交互式多轮对话 agent loop (REPL, 输入 quit 退出)。"""
    from .agent_loop import AgentLoop
    from .nodes import import_all_nodes

    import_all_nodes()

    loop = AgentLoop(mlx_client=_get_mlx_client(), model=model, max_steps=max_steps, on_turn=_print_agent_turn)
    console.print_header("🤖 Agent Loop 对话模式 (输入 quit 退出, stop 叫停当前轮)")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("\n再见")
            break
        if not line:
            continue
        if line.lower() == "quit":
            click.echo("再见")
            break
        if line.lower() == "stop":
            loop.interrupt()
            continue
        result = asyncio.run(loop.run(line))
        if result.degraded:
            console.print_warning("⚠️  LLM 不可用, 降级模式")
        console.print_info(f"  完成={result.completed} 步数={len(result.turns)}")


# ── Schema 结构化输出命令 ──


@cli.group("collab")
def collab():
    """协作层 WebSocket 双向通道 — 实时聊天/光标/presence。"""
    pass


@collab.command("serve")
@click.option("--host", default="127.0.0.1", help="监听地址")
@click.option("--port", default=11439, help="监听端口")
def collab_serve(host: str, port: int):
    """启动协作 WS 服务 (阻塞)。"""
    from .config_center import ConfigCenter
    from .server.collab_ws import CollabHub

    auth_token = ConfigCenter.get_instance().get("collab.auth_token") or None

    async def _run():
        hub = CollabHub(auth_token=auth_token)
        server = await hub.serve_ws(host=host, port=port)
        console.print_success(f"✅ 协作 WS 服务: ws://{host}:{port} (Ctrl+C 退出)")
        try:
            await asyncio.Future()
        finally:
            server.close()
            await server.wait_closed()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\n停止")


# ── Schema 结构化输出命令 ──


@cli.group("schema")
def schema():
    """结构化输出 — JSON Schema 校验。"""
    pass


@schema.command("validate")
@click.argument("data_file", type=click.Path(exists=True))
@click.argument("schema_file", type=click.Path(exists=True))
def schema_validate(data_file: str, schema_file: str):
    """校验数据文件是否符合 schema。"""
    from .engine.schema import OutputSchema

    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)
    with open(schema_file, encoding="utf-8") as f:
        schema = json.load(f)
    valid = OutputSchema.validate(data, schema)
    if valid:
        console.print_success("✅ 数据符合 schema")
    else:
        console.print_error("❌ 数据不符合 schema")
        errors = OutputSchema.validate_detailed(data, schema)
        for err in errors:
            click.echo(f"  - {err}")


@schema.command("check")
@click.argument("node_name")
def schema_check(node_name: str):
    """检查节点的输出 schema。"""
    node_cls = NodeRegistry.get(node_name)
    if not node_cls:
        console.print_error(f"未知节点: {node_name}")
        return
    node = node_cls()
    params_schema = node.get_params_schema()
    click.echo(json.dumps(params_schema, indent=2, ensure_ascii=False))


# ── Benchmark ──


@cli.group("benchmark")
def benchmark():
    """功能对比与性能基准。"""
    pass


@benchmark.command("report")
@click.option("--format", "fmt", default="markdown", type=click.Choice(["markdown", "html", "json"]))
@click.option("--output", "-o", default="", help="输出文件路径")
def benchmark_report(fmt, output):
    """生成 Claude Cowork vs Fusion-Cowork 对比报告。"""
    from fusion_cowork.benchmark import CapabilityMatrix, ReportRenderer

    matrix = CapabilityMatrix()
    renderer = ReportRenderer(matrix=matrix)
    if fmt == "json":
        content = matrix.to_json()
    elif fmt == "html":
        content = renderer.render_html()
    else:
        content = renderer.render_markdown()
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        console.print_result(f"报告已保存: {output}")
    else:
        click.echo(content)


@benchmark.command("run")
@click.option("--node", "-n", multiple=True, help="要测试的节点名 (可多次指定)")
@click.option("--repeats", default=3, type=int, help="每个节点重复次数")
def benchmark_run(node, repeats):
    """运行节点性能基准。"""
    import asyncio

    from fusion_cowork.benchmark import BenchmarkRunner

    runner = BenchmarkRunner(repeats=repeats)
    specs = (
        [{"node": n, "params": {}} for n in node]
        if node
        else [
            {"node": "file_input", "params": {"path": "~"}},
            {"node": "shell_exec", "params": {"command": "echo hello", "timeout": 5}},
        ]
    )
    asyncio.run(runner.run_nodes(specs))
    click.echo(runner.to_json())


# ── Space 协作空间 ──


@cli.group("space")
def space():
    """协作空间管理 — 创建/列表/归档/成员管理。"""
    pass


@space.command("create")
@click.argument("name")
@click.option("--owner", "-o", default="local_user", help="Owner 用户 ID")
@click.option("--description", "-d", default="", help="空间描述")
@click.option("--collab-mode", default="local", help="协作模式 (local/p2p)")
def space_create(name: str, owner: str, description: str, collab_mode: str):
    """创建协作空间。"""
    asyncio.run(_async_space_create(name, owner, description, collab_mode))


async def _async_space_create(name: str, owner: str, description: str, collab_mode: str):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        space_obj = await svc.create(name=name, owner_id=owner, description=description, collab_mode=collab_mode)
        console.print_success(f"空间已创建: {space_obj.id}")
        console.print_result(f"  名称: {space_obj.name}")
        console.print_result(f"  Owner: {space_obj.owner_id}")
        console.print_result(f"  状态: {space_obj.status}")
    finally:
        await store.close()


@space.command("list")
@click.option("--status", "-s", default=None, help="按状态过滤 (active/archived)")
@click.option("--owner", "-o", default=None, help="按 Owner 过滤")
@click.option("--limit", "-n", default=20, help="返回数量")
def space_list(status, owner, limit):
    """列出协作空间。"""
    asyncio.run(_async_space_list(status, owner, limit))


async def _async_space_list(status, owner, limit):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        spaces = await svc.list(status=status, owner_id=owner, limit=limit)
        if not spaces:
            console.print_info("没有协作空间")
            return
        console.print_header(f"协作空间列表 ({len(spaces)} 个)")
        rows = []
        for sp in spaces:
            rows.append([sp.id, sp.name, sp.owner_id, sp.status, sp.collab_mode])
        console.print_table(["ID", "名称", "Owner", "状态", "协作模式"], rows)
    finally:
        await store.close()


@space.command("get")
@click.argument("space_id")
def space_get(space_id: str):
    """查看协作空间详情。"""
    asyncio.run(_async_space_get(space_id))


async def _async_space_get(space_id: str):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        sp = await svc.get(space_id)
        if not sp:
            console.print_error(f"空间不存在: {space_id}")
            return
        console.print_header(f"🚀 {sp.name}")
        click.echo(f"  ID: {sp.id}")
        click.echo(f"  描述: {sp.description}")
        click.echo(f"  Owner: {sp.owner_id}")
        click.echo(f"  状态: {sp.status}")
        click.echo(f"  协作模式: {sp.collab_mode}")
        click.echo(f"  KB 绑定: {sp.kb_bind_mode} ({sp.kb_id or '无'})")
        click.echo(f"  创建: {sp.created_at}")
        click.echo(f"  更新: {sp.updated_at}")
        config = sp.config
        click.echo("\n  配置:")
        click.echo(f"    最大成员: {config.max_members}")
        click.echo(f"    Web 搜索: {'✅' if config.enable_web_search else '❌'}")
        click.echo(f"    深度研究: {'✅' if config.enable_deep_research else '❌'}")
        click.echo(f"    Computer Use: {'✅' if config.enable_computer_use else '❌'}")
        click.echo(f"    流式响应: {'✅' if config.stream_response else '❌'}")
        members = await store.list_members(space_id)
        click.echo(f"\n  成员 ({len(members)} 人):")
        for m in members:
            click.echo(f"    ├─ {m.display_name} ({m.user_id}) — {m.role}")
    finally:
        await store.close()


@space.command("archive")
@click.argument("space_id")
def space_archive(space_id: str):
    """归档协作空间。"""
    asyncio.run(_async_space_archive(space_id))


async def _async_space_archive(space_id: str):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        result = await svc.archive(space_id)
        if result:
            console.print_success(f"空间已归档: {space_id}")
        else:
            console.print_error(f"归档失败: 空间不存在 {space_id}")
    finally:
        await store.close()


@space.group("member")
def space_member():
    """协作空间成员管理。"""
    pass


@space_member.command("list")
@click.argument("space_id")
def space_member_list(space_id: str):
    """列出空间成员。"""
    asyncio.run(_async_space_member_list(space_id))


async def _async_space_member_list(space_id: str):
    from fusion_cowork.space import SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        members = await store.list_members(space_id)
        if not members:
            console.print_info("空间没有成员")
            return
        console.print_header(f"成员列表 ({len(members)} 人)")
        rows = []
        for m in members:
            rows.append([m.user_id, m.display_name, m.role, m.joined_at[:19]])
        console.print_table(["用户 ID", "显示名", "角色", "加入时间"], rows)
    finally:
        await store.close()


@space_member.command("invite")
@click.argument("space_id")
@click.option("--inviter", "-i", default="local_user", help="邀请人 ID")
@click.option("--role", "-r", default="member", help="成员角色 (member/admin/viewer)")
@click.option("--max-uses", default=0, type=int, help="最大使用次数 (0=无限)")
@click.option("--expires-hours", default=0, type=int, help="过期时间 (小时, 0=永不过期)")
def space_member_invite(space_id: str, inviter: str, role: str, max_uses: int, expires_hours: int):
    """生成邀请链接。"""
    asyncio.run(_async_space_member_invite(space_id, inviter, role, max_uses, expires_hours))


async def _async_space_member_invite(space_id: str, inviter: str, role: str, max_uses: int, expires_hours: int):
    from fusion_cowork.space import SpaceMemberService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        svc = SpaceMemberService(store, perm)
        code = await svc.invite(space_id, inviter, role=role, max_uses=max_uses, expires_hours=expires_hours)
        console.print_success(f"邀请码已生成: {code}")
        console.print_info(f"使用 'fusion-cowork space member join {code}' 加入空间")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_member.command("join")
@click.argument("invite_code")
@click.option("--user", "-u", default="local_user", help="用户 ID")
@click.option("--display-name", "-n", default="", help="显示名")
def space_member_join(invite_code: str, user: str, display_name: str):
    """通过邀请码加入空间。"""
    asyncio.run(_async_space_member_join(invite_code, user, display_name))


async def _async_space_member_join(invite_code: str, user: str, display_name: str):
    from fusion_cowork.space import SpaceMemberService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        svc = SpaceMemberService(store, perm)
        member = await svc.join(invite_code, user_id=user, display_name=display_name)
        console.print_success(f"已加入空间: {member.space_id} (角色: {member.role})")
    except ValueError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_member.command("remove")
@click.argument("space_id")
@click.argument("user_id")
@click.option("--operator", "-o", default="local_user", help="操作人 ID")
def space_member_remove(space_id: str, user_id: str, operator: str):
    """移除空间成员。"""
    asyncio.run(_async_space_member_remove(space_id, user_id, operator))


async def _async_space_member_remove(space_id: str, user_id: str, operator: str):
    from fusion_cowork.space import SpaceMemberService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        svc = SpaceMemberService(store, perm)
        removed = await svc.remove(space_id, user_id, operator_id=operator)
        if removed:
            console.print_success(f"已移除成员: {user_id}")
        else:
            console.print_error("移除失败: 成员不存在")
    except (PermissionError, ValueError) as e:
        console.print_error(str(e))
    finally:
        await store.close()


# ── Space Chat ──


@space.command("chat")
@click.argument("space_id")
@click.option("--user", "-u", default="local_user", help="用户 ID")
@click.option("--agent", "-a", default=None, help="Agent ID (触发 Agent 回复)")
@click.option("--model", "-m", default=None, help="模型名称")
def space_chat(space_id: str, user: str, agent: str, model: str):
    """交互式共享对话。输入消息后按回车发送，Ctrl+C 退出。"""
    asyncio.run(_async_space_chat(space_id, user, agent, model))


async def _async_space_chat(space_id: str, user: str, agent: str, model: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceChatService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        sp = await store.get_space(space_id)
        if not sp:
            console.print_error(f"空间不存在: {space_id}")
            return
        perm = SpacePermission(store)
        mlx = FusionMLXClient()
        chat_svc = SpaceChatService(store, mlx, perm)

        console.print_header(f"Chat in {sp.name} (space: {space_id})")
        console.print_info("Type message and press Enter. Ctrl+C to exit.")

        history = await chat_svc.list_messages(space_id, limit=20)
        if history:
            console.print_info(f"--- Recent messages ({len(history)}) ---")
            for msg in history:
                role = msg.role or "user"
                prefix = "🤖" if role == "assistant" else "👤"
                content_preview = msg.content[:200] + ("..." if len(msg.content) > 200 else "")
                click.echo(f"  {prefix} [{msg.user_id or msg.agent_id or 'anon'}] {content_preview}")

        while True:
            try:
                content = click.prompt("", prompt_suffix="", default="", show_default=False)
                if not content.strip():
                    continue
                if agent:
                    console.print_info("Streaming agent response...")
                    full = []
                    async for chunk in chat_svc.stream_message(space_id, user, content, agent, model=model or ""):
                        full.append(chunk)
                        click.echo(chunk, nl=False)
                    click.echo()
                else:
                    msg = await chat_svc.send_message(space_id, user, content)
                    console.print_success(f"Sent (msg: {msg.id})")
            except KeyboardInterrupt:
                console.print_info("Exiting chat.")
                break
            except EOFError:
                break
            except PermissionError as e:
                console.print_error(str(e))
                break
    finally:
        await store.close()


# ── Space Knowledge ──


@space.group("knowledge")
def space_knowledge():
    """空间知识库管理。"""
    pass


@space_knowledge.command("bind")
@click.argument("space_id")
@click.option("--operator", "-o", default="local_user", help="操作人 ID")
@click.option("--kb-id", "-k", default=None, help="绑定已有知识库 ID (不传则自动创建)")
def space_kb_bind(space_id: str, operator: str, kb_id: str):
    """绑定知识库到空间。"""
    asyncio.run(_async_space_kb_bind(space_id, operator, kb_id))


async def _async_space_kb_bind(space_id: str, operator: str, kb_id: str):
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_svc = SpaceKBService(store, None, perm)
        result = await kb_svc.bind_kb(space_id, operator, kb_id=kb_id)
        console.print_success(f"知识库已绑定: {result}")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_knowledge.command("status")
@click.argument("space_id")
def space_kb_status(space_id: str):
    """查看空间知识库状态。"""
    asyncio.run(_async_space_kb_status(space_id))


async def _async_space_kb_status(space_id: str):
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_svc = SpaceKBService(store, None, perm)
        status = await kb_svc.get_kb_status(space_id)
        if status["bound"]:
            console.print_header(f"知识库已绑定: {status['kb_id']}")
            docs = status.get("document_count", 0)
            click.echo(f"  文档数量: {docs}")
        else:
            console.print_info("空间未绑定知识库")
    finally:
        await store.close()


@space_knowledge.command("upload")
@click.argument("space_id")
@click.argument("file_path")
@click.option("--operator", "-o", default="local_user", help="操作人 ID")
def space_kb_upload(space_id: str, file_path: str, operator: str):
    """上传文件到空间知识库。"""
    asyncio.run(_async_space_kb_upload(space_id, file_path, operator))


async def _async_space_kb_upload(space_id: str, file_path: str, operator: str):
    from fusion_cowork.ai.mlx_client import KBClient
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_client = KBClient()
        kb_svc = SpaceKBService(store, kb_client, perm)
        result = await kb_svc.upload_document(space_id, operator, file_path)
        console.print_success(f"文件已上传: {result}")
    except PermissionError as e:
        console.print_error(str(e))
    except FileNotFoundError:
        console.print_error(f"文件不存在: {file_path}")
    finally:
        await store.close()


@space_knowledge.command("search")
@click.argument("space_id")
@click.argument("query")
@click.option("--top-k", "-k", default=5, help="返回结果数量")
def space_kb_search(space_id: str, query: str, top_k: int):
    """在空间知识库中搜索。"""
    asyncio.run(_async_space_kb_search(space_id, query, top_k))


async def _async_space_kb_search(space_id: str, query: str, top_k: int):
    from fusion_cowork.ai.mlx_client import KBClient
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_client = KBClient()
        kb_svc = SpaceKBService(store, kb_client, perm)
        results = await kb_svc.search(space_id, query, top_k=top_k)
        if not results:
            console.print_info("无搜索结果")
            return
        console.print_header(f"搜索结果 ({len(results)} 条)")
        for i, r in enumerate(results, 1):
            score = r.get("score", 0)
            content = r.get("content", "")[:150]
            click.echo(f"  {i}. [score={score:.2f}] {content}")
    finally:
        await store.close()


@space_knowledge.command("unbind")
@click.argument("space_id")
@click.option("--operator", "-o", default="local_user", help="操作人 ID")
def space_kb_unbind(space_id: str, operator: str):
    """解除空间知识库绑定。"""
    asyncio.run(_async_space_kb_unbind(space_id, operator))


async def _async_space_kb_unbind(space_id: str, operator: str):
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_svc = SpaceKBService(store, None, perm)
        await kb_svc.unbind_kb(space_id, operator)
        console.print_success(f"知识库已解绑: {space_id}")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


# ── Space Agent ──


@space.group("agent")
def space_agent():
    """空间 Agent 管理。"""
    pass


@space_agent.command("list")
@click.argument("space_id")
def space_agent_list(space_id: str):
    """列出空间 Agent。"""
    asyncio.run(_async_space_agent_list(space_id))


async def _async_space_agent_list(space_id: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceAgentRuntime, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(store, mlx, perm)
        agents = await rt.list_agents(space_id)
        if not agents:
            console.print_info("空间没有 Agent")
            return
        console.print_header(f"Agent 列表 ({len(agents)} 个)")
        rows = []
        for a in agents:
            rows.append(
                [
                    a.get("id", ""),
                    a.get("name", ""),
                    a.get("agent_type", ""),
                    "✅" if a.get("enable_rag") else "❌",
                    a.get("created_by", ""),
                ]
            )
        console.print_table(["ID", "名称", "类型", "RAG", "创建人"], rows)
    finally:
        await store.close()


@space_agent.command("add")
@click.argument("space_id")
@click.argument("name")
@click.option("--type", "-t", "agent_type", default="assistant", help="Agent 类型")
@click.option("--prompt", "-p", default="", help="系统提示词")
@click.option("--rag/--no-rag", default=False, help="启用 RAG")
@click.option("--operator", "-o", default="local_user", help="操作人 ID")
def space_agent_add(space_id: str, name: str, agent_type: str, prompt: str, rag: bool, operator: str):
    """添加 Agent 到空间。"""
    asyncio.run(_async_space_agent_add(space_id, name, agent_type, prompt, rag, operator))


async def _async_space_agent_add(space_id: str, name: str, agent_type: str, prompt: str, rag: bool, operator: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceAgentRuntime, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(store, mlx, perm)
        result = await rt.add_agent(
            space_id=space_id,
            operator_id=operator,
            name=name,
            agent_type=agent_type,
            system_prompt=prompt,
            enable_rag=rag,
        )
        console.print_success(f"Agent 已添加: {result['id']} ({result['name']})")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_agent.command("remove")
@click.argument("space_id")
@click.argument("agent_id")
@click.option("--operator", "-o", default="local_user", help="操作人 ID")
def space_agent_remove(space_id: str, agent_id: str, operator: str):
    """移除空间 Agent。"""
    asyncio.run(_async_space_agent_remove(space_id, agent_id, operator))


async def _async_space_agent_remove(space_id: str, agent_id: str, operator: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceAgentRuntime, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(store, mlx, perm)
        removed = await rt.remove_agent(space_id, agent_id, operator)
        if removed:
            console.print_success(f"Agent 已移除: {agent_id}")
        else:
            console.print_error(f"Agent 不存在: {agent_id}")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_agent.command("call")
@click.argument("space_id")
@click.argument("agent_id")
@click.argument("message")
@click.option("--user", "-u", default="local_user", help="用户 ID")
@click.option("--model", "-m", default="", help="模型名称")
def space_agent_call(space_id: str, agent_id: str, message: str, user: str, model: str):
    """调用空间 Agent。"""
    asyncio.run(_async_space_agent_call(space_id, agent_id, message, user, model))


async def _async_space_agent_call(space_id: str, agent_id: str, message: str, user: str, model: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceAgentRuntime, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(store, mlx, perm)
        reply = await rt.call_agent(space_id, agent_id, user, message, model=model)
        click.echo(reply)
    except PermissionError as e:
        console.print_error(str(e))
    except ValueError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_agent.command("relay")
@click.argument("space_id")
@click.argument("message")
@click.option("--agents", "-a", required=True, help="Agent ID 列表 (逗号分隔)")
@click.option("--user", "-u", default="local_user", help="用户 ID")
@click.option("--model", "-m", default="", help="模型名称")
def space_agent_relay(space_id: str, message: str, agents: str, user: str, model: str):
    """多 Agent 接力调用。"""
    asyncio.run(_async_space_agent_relay(space_id, message, agents, user, model))


async def _async_space_agent_relay(space_id: str, message: str, agents: str, user: str, model: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceAgentRuntime, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(store, mlx, perm)
        agent_ids = [a.strip() for a in agents.split(",") if a.strip()]
        results = await rt.chain_agents(space_id, agent_ids, user, message, model=model)
        console.print_header(f"Agent 接力结果 ({len(results)} 步)")
        for i, r in enumerate(results, 1):
            aid = r.get("agent_id", "?")
            if "error" in r:
                click.echo(f"  Step {i} [{aid}]: ❌ {r['error']}")
            else:
                content = r.get("content", "")
                preview = content[:300] + ("..." if len(content) > 300 else "")
                click.echo(f"  Step {i} [{aid}]: {preview}")
    except PermissionError as e:
        console.print_error(str(e))
    except ValueError as e:
        console.print_error(str(e))
    finally:
        await store.close()


# ── 数据库命令 (v0.4.0 Stage 3) ──


@cli.group()
def db():
    """数据库备份/恢复 + 迁移管理 (postgres 后端)。"""


@db.command("backup")
@click.option("--dsn", default="", help="Postgres DSN (缺则读 env FUSION_PG_DSN)")
@click.option("--dest", default="", help="备份输出路径 (.sql.gz)")
def db_backup(dsn: str, dest: str):
    """pg_dump 备份到 .sql.gz。"""
    import os

    dsn = dsn or os.environ.get("FUSION_PG_DSN", "")
    if not dsn:
        console.print_error("缺 DSN — 传 --dsn 或设 env FUSION_PG_DSN")
        raise SystemExit(1)
    if not dest:
        from datetime import datetime

        dest = f"fusion-cowork-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sql.gz"
    from .db.backup import BackupManager

    mgr = BackupManager(dsn)
    out = mgr.backup(dest)
    console.print_success(f"备份完成: {out} ({out.stat().st_size} bytes)")


@db.command("restore")
@click.option("--dsn", default="", help="Postgres DSN (缺则读 env FUSION_PG_DSN)")
@click.option("--src", required=True, help="备份文件 (.sql.gz)")
@click.option("--confirm", is_flag=True, help="确认覆盖现有数据库 (破坏性)")
def db_restore(dsn: str, src: str, confirm: bool):
    """从 .sql.gz 恢复 (破坏性, 需 --confirm)。"""
    import os

    dsn = dsn or os.environ.get("FUSION_PG_DSN", "")
    if not dsn:
        console.print_error("缺 DSN — 传 --dsn 或设 env FUSION_PG_DSN")
        raise SystemExit(1)
    from .db.backup import BackupManager

    mgr = BackupManager(dsn)
    mgr.restore(src, confirm=confirm)
    console.print_success(f"恢复完成: {src}")


@db.command("migrate")
@click.option("--dsn", default="", help="Postgres DSN (缺则读 env FUSION_PG_DSN)")
def db_migrate(dsn: str):
    """执行待应用的数据库迁移 (版本化, 幂等)。"""
    import asyncio
    import os

    dsn = dsn or os.environ.get("FUSION_PG_DSN", "")
    if not dsn:
        console.print_error("缺 DSN — 传 --dsn 或设 env FUSION_PG_DSN")
        raise SystemExit(1)

    async def _run():
        import asyncpg

        from .db.migrations import MigrationRunner

        conn = await asyncpg.connect(dsn)
        try:
            # 建基础 schema (迁移依赖表存在)
            from .space.store import _pg_schema_sql

            await conn.execute(_pg_schema_sql())

            async def _exec(sql, params=(), read=False):
                from .db.placeholders import normalize_placeholders

                pgsql = normalize_placeholders(sql)
                if read:
                    return list(await conn.fetch(pgsql, *(params or ())))
                await conn.execute(pgsql, *(params or ()))
                return None

            runner = MigrationRunner("postgres", executor=_exec, error_class=asyncpg.DuplicateColumnError)
            applied = await runner.run_pending()
            console.print_success(f"迁移完成: 应用版本 {applied or '(无待应用)'}")
        finally:
            await conn.close()

    asyncio.run(_run())


# ── 主入口 ──


def main():
    """Fusion-Cowork 主入口。"""
    cli()


if __name__ == "__main__":
    main()
