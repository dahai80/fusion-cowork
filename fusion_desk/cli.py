"""Fusion-Desk CLI 入口 — 命令行界面。

提供完整的命令行操作：
- 模板列表/搜索/运行
- 自然语言生成工作流
- 工作流执行
- 定时任务管理
- AI 服务状态
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from . import __version__, __app_name__, NODE_NAME_ALIASES
from .engine import (
    Workflow, WorkflowEngine, WorkflowStatus,
    NodeRegistry, TaskScheduler, TaskStatus,
)
from .nodes.macos import (
    DesktopCleanNode, DownloadOrganizerNode, FileClassifierNode,
    FileBatchRenameNode, DiskCleanerNode, FileWatcherNode,
    FileCopyNode, FileMoveNode, FileDeleteNode, FileFindNode,
)
from .nodes.ai import AIClassifyNode, AISummarizeNode, AIGenerateNameNode
from .nodes.io import FileInputNode, FileOutputNode
from .nodes.logic import FilterNode, LoopNode, MergeNode
from .nodes.tools import ShellExecNode, PythonREPLNode, WebSearchNode, FetchURLNode, ApplyEditNode
from .nodes.browser import BrowserClient, BrowserManager
from .ai import FusionMLXClient, NLWorkflowGenerator
from .templates import TemplateManager
from .utils.logger import setup_logger

logger = logging.getLogger(__name__)

# 全局实例
_engine = WorkflowEngine()
_scheduler = TaskScheduler()
_template_mgr = TemplateManager()
_mlx_client = FusionMLXClient()


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
@click.version_option(version=__version__, prog_name=__app_name__)
def cli(verbose: bool, log_file: str):
    """Fusion-Desk — macOS 原生、纯本地离线、零代码桌面智能自动化平台。

    让 Mac 自己干活，本地 AI 全自动桌面办公。
    """
    level = logging.DEBUG if verbose else logging.INFO
    setup_logger(level=level, log_file=log_file, verbose=verbose)

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
        templates = _template_mgr.search_templates(search)
    else:
        templates = _template_mgr.list_templates(category=category, tag=tag)

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
            rows.append([
                tpl.get("id", ""),
                f"{tpl.get('icon', '📄')} {tpl.get('name', '')}{ai_tag}",
                tpl.get("difficulty", ""),
                tpl.get("estimated_time", ""),
            ])
        console.print_table(["ID", "名称", "难度", "预计时间"], rows)

    click.echo(f"\n💡 使用 'fusion-desk template run <id>' 运行模板")
    click.echo(f"💡 使用 'fusion-desk template show <id>' 查看详情")


@template.command("show")
@click.argument("template_id")
def show_template(template_id: str):
    """查看模板详情。"""
    tpl = _template_mgr.get_template(template_id)
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
        click.echo(f"  🤖 需要 AI 模型 (fusion-mlx)")

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

    click.echo(f"\n💡 运行: fusion-desk template run {template_id}")


@template.command("run")
@click.argument("template_id")
@click.option("--dry-run", "-n", is_flag=True, help="预览模式")
@click.option("--params", "-p", default="", help="覆盖参数 JSON")
def run_template(template_id: str, dry_run: bool, params: str):
    """运行模板。"""
    asyncio.run(_async_run_template(template_id, dry_run, params))


async def _async_run_template(template_id: str, dry_run: bool, params_json: str):
    console.print_header(f"🚀 运行模板: {template_id}")

    # 加载模板
    wf = _template_mgr.template_to_workflow(template_id)
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

    # 执行工作流
    console.print_info("正在执行...")
    execution = await _engine.execute(wf)

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

    示例: fusion-desk ai generate "帮我把桌面所有 PDF 按主题分类归档"
    """
    asyncio.run(_async_generate(prompt, model))


async def _async_generate(prompt: str, model: str):
    console.print_header(f"🤖 AI 生成工作流")

    # 检查 fusion-mlx 是否可用
    health = await _mlx_client.health()
    if not health:
        console.print_warning("⚠️  fusion-mlx 未运行，将使用本地规则引擎")
        console.print_info("   启动: fusion-mlx 或 MLX 服务")
        console.print_info("   将使用模板匹配模式")

    # 生成工作流
    generator = NLWorkflowGenerator(_mlx_client, model=model)
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

    click.echo(f"\n💡 运行: fusion-desk workflow run <保存的工作流文件>")


@ai.command("status")
def ai_status():
    """检查 fusion-mlx 和 AI 服务状态。"""
    asyncio.run(_async_ai_status())


async def _async_ai_status():
    console.print_header("🔌 AI 服务状态")

    # 检查 fusion-mlx
    mlx_ok = await _mlx_client.health()
    if mlx_ok:
        console.print_success("✅ fusion-mlx: 运行中")
        try:
            models = await _mlx_client.list_models()
            if models:
                click.echo(f"   可用模型: {', '.join(m.get('id', m.get('model', '?')) for m in models[:5])}")
        except Exception:
            pass
    else:
        console.print_warning("⚠️  fusion-mlx: 未运行")
        click.echo("   启动: 请启动 fusion-mlx 服务 (默认端口 8000)")

    # 检查融合 KB
    from .ai import KBClient
    kb = KBClient()
    kb_ok = await kb.health()
    if kb_ok:
        console.print_success("✅ Fusion-KB: 运行中")
    else:
        console.print_info("ℹ️  Fusion-KB: 未运行 (可选)")


# ── 工作流命令 ──

@cli.group()
def workflow():
    """工作流管理：执行、查看、管理。"""
    pass


@workflow.command("run")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.option("--dry-run", "-n", is_flag=True, help="预览模式")
def run_workflow_file(workflow_file: str, dry_run: bool):
    """从 JSON 文件加载并执行工作流。"""
    asyncio.run(_async_run_workflow_file(workflow_file, dry_run))


async def _async_run_workflow_file(workflow_file: str, dry_run: bool):
    console.print_header(f"🚀 执行工作流: {workflow_file}")

    try:
        with open(workflow_file, "r", encoding="utf-8") as f:
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

    console.print_info("正在执行...")
    execution = await _engine.execute(wf)

    if execution.status == WorkflowStatus.SUCCESS:
        console.print_success(f"✅ 执行成功! ({execution.total_time:.2f}s)")
    elif execution.status == WorkflowStatus.FAILED:
        console.print_error(f"❌ 执行失败: {execution.error}")

    for step in execution.steps:
        status_icon = {
            "success": "✅", "failed": "❌", "running": "⏳",
            "pending": "⏸️", "skipped": "⏭️",
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
    executions = _engine.list_executions(limit=20)
    if not executions:
        console.print_info("暂无工作流执行记录")
        return

    console.print_header("最近执行记录")
    rows = []
    for exec_ in executions:
        status_icon = {
            "success": "✅", "failed": "❌", "running": "⏳",
            "pending": "⏸️", "cancelled": "⏹️", "partial": "⚠️",
        }.get(exec_.status.value, "❓")
        rows.append([
            exec_.id[:12],
            exec_.workflow_name[:30],
            f"{status_icon} {exec_.status.value}",
            f"{exec_.total_time:.1f}s",
            time.strftime("%H:%M:%S", time.localtime(exec_.started_at)),
        ])
    console.print_table(["ID", "名称", "状态", "耗时", "时间"], rows)


# ── 定时任务命令 ──

@cli.group()
def schedule():
    """定时任务管理：创建、列出、管理定时自动化任务。"""
    pass


@schedule.command("list")
def list_schedules():
    """列出所有定时任务。"""
    tasks = _scheduler.list_tasks()
    if not tasks:
        console.print_info("暂无定时任务")
        console.print_info("使用 'fusion-desk schedule add' 添加定时任务")
        return

    console.print_header("定时任务列表")
    rows = []
    for task in tasks:
        status_icon = "✅" if task.status == TaskStatus.ACTIVE else "⏸️"
        next_run = time.strftime("%m-%d %H:%M", time.localtime(task.next_run)) if task.next_run else "-"
        rows.append([
            task.id[:12],
            task.name[:25],
            f"{status_icon} {task.status.value}",
            f"{task.run_count}次",
            next_run,
        ])
    console.print_table(["ID", "名称", "状态", "执行", "下次运行"], rows)


@schedule.command("add")
@click.option("--name", "-n", required=True, help="任务名称")
@click.option("--cron", "-c", required=True, help="Cron 表达式 (如: 0 21 * * *)")
@click.option("--template", "-t", required=True, help="要运行的模板 ID")
@click.option("--description", "-d", default="", help="任务描述")
def add_schedule(name: str, cron: str, template: str, description: str):
    """添加定时任务。"""
    task_id = _scheduler.add_cron_task(
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
    if _scheduler.remove_task(task_id):
        console.print_success(f"已删除任务: {task_id}")
    else:
        console.print_error(f"任务不存在: {task_id}")


@schedule.command("start")
def start_scheduler():
    """启动调度器。"""
    _scheduler.start()
    console.print_success("✅ 调度器已启动")


@schedule.command("stop")
def stop_scheduler():
    """停止调度器。"""
    _scheduler.shutdown()
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
    console.print_header("🖥️  Fusion-Desk 系统信息")
    click.echo(f"  版本: {__version__}")
    click.echo(f"  Python: {sys.version.split()[0]}")
    click.echo(f"  平台: {platform.system()} {platform.machine()}")
    click.echo(f"  Apple Silicon: {platform.machine() == 'arm64'}")
    click.echo(f"  工作目录: {os.getcwd()}")
    click.echo(f"  数据目录: {Path.home() / '.fusion-desk'}")

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
    templates = _template_mgr.list_templates()
    click.echo(f"  内置模板: {len(templates)} 个")


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
    desktop = DesktopCleanNode(config=type('obj', (object,), {'params': {
        "organize_by_type": True,
        "skip_hidden": True,
        "dry_run": dry_run,
    }})())
    result = await desktop.execute({})
    console.print_info(f"  桌面: {result.summary}")

    # 下载整理
    from .nodes.macos import DownloadOrganizerNode
    download = DownloadOrganizerNode(config=type('obj', (object,), {'params': {
        "organize_by_type": True,
        "deduplicate": True,
        "dry_run": dry_run,
    }})())
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
        console.print_error("启动失败，请先构建: fusion-desk browser build")


@browser.command("build")
def browser_build():
    """构建 Fusion 内嵌浏览器。"""
    asyncio.run(_async_browser_build())


async def _async_browser_build():
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
    client = BrowserClient()
    try:
        result = await client.open_url(url)
        console.print_success(f"已打开: {url}")
    except Exception as e:
        console.print_error(f"打开失败: {e}")
        console.print_info("请先启动浏览器: fusion-desk browser start")
    finally:
        await client.close()


@browser.command("status")
def browser_status():
    """检查浏览器状态。"""
    client = BrowserClient()
    if client.is_running():
        console.print_success("✅ 浏览器正在运行")
    else:
        console.print_warning("⚠️ 浏览器未运行")
        console.print_info("启动: fusion-desk browser start")


@browser.command("extract")
@click.argument("url", required=False)
@click.option("--to-file", "-o", default="", help="保存到文件")
def browser_extract(url: str = "", to_file: str = ""):
    """提取网页文本内容。"""
    asyncio.run(_async_browser_extract(url, to_file))


async def _async_browser_extract(url: str, to_file: str):
    from .nodes.browser import BrowserExtractNode
    from .engine import NodeConfig

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


# ── 主入口 ──

def main():
    """Fusion-Desk 主入口。"""
    cli()


if __name__ == "__main__":
    main()