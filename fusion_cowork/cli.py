"""Fusion-Cowork CLI entry — command line interface.

Provides full command line operations:
- template list/search/run
- natural language workflow generation
- workflow execution
- scheduled task management
- AI service status
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
from typing import Any, Dict, List

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
from .engine.lifecycle import get_lifecycle_bus

# LO-3: nodes.browser/input_nodes top-level import coupling — moved into lazy imports inside the command functions that use them,
# prevents nodes.browser from pulling missing optional deps in the future and breaking import fusion_cowork.cli entirely (every subcommand down)
from .templates import TemplateManager
from .utils.logger import setup_logger

logger = logging.getLogger(__name__)


def _set_process_title(title: str) -> None:
    # issue #203: long-running processes must show as "fusion-cowork <role>" in ps/Activity Monitor,
    # not a bare homebrew "Python". Prefer fusion_core.proc (shared helper); without
    # fusion-core try setproctitle directly; if neither is available degrade to a debug log (title stays default).
    try:
        from fusion_core.proc import set_process_title

        set_process_title(title)
        return
    except ImportError:
        pass
    try:
        import setproctitle

        setproctitle.setproctitle(title)
        logger.debug("process title set: %s", title)
    except Exception as exc:
        logger.debug("setproctitle unavailable, process title stays default (%s): %s", title, exc)


# Global instances — lazy initialization to avoid test pollution
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
        event_emitter.attach_lifecycle_bus(get_lifecycle_bus())
        space_store = None
        try:
            from .space import SpaceStore

            space_store = SpaceStore()
        except Exception as e:
            logger.warning(f"SpaceStore build failed (desk.space.* will be unavailable): {e}")
        _runtime = {
            "permission_manager": permission_manager,
            "hook_manager": hook_manager,
            "session_store": session_store,
            "event_emitter": event_emitter,
            "space_store": space_store,
        }
        logger.info("runtime managers built: permission/hook/session/event/space")
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
        # LO-1: asyncio.get_event_loop() is deprecated in py3.12 and errors without a running loop;
        # atexit runs on the main thread without a loop, so use asyncio.run to start a fresh loop for the close coroutine
        try:
            asyncio.get_running_loop()
            # Running loop present (should not happen in atexit, but defensive): dispatch a task without blocking
            asyncio.ensure_future(_mlx_client.close())
        except RuntimeError:
            asyncio.run(_mlx_client.close())
    except Exception as e:
        logger.debug(f"mlx_client cleanup failed: {e}")
    finally:
        _mlx_client = None


atexit.register(_cleanup_mlx_client)


class RichConsole:
    """Simple terminal output (fallback without the rich dependency)."""

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
        """Print a simple table."""
        # Compute column widths
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], len(str(cell)))

        # Header
        header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
        click.echo(header_line)
        click.echo("-" * len(header_line))

        # Data rows
        for row in rows:
            click.echo("  ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)))


console = RichConsole()


# ── CLI main command group ──


@click.group()
# i18n: user-facing
@click.option("--verbose", "-v", is_flag=True, help="verbose output")
# i18n: user-facing
@click.option("--log-file", default="", help="log file path")
@click.option(
    "--max-budget-usd",
    "max_budget_usd",
    default=0.0,
    type=float,
    help="token budget cap (USD), abort LLM calls beyond it (0=unlimited)",
)
@click.version_option(version=__version__, prog_name=__app_name__)
def cli(verbose: bool, log_file: str, max_budget_usd: float):

    """Fusion-Cowork — macOS native, fully local offline, zero-code desktop intelligent automation platform.

    Let the Mac do the work — local AI, fully automated desktop office.
    """
    level = logging.DEBUG if verbose else logging.INFO
    setup_logger(level=level, log_file=log_file, verbose=verbose)

    if max_budget_usd > 0:
        from .ai import get_budget_tracker

        get_budget_tracker(max_budget_usd=max_budget_usd, enforce=True)
        logger.info(f"Token budget cap enabled: ${max_budget_usd:.4f}")

    # Register tool name aliases (adopted from Squish tool_name_map.py pattern)
    _register_node_aliases()


def _register_node_aliases():
    """Register node alias mappings, enabling user-friendly Chinese name lookup.

    Integrated from Squish tool_name_map.py's VSCODE_TO_BACKEND mapping pattern.
    """
    for alias, target in NODE_NAME_ALIASES.items():
        NodeRegistry.register_alias(alias, target)


# ── Template commands ──


@cli.group()
def template():

    """Template management: list, search, run automation templates."""
    pass


@template.command("list")
# i18n: user-facing
@click.option("--category", "-c", default="", help="filter by category")
# i18n: user-facing
@click.option("--tag", "-t", default="", help="filter by tag")
# i18n: user-facing
@click.option("--search", "-s", default="", help="search keyword")
def list_templates(category: str, tag: str, search: str):

    """List available templates."""
    if search:
        templates = _get_template_mgr().search_templates(search)
    else:
        templates = _get_template_mgr().list_templates(category=category, tag=tag)

    if not templates:
        console.print_warning("no matching templates found")
        return

    console.print_header(f"Template list ({len(templates)})")

    # Group by category
    categories = {}
    for tpl in templates:
        cat = tpl.get("category", "Other")
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
        console.print_table(["ID", "Name", "Difficulty", "Est. Time"], rows)

    click.echo("\n💡 Run 'fusion-cowork template run <id>' to run a template")
    click.echo("💡 Run 'fusion-cowork template show <id>' for details")


@template.command("show")
@click.argument("template_id")
def show_template(template_id: str):

    """Show template details."""
    tpl = _get_template_mgr().get_template(template_id)
    if not tpl:
        console.print_error(f"template not found: {template_id}")
        return

    console.print_header(f"📄 {tpl.get('name', '')}")  # i18n: user-facing (template product data)
    click.echo(f"  Description: {tpl.get('description', '')}")
    click.echo(f"  Category: {tpl.get('category', '')}")
    click.echo(f"  Tags: {', '.join(tpl.get('tags', []))}")
    click.echo(f"  Difficulty: {tpl.get('difficulty', '')}")
    click.echo(f"  Estimated time: {tpl.get('estimated_time', '')}")
    if tpl.get("needs_ai"):
        click.echo("  🤖 Requires AI model (fusion-mlx)")

    # Show workflow nodes
    workflow = tpl.get("workflow", {})
    nodes = workflow.get("nodes", [])
    edges = workflow.get("edges", [])

    click.echo(f"\n  Workflow: {workflow.get('name', '')}")
    click.echo(f"  📋 Nodes ({len(nodes)}):")
    for node in nodes:
        params = node.get("config", {}).get("params", {})
        param_str = ", ".join(f"{k}={v}" for k, v in params.items() if not isinstance(v, (dict, list)))
        click.echo(f"    ├─ {node.get('id')}: {node.get('name')} ({param_str[:80]})")
    click.echo(f"  🔗 Edges ({len(edges)})")
    for edge in edges:
        click.echo(f"    ├─ {edge.get('source_id')} → {edge.get('target_id')}")

    click.echo(f"\n💡 Run: fusion-cowork template run {template_id}")


@template.command("run")
@click.argument("template_id")
# i18n: user-facing
@click.option("--dry-run", "-n", is_flag=True, help="dry-run mode")
# i18n: user-facing
@click.option("--params", "-p", default="", help="override params JSON (by node id)")
# i18n: user-facing
@click.option("--vars", "-v", "template_vars", default="", help="template-level vars JSON (substitutes ${param.X} in node configs)")
# i18n: user-facing
@click.option("--resume", "session_id", default="", help="resume: specify session_id, skip completed steps and continue")
def run_template(template_id: str, dry_run: bool, params: str, template_vars: str, session_id: str):

    """Run a template.

    --resume <session_id>: resume from the given session checkpoint (skips completed nodes).
    """
    asyncio.run(_async_run_template(template_id, dry_run, params, session_id, template_vars))


async def _async_run_template(
    template_id: str, dry_run: bool, params_json: str, resume_session_id: str = "", template_vars_json: str = ""
):
    console.print_header(f"🚀 Run template: {template_id}")

    # issue #287: node registration — the CLI process does not go through serve's import_all_nodes, so
    # governance templates and other ecosystem nodes (github_api/approval_gate/http_request) would report unknown node type
    from .nodes import import_all_nodes

    import_all_nodes()

    # issue #287: template-level vars — ${param.X} substituted at assembly time, no more hardcoded org/repo
    template_vars: Dict[str, Any] = {}
    if template_vars_json:
        try:
            parsed_vars = json.loads(template_vars_json)
            if not isinstance(parsed_vars, dict):
                console.print_error("template vars JSON must be an object")
                return
            template_vars = parsed_vars
        except json.JSONDecodeError as e:
            console.print_error(f"template vars JSON parse failed: {e}")
            return

    # Load template
    wf = _get_template_mgr().template_to_workflow(template_id, param_values=template_vars)
    if not wf:
        console.print_error(f"template not found: {template_id}")
        return

    # Override params
    if params_json:
        try:
            overrides = json.loads(params_json)
            for node_id, node_params in overrides.items():
                if node_id in wf.nodes:
                    wf.nodes[node_id].config.params.update(node_params)
                    console.print_info(f"overrode params for node {node_id}")
        except json.JSONDecodeError as e:
            console.print_error(f"params JSON parse failed: {e}")
            return

    # Validate
    errors = wf.validate()
    if errors:
        console.print_error("workflow validation failed:")
        for e in errors:
            console.print_error(f"  - {e}")
        return

    # Show workflow
    click.echo(f"\nWorkflow: {wf.name}")
    for nid, node in wf.nodes.items():
        console.print_info(f"  {node.icon} {node.config.label or node.display_name} ({node.name})")
    click.echo()

    if dry_run:
        console.print_success("dry-run mode, not executed")
        click.echo(f"  JSON: {wf.to_json()}")
        return

    # Resume from checkpoint: load completed-step snapshot (P1-2)
    resume_steps = None
    if resume_session_id:
        from fusion_cowork.engine.session import SessionStore

        store = SessionStore()
        payload = store.resume(resume_session_id)
        if not payload:
            console.print_error(f"resume failed, session not found: {resume_session_id}")
            return
        resume_steps = payload.get("steps_snapshot") or []
        console.print_info(f"resume: restored {len(resume_steps)} steps from session {resume_session_id}")

    # Execute workflow
    console.print_info("executing...")
    execution = await _get_engine().execute(wf, resume_steps=resume_steps)

    # Output result
    if execution.status == WorkflowStatus.SUCCESS:
        console.print_success(f"✅ executed successfully ({execution.total_time:.2f}s)")
    elif execution.status == WorkflowStatus.FAILED:
        console.print_error(f"❌ execution failed: {execution.error}")
    elif execution.status == WorkflowStatus.CANCELLED:
        console.print_warning("⏹️ cancelled")
    else:
        console.print_info(f"status: {execution.status.value}")

    # Show step summary
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


# ── AI commands ──


@cli.group()
def ai():
    """AI capabilities: natural language workflow generation, AI service management."""
    pass


@ai.command("generate")
@click.argument("prompt")
@click.option("--model", "-m", default="", help="fusion-mlx model name")
def generate_workflow(prompt: str, model: str):
    """Generate an automation workflow from a natural language description.

    Example: fusion-cowork ai generate "organize all PDFs on my desktop by topic"
    """
    asyncio.run(_async_generate(prompt, model))


@ai.command("cost")
def ai_cost():
    """Show the current session token budget and cost accounting."""
    from .ai import get_budget_tracker

    budget = get_budget_tracker()
    click.echo(json.dumps(budget.to_dict(), indent=2, ensure_ascii=False))


async def _async_generate(prompt: str, model: str):
    console.print_header("🤖 AI workflow generation")

    # Check whether fusion-mlx is available
    health = await _get_mlx_client().health()
    if not health:
        console.print_warning("⚠️  fusion-mlx not running, falling back to the local rule engine")
        console.print_info("   start: fusion-mlx or an MLX service")
        console.print_info("   template matching mode will be used")

    # Generate workflow
    generator = NLWorkflowGenerator(_get_mlx_client(), model=model)
    result = await generator.generate(prompt)

    error = result.get("error")
    if error:
        console.print_error(f"generation failed: {error}")
        if result.get("raw_output"):
            click.echo(f"\nraw output:\n{result['raw_output']}")
        return

    # Show generation result
    click.echo(f"\n📋 Workflow: {result.get('name', '')}")
    click.echo(f"   Description: {result.get('description', '')}")
    click.echo(f"   nodes ({len(result.get('nodes', []))}):")

    for node in result.get("nodes", []):
        params = node.get("config", {}).get("params", {})
        param_str = ", ".join(f"{k}={v}" for k, v in params.items() if not isinstance(v, (dict, list)))
        click.echo(f"     ├─ {node.get('id')}: {node.get('name')} ({param_str[:80]})")

    for edge in result.get("edges", []):
        click.echo(f"     └─ {edge.get('source_id')} → {edge.get('target_id')}")

    click.echo("\n💡 Run: fusion-cowork workflow run <saved workflow file>")


@ai.command("status")
def ai_status():
    """Check fusion-mlx and AI service status."""
    asyncio.run(_async_ai_status())


async def _async_ai_status():
    console.print_header("🔌 AI service status")

    # Check fusion-mlx
    mlx_ok = await _get_mlx_client().health()
    if mlx_ok:
        console.print_success("✅ fusion-mlx: running")
        try:
            models = await _get_mlx_client().list_models()
            if models:
                click.echo(f"   available models: {', '.join(m.get('id', m.get('model', '?')) for m in models[:5])}")
        except Exception:
            pass
    else:
        console.print_warning("⚠️  fusion-mlx: not running")
        click.echo("   start: please start the fusion-mlx service (engine 11434, gateway 11432)")

    # Check Fusion KB
    from .ai import KBClient

    kb = KBClient()
    kb_ok = await kb.health()
    if kb_ok:
        console.print_success("✅ Fusion-KB: running")
    else:
        console.print_info("ℹ️  Fusion-KB: not running (optional)")
    await kb.close()


# ── Workflow commands ──


@cli.group()
def workflow():

    """Workflow management: execute, inspect, manage."""
    pass


@workflow.command("run")
@click.argument("workflow_file", type=click.Path(exists=True))
# i18n: user-facing
@click.option("--dry-run", "-n", is_flag=True, help="dry-run mode")
# i18n: user-facing
@click.option("--resume", "session_id", default="", help="resume: specify session_id, skip completed steps and continue")
def run_workflow_file(workflow_file: str, dry_run: bool, session_id: str):

    """Load and execute a workflow from a JSON file.

    --resume <session_id>: resume from the given session checkpoint (skips completed nodes).
    """
    asyncio.run(_async_run_workflow_file(workflow_file, dry_run, session_id))


async def _async_run_workflow_file(workflow_file: str, dry_run: bool, resume_session_id: str = ""):
    console.print_header(f"🚀 Run workflow: {workflow_file}")

    try:
        with open(workflow_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.print_error(f"failed to load workflow file: {e}")
        return

    wf = Workflow.from_dict(data)

    errors = wf.validate()
    if errors:
        console.print_error("workflow validation failed:")
        for e in errors:
            console.print_error(f"  - {e}")
        return

    click.echo(f"\nWorkflow: {wf.name}")
    for nid, node in wf.nodes.items():
        console.print_info(f"  {node.icon} {node.config.label or node.display_name}")

    if dry_run:
        console.print_success("dry-run mode, not executed")
        return

    # Resume from checkpoint: load completed-step snapshot (P1-2)
    resume_steps = None
    if resume_session_id:
        from fusion_cowork.engine.session import SessionStore

        store = SessionStore()
        payload = store.resume(resume_session_id)
        if not payload:
            console.print_error(f"resume failed, session not found: {resume_session_id}")
            return
        resume_steps = payload.get("steps_snapshot") or []
        console.print_info(f"resume: restored {len(resume_steps)} steps from session {resume_session_id}")

    console.print_info("executing...")
    execution = await _get_engine().execute(wf, resume_steps=resume_steps)

    if execution.status == WorkflowStatus.SUCCESS:
        console.print_success(f"✅ executed successfully ({execution.total_time:.2f}s)")
    elif execution.status == WorkflowStatus.FAILED:
        console.print_error(f"❌ execution failed: {execution.error}")

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
    """List recent workflow executions."""
    executions = _get_engine().list_executions(limit=20)
    if not executions:
        console.print_info("no workflow execution records")
        return

    console.print_header("Recent executions")
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
    console.print_table(["ID", "Name", "Status", "Duration", "Time"], rows)


# ── Scheduled task commands ──


@cli.group()
def schedule():
    """Scheduled task management: create, list, and manage scheduled automations."""
    pass


@schedule.command("list")
def list_schedules():
    """List all scheduled tasks."""
    tasks = _get_scheduler().list_tasks()
    if not tasks:
        console.print_info("no scheduled tasks")
        console.print_info("use 'fusion-cowork schedule add' to add a scheduled task")
        return

    console.print_header("Scheduled task list")
    rows = []
    for task in tasks:
        status_icon = "✅" if task.status == TaskStatus.ACTIVE else "⏸️"
        next_run = time.strftime("%m-%d %H:%M", time.localtime(task.next_run)) if task.next_run else "-"
        rows.append(
            [
                task.id[:12],
                task.name[:25],
                f"{status_icon} {task.status.value}",
                f"{task.run_count} runs",
                next_run,
            ]
        )
    console.print_table(["ID", "Name", "Status", "Runs", "Next Run"], rows)


@schedule.command("add")
@click.option("--name", "-n", required=True, help="task name")
@click.option("--cron", "-c", required=True, help="cron expression (e.g. 0 21 * * *)")
@click.option("--template", "-t", required=True, help="template id to run")
@click.option("--description", "-d", default="", help="task description")
def add_schedule(name: str, cron: str, template: str, description: str):
    """Add a scheduled task."""
    task_id = _get_scheduler().add_cron_task(
        name=name,
        workflow_id=template,
        cron_expression=cron,
        executor=lambda: console.print_info(f"running scheduled task: {name}"),
        description=description or f"run template {template} on schedule",
    )
    console.print_success(f"✅ scheduled task added: {name} ({task_id[:12]})")
    console.print_info(f"   Cron: {cron}")
    console.print_info(f"   Template: {template}")


@schedule.command("remove")
@click.argument("task_id")
def remove_schedule(task_id: str):
    """Delete a scheduled task."""
    if _get_scheduler().remove_task(task_id):
        console.print_success(f"task deleted: {task_id}")
    else:
        console.print_error(f"task not found: {task_id}")


@schedule.command("start")
def start_scheduler():
    """Start the scheduler."""
    _get_scheduler().start()
    console.print_success("✅ scheduler started")


@schedule.command("stop")
def stop_scheduler():
    """Stop the scheduler."""
    _get_scheduler().shutdown()
    console.print_info("⏹️ scheduler stopped")


# ── System commands ──


@cli.group()
def system():
    """System tools: cleanup, monitoring, info."""
    pass


@system.command("info")
def system_info():
    """Show system info."""
    import platform

    console.print_header("🖥️  Fusion-Cowork system info")
    click.echo(f"  Version: {__version__}")
    click.echo(f"  Python: {sys.version.split()[0]}")
    click.echo(f"  Platform: {platform.system()} {platform.machine()}")
    click.echo(f"  Apple Silicon: {platform.machine() == 'arm64'}")
    click.echo(f"  Working dir: {os.getcwd()}")
    click.echo(f"  Data dir: {Path.home() / '.fusion-cowork'}")

    # Registered node stats
    nodes = NodeRegistry.list()
    click.echo(f"  Registered nodes: {len(nodes)}")
    cats = {}
    for n in nodes:
        cat = n.get("category", "other")
        cats[cat] = cats.get(cat, 0) + 1
    for cat, count in cats.items():
        click.echo(f"    ├─ {cat}: {count}")

    # Template stats
    templates = _get_template_mgr().list_templates()
    click.echo(f"  Built-in templates: {len(templates)}")


@system.command("scope")
@click.argument("folders", required=False)
@click.option("--enforce/--no-enforce", default=None, help="enable sandbox enforcement")
def system_scope(folders, enforce):
    """Show or set the authorized working folder (Scoped Folder) sandbox.

    \b
    fusion-cowork system scope                          # show current config
    fusion-cowork system scope ~/Cowork_Workspace       # set authorized folders
    fusion-cowork system scope ~/a,~/b --enforce        # multiple folders + enforcement
    """
    from .config_center import ConfigCenter
    from .security import get_scoped_folder_manager, reset_scoped_folder_manager

    cc = ConfigCenter.get_instance()
    if folders is None and enforce is None:
        cur = cc.get("workspace.scoped_folder", "")
        enf = cc.get("workspace.enforce_scope", False)
        console.print_info(f"Authorized folders: {cur or '(not set)'}")
        console.print_info(f"Sandbox enforcement: {'on' if enf else 'off'}")
        return
    if folders is not None:
        cc.set("workspace.scoped_folder", folders)
        console.print_result(f"authorized folders set: {folders}")
    if enforce is not None:
        cc.set("workspace.enforce_scope", enforce)
        console.print_result(f"sandbox enforcement {'enabled' if enforce else 'disabled'}")
    reset_scoped_folder_manager()
    mgr = get_scoped_folder_manager()
    console.print_info(f"effective: enforce={mgr.enforce} scopes={[str(s) for s in mgr.scopes]}")


@system.command("clean")
@click.option("--dry-run", "-n", is_flag=True, default=True, help="dry-run mode")
@click.option("--force", "-f", is_flag=True, help="force cleanup")
def system_clean(dry_run: bool, force: bool):
    """Quick-clean the Desktop and Downloads folders."""
    if force:
        dry_run = False
    asyncio.run(_async_system_clean(dry_run))


async def _async_system_clean(dry_run: bool):
    console.print_header(f"🧹 System clean ({'preview' if dry_run else 'execute'})")

    # Desktop clean
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
    console.print_info(f"  Desktop: {result.summary}")

    # Downloads organize
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
    console.print_info(f"  Downloads: {result.summary}")

    if not dry_run:
        console.print_success("✅ cleanup complete")
    else:
        console.print_info("💡 use --force or -f to actually clean")


# ── Browser commands ──


@cli.group()
def browser():
    """Embedded browser management: start, open, automate."""
    pass


@browser.command("start")
@click.option("--build", "-b", is_flag=True, help="build before start")
def browser_start(build: bool):
    """Start the Fusion embedded browser."""
    asyncio.run(_async_browser_start(build))


async def _async_browser_start(build: bool):
    from .nodes.browser import BrowserManager

    console.print_header("🌐 Fusion embedded browser")

    if build:
        console.print_info("building...")
        if BrowserManager.build():
            console.print_success("build succeeded")
        else:
            console.print_error("build failed")
            return

    if BrowserManager.launch():
        console.print_success("✅ browser started")
        console.print_info("   supports the fusion:// private protocol:")
        console.print_info("   ├─ fusion://start/ — start page")
        console.print_info("   ├─ fusion://kb/ — knowledge base management")
        console.print_info("   ├─ fusion://model/ — model management")
        console.print_info("   └─ fusion://automation/ — automation workflows")
    else:
        console.print_error("start failed, build first: fusion-cowork browser build")


@browser.command("build")
def browser_build():
    """Build the Fusion embedded browser."""
    asyncio.run(_async_browser_build())


async def _async_browser_build():
    from .nodes.browser import BrowserManager

    console.print_header("🔨 Build Fusion embedded browser")
    console.print_info("compiling the Swift native browser...")
    if BrowserManager.build():
        console.print_success("✅ build succeeded")
    else:
        console.print_error("❌ build failed")


@browser.command("open")
@click.argument("url")
def browser_open(url: str):
    """Open a URL in the browser."""
    asyncio.run(_async_browser_open(url))


async def _async_browser_open(url: str):
    from .nodes.browser import BrowserClient

    client = BrowserClient()
    try:
        _result = await client.open_url(url)
        console.print_success(f"opened: {url}")
    except Exception as e:
        console.print_error(f"open failed: {e}")
        console.print_info("start the browser first: fusion-cowork browser start")
    finally:
        await client.close()


@browser.command("status")
def browser_status():
    """Check browser status."""
    from .nodes.browser import BrowserClient

    client = BrowserClient()
    if client.is_running():
        console.print_success("✅ browser is running")
    else:
        console.print_warning("⚠️ browser not running")
        console.print_info("start: fusion-cowork browser start")


@browser.command("extract")
@click.argument("url", required=False)
@click.option("--to-file", "-o", default="", help="save to file")
def browser_extract(url: str = "", to_file: str = ""):
    """Extract web page text content."""
    asyncio.run(_async_browser_extract(url, to_file))


async def _async_browser_extract(url: str, to_file: str):
    from .engine import NodeConfig, NodeStatus
    from .nodes.browser import BrowserExtractNode

    node = BrowserExtractNode(config=NodeConfig(params={"url": url} if url else {}))
    result = await node.execute({"url": url} if url else {})

    if result.status == NodeStatus.SUCCESS:
        text = result.data.get("text", "")
        length = result.data.get("text_length", 0)
        console.print_success(f"extracted: {length} chars")
        if to_file:
            Path(to_file).write_text(text, encoding="utf-8")
            console.print_info(f"saved to: {to_file}")
        else:
            click.echo()
            click.echo(text[:2000])
            if len(text) > 2000:
                click.echo(f"\n... ({len(text)} chars total, use --to-file to save the full content)")
    else:
        console.print_error(f"extraction failed: {result.error}")


# ── MCP service ──


@cli.group("mcp")
def mcp():
    """MCP service management — for Claude Desktop/Code."""
    pass


@mcp.command("serve")
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "http", "streamable"]),
    default="stdio",
    help="transport mode (stdio/http/streamable)",
)
@click.option("--host", "-h", default="127.0.0.1", help="HTTP listen address")
@click.option("--port", "-p", default=11438, type=int, help="HTTP listen port")
def mcp_serve(transport: str, host: str, port: int):
    """Start MCP service — called by Claude Code / Claude Desktop."""
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
        console.print_info("MCP service started (stdio mode) — waiting for Claude Code to connect...")
        asyncio.run(server.serve_stdio())
    elif transport == "streamable":
        console.print_info(f"MCP service started (Streamable HTTP 2025-03-26 mode) — {host}:{port}")
        asyncio.run(server.serve_streamable_http(event_emitter=emitter))
    else:
        console.print_info(f"MCP service started (HTTP mode) — {host}:{port}")
        asyncio.run(server.serve_http(event_emitter=emitter))


# ── Desk RPC service ──


@cli.group("desk")
def desk():
    """Desk RPC service management — for Fusion-Studio GUI."""
    pass


@desk.command("rpc")
@click.option("--sock", "-s", default="/tmp/fusion-cowork.sock", help="UDS path")
@click.option("--http-port", default=11438, show_default=True, help="HTTP port (/rpc /health /mcp /sse); 0=disable HTTP")
def desk_rpc(sock: str, http_port: int):
    """Start Desk RPC service — called by Fusion-Studio.

    Starts both UDS (desk.*) and HTTP (/rpc /health /mcp /sse) channels by default.
    The HTTP channel serves the fusion-studio PluginBridge plugins/* integration panels (issue #48).
    --http-port 0 disables HTTP (UDS only).
    """
    _set_process_title("fusion-cowork desk-rpc")
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
        console.print_success(f"Desk RPC service started (UDS): {sock}")

        http_task = None
        if http_port > 0:
            http_task = asyncio.create_task(_serve_http(http_port))

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _request_stop():
            console.print_info("stop signal received, starting graceful shutdown...")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except (NotImplementedError, RuntimeError):
                pass

        console.print_info("waiting for Fusion-Studio to connect... (Ctrl+C to stop)")
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
            console.print_info("Desk RPC service stopped")

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
            console.print_success(f"Desk HTTP service started: {bind_host}:{port} (/rpc /health /mcp /sse)")
            await mcp.serve_http(event_emitter=rt["event_emitter"])
        except ImportError:
            console.print_warning("HTTP channel not started (missing [web] deps): pip install fusion-cowork[web]; UDS only")
        except Exception as e:
            console.print_warning(f"HTTP channel failed to start ({e}); UDS only")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print_info("Desk RPC service stopped")


@cli.group("web")
def web():
    """Union Web module (issue #94) — DAG workflow canvas UI backend."""


@web.command("serve")
@click.option("--port", default=11461, show_default=True, help="Web API HTTP port (port-registry: fusion-cowork-web)")
@click.option("--host", default=None, help="bind address (default FUSION_BIND_HOST or 127.0.0.1)")
def web_serve(port: int, host: str):
    """Start the Web API service — REST (nodes/workflows/runs) + web/dist static hosting.

    Auth: when FUSION_COWORK_WEB_API_KEY is set, all /api paths require
    Authorization: Bearer or x-api-key (injected by gateway/shell proxy); unset allows access
    (localhost binding by default).
    shell registration (issue #97): with FUSION_SHELL_GATEWAY_URL + FUSION_ADMIN_KEY
    both set, a background task registers web/manifest.json with the gateway and keeps it alive via heartbeat.
    Cross-device sync (#123): with FUSION_COWORK_CROSS_SYNC=1, starts CrossDeviceSync
    (W22 /api/deliver channel), configured via FUSION_COWORK_SYNC_HOST/PORT/TOKEN/CERT/KEY.
    """
    _set_process_title(f"fusion-cowork web :{port}")
    import asyncio

    bind_host = host or os.environ.get("FUSION_BIND_HOST", "127.0.0.1")
    try:
        import uvicorn

        from .server.shell_registry import shell_registration_enabled, shell_registry_loop
        from .server.sync import cross_sync_from_env
        from .server.web_api import auth_configured, create_web_app
    except ImportError:
        console.print_warning("Web API not started (missing [web] deps): pip install fusion-cowork[web]")
        return

    cross_sync = cross_sync_from_env()

    app = create_web_app(cross_sync=cross_sync)
    console.print_success(f"Cowork Web API started: {bind_host}:{port} (/api /health)")
    if not auth_configured():
        console.print_warning("FUSION_COWORK_WEB_API_KEY not set — auth open (localhost binding recommended only)")
    register_enabled = shell_registration_enabled()
    if not register_enabled:
        console.print_info("shell gateway registration not enabled (FUSION_SHELL_GATEWAY_URL / FUSION_ADMIN_KEY not set)")

    async def _serve():
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler:
            scheduler.start()
            console.print_info("task scheduler started (scheduled tasks active)")
        if cross_sync:
            try:
                await cross_sync.start()
                console.print_info(
                    f"cross-device sync started: {cross_sync.host}:{cross_sync.port} (device={cross_sync.device_id})"
                )
            except Exception as e:
                logger.warning(f"cross-device sync failed to start (/api/deliver unavailable): {e}")
        registry_task = asyncio.create_task(shell_registry_loop()) if register_enabled else None
        config = uvicorn.Config(app, host=bind_host, port=port, log_level="info")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if registry_task:
                registry_task.cancel()
            if cross_sync:
                try:
                    await cross_sync.stop()
                except Exception as e:
                    logger.warning(f"cross-device sync stop error: {e}")
            if scheduler:
                scheduler.shutdown(wait=False)
            space_store = getattr(app.state, "space_store", None)
            if space_store:
                await space_store.close()
                console.print_info("space store closed")

    asyncio.run(_serve())


# ── Team messaging (issue #190) ──


def _team_api(method: str, path: str, payload=None):
    # status/lifecycle go through daemon HTTP; roster and mailbox talk to SQLite directly (send/receive works without the daemon)
    import httpx

    base = os.environ.get("FUSION_COWORK_WEB_URL", "http://127.0.0.1:11461").rstrip("/")
    headers = {}
    key = os.environ.get("FUSION_COWORK_WEB_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.request(method, f"{base}{path}", json=payload, headers=headers)
    except httpx.HTTPError as e:
        console.print_warning(f"Web API unreachable ({base}): {e} — run fusion-cowork web serve first")
        return None
    if resp.status_code >= 400:
        console.print_error(f"Web API {method} {path} failed: {resp.status_code} {resp.text[:200]}")
        return None
    try:
        return resp.json()
    except ValueError:
        return None


@cli.group("team")
def team():
    """Team messaging — roster / durable mailbox / consumer pool (issue #190)."""


@team.command("register")
@click.argument("agent_id")
@click.option("--workflow", "workflow_id", required=True, help="workflow definition id to execute on message receipt")
@click.option("--name", default="", help="display name (default = agent_id)")
@click.option("--team", "team_name", default="", help="team the agent belongs to")
@click.option("--disable", is_flag=True, help="register as disabled (registered but not consuming)")
def team_register(agent_id, workflow_id, name, team_name, disable):
    """Register a resident consumer agent."""
    from .orchestrator.team_mailbox import CoworkRoster

    try:
        row = CoworkRoster().register(agent_id, workflow_id, name=name, team=team_name, enabled=not disable)
    except ValueError as e:
        console.print_error(str(e))
        return
    console.print_success(f"registered: {row['agent_id']} → workflow {row['workflow_id']} (enabled={bool(row['enabled'])})")


@team.command("roster")
def team_roster():
    """List the roster."""
    from .orchestrator.team_mailbox import CoworkRoster

    rows = CoworkRoster().list()
    if not rows:
        console.print_info("roster is empty — fusion-cowork team register <agent_id> --workflow <id>")
        return
    for r in rows:
        state = "enabled" if r["enabled"] else "disabled"
        console.print_result(f"{r['agent_id']:20s} {state}  workflow={r['workflow_id']}  team={r['team']}")


@team.command("remove")
@click.argument("agent_id")
def team_remove(agent_id):
    """Remove a consumer agent."""
    from .orchestrator.team_mailbox import CoworkRoster

    if CoworkRoster().remove(agent_id):
        console.print_success(f"removed: {agent_id}")
    else:
        console.print_warning(f"not registered: {agent_id}")


@team.command("send")
@click.option("--from", "sender", required=True, help="sender agent id")
@click.option("--to", "recipient", required=True, help="recipient agent id (cross-repo allowed: agent-studio agent)")
@click.option("--kind", default="message", show_default=True, help="message kind")
@click.option("--team", "team_name", default="", help="team label")
@click.argument("body")
def team_send(sender, recipient, kind, team_name, body):
    """Deliver a durable message (persisted to SQLite, at-least-once delivery)."""
    from .orchestrator.team_mailbox import TeamMailbox

    try:
        msg = TeamMailbox().send(sender, recipient, body, kind=kind, team=team_name)
    except ValueError as e:
        console.print_error(str(e))
        return
    console.print_success(f"delivered: {msg['message_id']} {sender} → {recipient}")


@team.command("inbox")
@click.argument("agent_id")
@click.option("--all", "show_all", is_flag=True, help="include acknowledged history")
@click.option("--limit", default=50, show_default=True, help="max entries")
def team_inbox(agent_id, show_all, limit):
    """Show an agent's mailbox."""
    from .orchestrator.team_mailbox import TeamMailbox

    messages = TeamMailbox().inbox(agent_id, unread_only=not show_all, limit=limit)
    if not messages:
        console.print_info(f"{agent_id} has no unread messages")
        return
    for m in messages:
        acked = "read" if m.get("acked_at") else "unread"
        console.print_result(f"{m['message_id']}  {m['sender']:20s} {acked}  {m['body'][:80]}")


@team.command("status")
def team_status():
    """Consumer pool status — live consumers / backlog depth / metrics."""
    data = _team_api("GET", "/api/team/status")
    if data is None:
        return
    console.print_result(f"consumer pool: {'running' if data.get('running') else 'stopped'}  poll {data.get('poll_interval')}s")
    console.print_result(f"live consumers: {data.get('consumers', 0)}")
    metrics = data.get("metrics") or {}
    console.print_result(
        f"backlog {metrics.get('mailbox_depth', 0)} / oldest unacked {metrics.get('oldest_unacked_age_s', 0)}s "
        f"/ consumed {metrics.get('consumed', 0)} / replies {metrics.get('replies_sent', 0)} "
        f"/ dispatch errors {metrics.get('dispatch_errors', 0)}"
    )
    for a in data.get("agents") or []:
        console.print_result(
            f"  {a['agent_id']:20s} workflow={a['workflow_id']:20s} backlog={a['inbox_depth']} "
            f"consumed={a['consumed']} replies={a['replies_sent']}"
        )


@team.command("start")
def team_start():
    """Start the consumer pool (daemon resident task)."""
    data = _team_api("POST", "/api/team/pool/start")
    if data is not None:
        console.print_success(f"consumer pool: running={data.get('running')}")


@team.command("stop")
def team_stop():
    """Stop the consumer pool."""
    data = _team_api("POST", "/api/team/pool/stop")
    if data is not None:
        console.print_success(f"consumer pool: running={data.get('running')}")


# ── Session management ──


@cli.group("session")
def session():
    """Session management — query/resume/fork workflow execution sessions."""
    pass


@session.command("list")
@click.option("--status", "-s", default=None, help="filter by status (created/running/completed/failed)")
@click.option("--limit", "-n", default=20, help="max results")
def session_list(status, limit):
    """List sessions."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    sessions = store.list_sessions(status=status, limit=limit)
    if not sessions:
        console.print_info("no session records")
        return
    for s in sessions:
        console.print_result(f"{s.id}  {s.status:10s}  {s.workflow_name:20s}  {s.created_at:.0f}")


@session.command("show")
@click.argument("session_id")
def session_show(session_id):
    """Show session details."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    s = store.get(session_id)
    if not s:
        console.print_error(f"session not found: {session_id}")
        return
    console.print_result(json.dumps(store.to_dict(s), indent=2, ensure_ascii=False))


@session.command("resumable")
@click.option("--limit", "-n", default=20, help="max results")
def session_resumable(limit):
    """List resumable sessions (paused/failed/running)."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    sessions = store.list_resumable(limit=limit)
    if not sessions:
        console.print_info("no resumable sessions")
        return
    for s in sessions:
        console.print_result(f"{s.id}  {s.status:10s}  {s.workflow_name:20s}  steps={len(s.steps_snapshot)}")


@session.command("resume")
@click.argument("session_id")
def session_resume(session_id):
    """Resume a session — load snapshot for replay/continuation."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    payload = store.resume(session_id)
    if not payload:
        console.print_error(f"resume failed: {session_id}")
        return
    console.print_result(json.dumps(payload, indent=2, ensure_ascii=False))


@session.command("fork")
@click.argument("session_id")
@click.option("--from-step", "-f", default=0, type=int, help="step to fork from (0=all)")
def session_fork(session_id, from_step):
    """Fork a session."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    forked = store.fork(session_id, from_step=from_step)
    if not forked:
        console.print_error(f"fork failed: {session_id}")
        return
    console.print_result(f"forked: {forked.id} (from {session_id}, step={from_step})")


@session.command("delete")
@click.argument("session_id")
# i18n: user-facing
@click.confirmation_option(prompt="确认删除该会话?")
def session_delete(session_id):
    """Delete a session."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    if store.delete(session_id):
        console.print_result(f"deleted: {session_id}")
    else:
        console.print_error(f"delete failed: {session_id}")


@session.command("cleanup")
@click.option("--days", "-d", default=30, type=int, help="clean up sessions expired this many days ago")
def session_cleanup(days):
    """Clean up expired sessions."""
    from fusion_cowork.engine.session import SessionStore

    store = SessionStore()
    count = store.cleanup_expired(expire_days=days)
    console.print_result(f"cleaned {count} expired sessions")


# ── Permission management ──


@cli.group("permission")
def permission():
    """Permission management — view/approve/deny tool permissions."""
    pass


@permission.command("level")
@click.argument("level", type=click.Choice(["manual", "auto", "plan", "bypass"]))
def permission_level(level):
    """Set the permission level."""
    from fusion_cowork.engine.permission import PermissionLevel, PermissionManager

    pm = PermissionManager()
    pm.level = PermissionLevel(level)
    pm.save()
    console.print_result(f"permission level set to: {level}")


@permission.command("approve")
@click.argument("tool_name")
@click.option("--scope", "-s", default="*", help="permission scope (e.g. 'command:git *')")
def permission_approve(tool_name, scope):
    """Approve a tool permission."""
    from fusion_cowork.engine.permission import PermissionManager

    pm = PermissionManager()
    pm.load()
    pm.approve(tool_name, scope=scope)
    pm.save()
    console.print_result(f"approved: {tool_name} (scope={scope})")


@permission.command("deny")
@click.argument("tool_name")
@click.option("--scope", "-s", default="*", help="permission scope")
def permission_deny(tool_name, scope):
    """Deny a tool permission."""
    from fusion_cowork.engine.permission import PermissionManager

    pm = PermissionManager()
    pm.load()
    pm.deny(tool_name, scope=scope)
    pm.save()
    console.print_result(f"denied: {tool_name} (scope={scope})")


@permission.command("list")
def permission_list():
    """List permission rules."""
    from fusion_cowork.engine.permission import PermissionManager

    pm = PermissionManager()
    pm.load()
    data = pm.to_dict()
    console.print_result(json.dumps(data, indent=2, ensure_ascii=False))


# ── Plugin management commands ──


@cli.group("plugin")
def plugin():
    """Plugin management — discover/install/load/unload plugins."""
    pass


@plugin.command("list")
def plugin_list():
    """List discovered and loaded plugins."""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    discovered = loader.discover()
    if not discovered:
        console.print_info("no plugins found")
        console.print_info("plugin directory: ~/.fusion-cowork/plugins/")
        return
    console.print_header(f"Plugin list ({len(discovered)})")
    rows = []
    for name, manifest in discovered.items():
        loaded = "✅" if name in loader._loaded else "⏸️"
        nodes = ", ".join(manifest.nodes[:3])
        if len(manifest.nodes) > 3:
            nodes += f" +{len(manifest.nodes) - 3}"
        rows.append([name, manifest.version, loaded, manifest.author or "-", nodes])
    console.print_table(["Name", "Version", "Status", "Author", "Nodes"], rows)


@plugin.command("install")
@click.argument("path")
def plugin_install(path: str):
    """Install a plugin from a directory, zip, or URL (http(s)://...zip)."""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        ok = loader.install(path)
        if ok:
            console.print_success(f"plugin installed: {path}")
        else:
            console.print_error(f"install failed: {path}")
    except Exception as e:
        console.print_error(f"install failed: {e}")


@plugin.command("uninstall")
@click.argument("name")
# i18n: user-facing
@click.confirmation_option(prompt="确认卸载该插件?")
def plugin_uninstall(name: str):
    """Uninstall a plugin."""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        loader.uninstall(name)
        console.print_success(f"plugin uninstalled: {name}")
    except Exception as e:
        console.print_error(f"uninstall failed: {e}")


@plugin.command("import-claude-desktop")
@click.option(
    "--config",
    "config_path",
    default="",
    help="path to claude_desktop_config.json (default ~/Library/Application Support/Claude/)",
)
def plugin_import_claude_desktop(config_path: str):
    """Import MCP servers from Claude Desktop config as plugins."""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        imported = loader.import_from_claude_desktop(config_path)
        if imported:
            console.print_success(f"imported {len(imported)} MCP servers from Claude Desktop:")
            for name in imported:
                console.print_info(f"  - {name}")
        else:
            console.print_warning("no importable MCP servers found (config missing or mcpServers empty)")
    except Exception as e:
        console.print_error(f"import failed: {e}")


@plugin.command("load")
@click.argument("name")
def plugin_load(name: str):
    """Load a plugin (register its nodes)."""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        loader.load(name)
        console.print_success(f"plugin loaded: {name}")
    except Exception as e:
        console.print_error(f"load failed: {e}")


@plugin.command("unload")
@click.argument("name")
def plugin_unload(name: str):
    """Unload a plugin (unregister its nodes)."""
    from .plugins import get_plugin_loader

    loader = get_plugin_loader()
    try:
        loader.unload(name)
        console.print_success(f"plugin nodes unloaded: {name}")
    except Exception as e:
        console.print_error(f"uninstall failed: {e}")


# ── Skill commands ──


@cli.group("skill")
def skill():
    """Skill management — list/search/execute skills."""
    pass


def _get_skill_registry():
    from .skills import SkillRegistry, register_builtin_skills, register_user_packs

    registry = SkillRegistry()
    register_builtin_skills(registry)
    try:
        register_user_packs(registry)
    except Exception as e:
        console.print_warning(f"failed to load user skill packs: {e}")
    return registry


@skill.command("list")
def skill_list():
    """List available skills."""
    registry = _get_skill_registry()
    skills = registry.list_skills()
    if not skills:
        console.print_info("no skills available")
        return
    console.print_header(f"Skill list ({len(skills)})")
    rows = []
    for s in skills:
        aliases = ", ".join(s.aliases) if s.aliases else "-"
        rows.append([s.name, s.description[:40], s.category or "-", aliases])
    console.print_table(["Name", "Description", "Category", "Aliases"], rows)


@skill.command("run")
@click.argument("name")
@click.option("--params", "-p", default="{}", help="params JSON")
def skill_run(name: str, params: str):
    """Execute a skill."""
    registry = _get_skill_registry()
    try:
        kwargs = json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        console.print_error(f"params JSON parse failed: {e}")
        return
    try:
        result = asyncio.run(registry.execute(name, **kwargs))
        console.print_success(f"skill executed successfully: {name}")
        if result:
            click.echo(
                json.dumps(result, indent=2, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
            )
    except Exception as e:
        console.print_error(f"skill execution failed: {e}")


@skill.command("search")
@click.argument("query")
def skill_search(query: str):
    """Search skills."""
    registry = _get_skill_registry()
    results = registry.search(query)
    if not results:
        console.print_info(f"no skills matching '{query}'")
        return
    for s in results:
        click.echo(f"  {s.name}: {s.description}")


@skill.command("create")
@click.option("--name", required=True, help="skill name (e.g. /my-skill)")
@click.option("--description", "-d", default="", help="skill description")
@click.option("--category", "-c", default="custom", help="category")
@click.option("--type", "stype", type=click.Choice(["workflow", "node", "script"]), default="node", help="handler type")
@click.option("--template-id", default="", help="workflow type: template id")
@click.option("--node", default="", help="node type: node name")
@click.option("--params", default="{}", help="node type: params JSON")
@click.option("--script", default="", help="script type: script filename (relative to pack dir)")
@click.option("--aliases", default="", help="aliases, comma-separated")
def skill_create(name, description, category, stype, template_id, node, params, script, aliases):
    """Create a user-authored skill pack (persisted to ~/.fusion-cowork/skills)."""
    from .skills import SkillPack, save_skill_pack

    try:
        params_dict = json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        console.print_error(f"params JSON parse failed: {e}")
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
        console.print_success(f"skill pack created: {name} -> {pdir}")
    except Exception as e:
        console.print_error(f"skill pack creation failed: {e}")


@skill.command("list-packs")
def skill_list_packs():
    """List user skill packs on disk."""
    from .skills import list_skill_packs

    packs = list_skill_packs()
    if not packs:
        console.print_info("no user skill packs")
        return
    console.print_header(f"User skill packs ({len(packs)})")
    rows = []
    for p in packs:
        rows.append([p.name, p.type, p.description[:40], ", ".join(p.aliases) or "-"])
    console.print_table(["Name", "Type", "Description", "Aliases"], rows)


@skill.command("packages")
def skill_packages():
    """List markdown skill packages (builtin + user directory)."""
    from .skills import get_package_registry

    packages = get_package_registry(reload=True).list_packages()
    if not packages:
        console.print_info("no markdown skill packages")
        return
    console.print_header(f"markdown skill packages ({len(packages)})")
    rows = [[p.name, p.title or "-", p.category, ", ".join(x.name for x in p.params) or "-"] for p in packages]
    console.print_table(["Name", "Title", "Category", "Params"], rows)


@skill.command("run-package")
@click.argument("name")
@click.option("--params", default="", help="params JSON, e.g. '{\"days\": 7}'")
def skill_run_package(name: str, params: str):
    """Execute a markdown skill package."""
    from .skills import get_package_registry

    try:
        values = json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        console.print_error(f"params JSON parse failed: {e}")
        return
    result = asyncio.run(get_package_registry().run(name, values))
    if result.get("error"):
        console.print_error(f"execution failed: {result['error']}")
        for err in result.get("errors", []):
            click.echo(f"  - {err}")
        return
    console.print_success(result.get("summary", f"completed: {name}"))
    click.echo(json.dumps(result.get("data", {}), ensure_ascii=False, indent=2))


@skill.command("delete-pack")
@click.argument("name")
def skill_delete_pack(name: str):
    """Delete a user skill pack."""
    from .skills import delete_skill_pack

    if delete_skill_pack(name):
        console.print_success(f"skill pack deleted: {name}")
    else:
        console.print_error(f"skill pack not found: {name}")


# ── CDP commands ──


@cli.group("cdp")
def cdp():
    """Chrome DevTools Protocol — remote control Chrome."""
    pass


@cdp.command("navigate")
@click.argument("url")
@click.option("--host", default="127.0.0.1", help="Chrome remote debugging address")
@click.option("--port", default=9222, type=int, help="Chrome remote debugging port")
def cdp_navigate(url: str, host: str, port: int):
    """Navigate to the given URL."""
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
    """Get the page accessibility snapshot."""
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
    """Click a page element (backendNodeId)."""
    from .engine import NodeConfig
    from .nodes.browser import CDPClickNode

    node = CDPClickNode(config=NodeConfig(params={"backend_node_id": backend_node_id, "host": host, "port": port}))
    result = asyncio.run(node.execute({"backend_node_id": backend_node_id}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.summary)


@cdp.command("fill")
@click.option("--selector", "-s", required=True, help="CSS selector")
@click.option("--value", "-v", required=True, help="value to fill")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_fill(selector: str, value: str, host: str, port: int):
    """Fill a form field."""
    from .engine import NodeConfig
    from .nodes.browser import CDPFillNode

    node = CDPFillNode(config=NodeConfig(params={"selector": selector, "value": value, "host": host, "port": port}))
    result = asyncio.run(node.execute({"selector": selector, "value": value}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.summary)


@cdp.command("screenshot")
@click.option("--save", "-o", default="", help="save path")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=9222, type=int)
def cdp_screenshot(save: str, host: str, port: int):
    """Take a page screenshot."""
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
    """Execute JavaScript in the page."""
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


# ── Computer Use commands ──


@cli.group("computer-use")
def computer_use():
    """Computer Use — mouse/keyboard control + AI loop."""
    pass


@computer_use.command("move")
@click.argument("x", type=int)
@click.argument("y", type=int)
@click.option("--duration", "-d", default=0, type=float, help="move duration (seconds)")
def cu_move(x: int, y: int, duration: float):
    """Move the mouse to the given coordinates."""
    from .nodes.macos.input_nodes import MouseMoveNode

    node = MouseMoveNode(config=NodeConfig(params={"x": x, "y": y, "duration": duration}))
    result = asyncio.run(node.execute({"x": x, "y": y}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("click")
@click.option("--x", type=int, default=None, help="X coordinate")
@click.option("--y", type=int, default=None, help="Y coordinate")
@click.option("--button", "-b", type=click.Choice(["left", "right", "middle"]), default="left")
@click.option("--count", "-c", type=int, default=1, help="click count")
def cu_click(x, y, button, count):
    """Mouse click."""
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
@click.option("--delay", "-d", default=0, type=float, help="delay between characters (seconds)")
def cu_type(text: str, delay: float):
    """Type text via keyboard."""
    from .nodes.macos.input_nodes import KeyboardTypeNode

    node = KeyboardTypeNode(config=NodeConfig(params={"text": text, "delay": delay}))
    result = asyncio.run(node.execute({"text": text}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("shortcut")
@click.argument("key")
@click.option("--modifiers", "-m", multiple=True, help="modifiers (cmd/shift/ctrl/alt)")
def cu_shortcut(key: str, modifiers):
    """Keyboard shortcut."""
    from .nodes.macos.input_nodes import KeyboardShortcutNode

    node = KeyboardShortcutNode(config=NodeConfig(params={"key": key, "modifiers": list(modifiers)}))
    result = asyncio.run(node.execute({"key": key}))
    if result.status.value == "success":
        console.print_success(result.summary)
    else:
        console.print_error(result.error or result.summary)


@computer_use.command("run")
@click.argument("task")
@click.option("--max-steps", default=10, type=int, help="max loop steps")
@click.option("--step-delay", default=1.0, type=float, help="per-step interval (seconds)")
@click.option("--model", default="default", help="fusion-mlx model name")
def cu_run(task: str, max_steps: int, step_delay: float, model: str):
    """Run a Computer Use loop task."""
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


# ── Remote control commands ──


@cli.group("remote")
def remote():
    """Remote control — join a fusion-cowork session over WebSocket."""
    pass


@remote.command("serve")
@click.option("--host", "-h", default="127.0.0.1", help="listen address")
@click.option("--port", "-p", default=11439, type=int, help="listen port")
@click.option("--token", "-t", default="", help="auth token (empty = no auth)")
@click.option("--tls-cert", default="", help="TLS cert path (PEM), enables wss://")
@click.option("--tls-key", default="", help="TLS key path (PEM)")
def remote_serve(host: str, port: int, token: str, tls_cert: str, tls_key: str):
    """Start the remote control server (optional TLS)."""
    _set_process_title("fusion-cowork remote")
    from .server.remote import RemoteControlServer

    server = RemoteControlServer(host=host, port=port, token=token or None, tls_cert=tls_cert, tls_key=tls_key)

    async def _run():
        await server.start()
        scheme = "wss" if (tls_cert and tls_key) else "ws"
        console.print_success(f"remote control service started: {scheme}://{host}:{port}/control")
        if token:
            _t = str(token)
            _shown = f"{_t[:4]}***{_t[-4:]}" if len(_t) > 8 else "(set)"
            console.print_info(f"auth token: {_shown}")
        if tls_cert and tls_key:
            console.print_info(f"TLS enabled: cert={tls_cert}")
        console.print_info("waiting for remote connections... (Ctrl+C to stop)")
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
        console.print_info("remote control service stopped")


@remote.command("connect")
@click.argument("url", default="ws://127.0.0.1:11439/control")
@click.option("--token", "-t", default="", help="auth token")
def remote_connect(url: str, token: str):
    """Connect to a remote fusion-cowork instance."""
    from .server.remote import RemoteControlClient

    client = RemoteControlClient(token=token or None)

    async def _run():
        await client.connect(url)
        console.print_success(f"connected: {url}")
        try:
            status = await client.get_status()
            click.echo(json.dumps(status, indent=2, ensure_ascii=False))
        finally:
            await client.close()

    try:
        asyncio.run(_run())
    except Exception as e:
        console.print_error(f"connection failed: {e}")


@remote.command("submit")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.option("--url", default="ws://127.0.0.1:11439/control", help="remote service URL")
@click.option("--token", "-t", default="", help="auth token")
def remote_submit(workflow_file: str, url: str, token: str):
    """Submit a workflow to a remote fusion-cowork for execution."""
    from .server.remote import RemoteControlClient

    client = RemoteControlClient(token=token or None)

    async def _run():
        await client.connect(url)
        try:
            with open(workflow_file, encoding="utf-8") as f:
                workflow = json.load(f)
            task_id = await client.submit_workflow(workflow)
            console.print_success(f"workflow submitted: {task_id}")
            click.echo(f"Task ID: {task_id}")
        finally:
            await client.close()

    try:
        asyncio.run(_run())
    except Exception as e:
        console.print_error(f"submit failed: {e}")


# ── Worktree git isolation commands ──


@cli.group("worktree")
def worktree():
    """Worktree — isolated git worktree execution."""
    pass


@worktree.command("create")
@click.argument("name")
@click.option("--branch", "-b", default="", help="checkout an existing branch (empty = create worktree/<name>)")
@click.option("--repo-root", "-r", default="", help="git repo root (empty = auto-detect)")
def worktree_create(name: str, branch: str, repo_root: str):
    """Create an isolated worktree."""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    wt = mgr.create(name, branch=branch)
    if not wt:
        console.print_error(f"worktree creation failed: {name}")
        return
    console.print_success(f"worktree created: {name}")
    click.echo(json.dumps(wt.to_dict(), indent=2, ensure_ascii=False))


@worktree.command("list")
@click.option("--repo-root", "-r", default="", help="git repo root (empty = auto-detect)")
def worktree_list(repo_root: str):
    """List all worktrees."""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    wts = mgr.list_worktrees()
    console.print_header(f"Worktree list ({len(wts)})")
    rows = [[w.path, w.branch, w.head[:8]] for w in wts]
    console.print_table(["Path", "Branch", "HEAD"], rows)


@worktree.command("exec")
@click.argument("name")
@click.argument("command")
@click.option("--repo-root", "-r", default="", help="git repo root (empty = auto-detect)")
@click.option("--timeout", "-t", default=30, type=int, help="timeout (seconds)")
def worktree_exec(name: str, command: str, repo_root: str, timeout: int):
    """Execute a command inside an isolated worktree."""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    # register an existing worktree matched by name
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
        console.print_error(f"execution failed: {e}")


@worktree.command("remove")
@click.argument("name")
@click.option("--repo-root", "-r", default="", help="git repo root (empty = auto-detect)")
@click.option("--force", "-f", is_flag=True, help="force delete")
def worktree_remove(name: str, repo_root: str, force: bool):
    """Delete an isolated worktree."""
    from .utils.worktree import WorktreeManager

    mgr = WorktreeManager(repo_root=repo_root)
    if not mgr.get(name):
        for w in mgr.list_worktrees():
            if Path(w.path).name == name:
                mgr._worktrees[name] = w
                break
    if mgr.remove(name, force=force):
        console.print_success(f"worktree deleted: {name}")
    else:
        console.print_error(f"worktree delete failed: {name}")


# ── LSP code intelligence commands ──


@cli.group("lsp")
def lsp():
    """LSP — code intelligence (definition/references/hover/completion)."""
    pass


def _lsp_query_common(action: str, path: str, line: int, character: int, root: str):
    from .code import query as lsp_query

    try:
        result = asyncio.run(lsp_query(action, path, line, character, root=root))
    except Exception as e:
        console.print_error(f"LSP query failed: {e}")
        return
    if "error" in result:
        console.print_error(result["error"])
        return
    console.print_success(f"LSP {action} result:")
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@lsp.command("definition")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace root (empty = file directory)")
def lsp_definition(path: str, line: int, character: int, root: str):
    """Query definition locations."""
    _lsp_query_common("definition", path, line, character, root)


@lsp.command("references")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace root (empty = file directory)")
def lsp_references(path: str, line: int, character: int, root: str):
    """Query reference locations."""
    _lsp_query_common("references", path, line, character, root)


@lsp.command("hover")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace root (empty = file directory)")
def lsp_hover(path: str, line: int, character: int, root: str):
    """Query hover info."""
    _lsp_query_common("hover", path, line, character, root)


@lsp.command("completion")
@click.argument("path")
@click.argument("line", type=int)
@click.argument("character", type=int)
@click.option("--root", "-r", default="", help="LSP workspace root (empty = file directory)")
def lsp_completion(path: str, line: int, character: int, root: str):
    """Query completion items."""
    _lsp_query_common("completion", path, line, character, root)


# ── Mobile push commands ──


@cli.group("push")
def push():
    """Mobile push — Bark/ntfy cross-platform notifications (falls back locally when unconfigured)."""
    pass


@push.command("send")
@click.argument("title")
@click.argument("message")
@click.option("--provider", "-p", type=click.Choice(["auto", "bark", "ntfy", "local"]), default="auto", help="push provider")
@click.option("--url", "-u", default="", help="Bark/ntfy server URL")
@click.option("--token", "-t", default="", help="Bark device key / ntfy topic")
@click.option("--sound", default="", help="sound (Bark)")
@click.option("--priority", default="", help="priority (ntfy 1-5)")
@click.option("--group", default="", help="group")
def push_send(title: str, message: str, provider: str, url: str, token: str, sound: str, priority: str, group: str):
    """Send a mobile push notification."""
    from .notification import push as do_push

    result = asyncio.run(
        do_push(title, message, provider=provider, url=url, token=token, sound=sound, priority=priority, group=group)
    )
    if result.success:
        tag = " (degraded to local)" if result.degraded else ""
        console.print_success(f"✅ push succeeded via {result.provider}{tag}")
    else:
        console.print_error(f"❌ push failed: {result.error}")
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
@click.option("--bark-url", default=None, help="Bark server URL (e.g. https://api.day.app)")
@click.option("--ntfy-url", default=None, help="ntfy server URL (e.g. https://ntfy.sh)")
@click.option("--ntfy-token", default=None, help="ntfy topic / token")
@click.option("--sound", default=None, help="default sound")
@click.option("--priority", default=None, help="default priority")
def push_config(bark_url, ntfy_url, ntfy_token, sound, priority):
    """Show or set push provider config (persisted to ConfigCenter)."""
    from .config_center import ConfigCenter

    cc = ConfigCenter.get_instance()
    if all(v is None for v in (bark_url, ntfy_url, ntfy_token, sound, priority)):
        console.print_info(f"Bark URL: {cc.get('push.bark_url', '(not set)')}")
        console.print_info(f"ntfy URL: {cc.get('push.ntfy_url', '(not set)')}")
        # Stage 2: masked token display (first4/last4, middle *)
        _ntfy = cc.get("push.ntfy_token", "") or ""
        _shown = f"{_ntfy[:4]}***{_ntfy[-4:]}" if len(_ntfy) > 8 else ("(set)" if _ntfy else "(not set)")
        console.print_info(f"ntfy Token: {_shown}")
        console.print_info(f"sound: {cc.get('push.sound', '(not set)')}")
        console.print_info(f"priority: {cc.get('push.priority', '(not set)')}")
        return
    if bark_url is not None:
        cc.set("push.bark_url", bark_url)
        console.print_result(f"Bark URL set: {bark_url}")
    if ntfy_url is not None:
        cc.set("push.ntfy_url", ntfy_url)
        console.print_result(f"ntfy URL set: {ntfy_url}")
    if ntfy_token is not None:
        cc.set("push.ntfy_token", ntfy_token)
        _nt = str(ntfy_token)
        _nshown = f"{_nt[:4]}***{_nt[-4:]}" if len(_nt) > 8 else "(set)"
        console.print_result(f"ntfy Token set: {_nshown}")
    if sound is not None:
        cc.set("push.sound", sound)
    if priority is not None:
        cc.set("push.priority", priority)


# ── Deep research commands ──


@cli.group("research")
def research():
    """Deep research — multi-agent research (plan -> search -> synthesize)."""
    pass


@research.command("run")
@click.argument("question")
@click.option("--depth", "-d", type=int, default=3, help="number of sub-questions (planning depth)")
@click.option("--max-sources", "-s", type=int, default=3, help="max sources per sub-question")
@click.option("--model", "-m", default="", help="fusion-mlx model (empty = auto)")
@click.option("--output", "-o", default="", help="output Markdown file path (empty = print)")
def research_run(question: str, depth: int, max_sources: int, model: str, output: str):
    """Run deep research, producing a cited research report."""
    from .research import run_deep_research

    console.print_header(f"🔍 Deep research: {question}")
    report = asyncio.run(
        run_deep_research(question, depth=depth, max_sources=max_sources, model=model, mlx_client=_get_mlx_client())
    )
    if report.error and not report.findings:
        console.print_error(f"❌ research failed: {report.error}")
        return
    if report.degraded:
        console.print_warning("⚠️  LLM unavailable, degraded mode (raw findings summary)")
    console.print_info(f"sub-questions: {len(report.sub_questions)} | sources: {report.sources_used}")

    md = _format_research_report(report)
    if output:
        Path(output).write_text(md, encoding="utf-8")
        console.print_success(f"✅ report written: {output}")
    else:
        click.echo(md)


def _format_research_report(report) -> str:
    lines = [f"# Deep Research Report: {report.question}", ""]
    if report.sub_questions:
        lines.append("## Research Sub-questions")
        for i, sq in enumerate(report.sub_questions, 1):
            lines.append(f"{i}. {sq.question}")
            if sq.rationale:
                lines.append(f"   _{sq.rationale}_")
        lines.append("")
    lines.append("## Research Report")
    lines.append("")
    lines.append(report.synthesis)
    lines.append("")
    if report.findings:
        lines.append("## Sources")
        for i, f in enumerate(report.findings, 1):
            lines.append(f"[{i}] {f.title} — {f.url}")
    return "\n".join(lines)


# ── Multi-agent code review commands ──


@cli.group("review")
def review():
    """UltraReview — multi-agent code review (security/correctness/style/tests)."""
    pass


@review.command("run")
@click.argument("paths", nargs=-1)
@click.option("--model", "-m", default="", help="fusion-mlx model (empty = auto)")
@click.option("--lens", "-l", multiple=True, help="review lenses (security/correctness/style/tests, repeatable)")
@click.option("--diff", "use_diff", is_flag=True, help="review git diff changed files (ignores paths)")
@click.option("--output", "-o", default="", help="output Markdown file path (empty = print)")
def review_run(paths, model: str, lens, use_diff: bool, output: str):
    """Run multi-lens code review."""
    from .review import run_ultra_review
    from .review.ultra_review import collect_git_diff_files

    targets = list(paths)
    if use_diff:
        targets = collect_git_diff_files()
        if not targets:
            console.print_warning("⚠️  git diff has no changed .py files")
            return
        console.print_info(f"reviewing git diff changes: {len(targets)} files")
    if not targets:
        console.print_error("❌ no review targets specified (paths or --diff)")
        return

    lenses = list(lens) if lens else None
    console.print_header(f"🔎 UltraReview: {len(targets)} targets")
    report = asyncio.run(run_ultra_review(targets, model=model, mlx_client=_get_mlx_client(), lenses=lenses))
    if report.error and not report.files_reviewed:
        console.print_error(f"❌ review failed: {report.error}")
        return
    if report.degraded:
        console.print_warning("⚠️  LLM unavailable, degraded mode (file list)")
    console.print_info(f"files reviewed: {len(report.files_reviewed)} | findings: {len(report.findings)}")

    md = _format_review_report(report)
    if output:
        Path(output).write_text(md, encoding="utf-8")
        console.print_success(f"✅ report written: {output}")
    else:
        click.echo(md)


def _format_review_report(report) -> str:
    lines = ["# UltraReview Code Review Report", "", f"Target: {report.target}", ""]
    if report.files_reviewed:
        lines.append("## Files Reviewed")
        for p in report.files_reviewed:
            lines.append(f"- {p}")
        lines.append("")
    if report.findings:
        lines.append("## Findings")
        for i, f in enumerate(report.findings, 1):
            lines.append(f"{i}. **[{f.severity.upper()}]** {f.file}:{f.line} — {f.message}  ")
            lines.append(f"   _Lens: {f.lens} | Category: {f.category}_")
        lines.append("")
    lines.append("## Review Summary")
    lines.append("")
    lines.append(report.summary)
    return "\n".join(lines)


# ── Structured output commands ──


@cli.group("agent-loop")
def agent_loop():
    """Interactive agent loop — decomposes tasks autonomously in plain language, observing/deciding/acting step by step."""
    pass


@agent_loop.command("run")
@click.argument("prompt")
@click.option("--model", "-m", default="", help="fusion-mlx model (empty = auto)")
@click.option("--max-steps", default=20, help="max loop steps")
def agent_loop_run(prompt: str, model: str, max_steps: int):
    """Run the interactive agent loop (single turn)."""
    from .agent_loop import AgentLoop
    from .nodes import import_all_nodes

    import_all_nodes()

    loop = AgentLoop(mlx_client=_get_mlx_client(), model=model, max_steps=max_steps, on_turn=_print_agent_turn)
    console.print_header(f"🤖 Agent Loop (max {max_steps} steps)")
    result = asyncio.run(loop.run(prompt))
    if result.degraded:
        console.print_warning("⚠️  LLM unavailable, degraded mode")
    if result.interrupted:
        console.print_warning("⚠️  interrupted by user")
    console.print_info(f"completed={result.completed} turns={len(result.turns)}")


def _print_agent_turn(turn):
    if turn.role == "user" and turn.content.startswith("[supplement]"):
        console.print_info(turn.content)
        return
    if turn.role == "user":
        return
    if turn.action is None:
        console.print_info(turn.content)
        return
    console.print_info(f"[{turn.action.type}] {turn.action.message}")
    if turn.action.node:
        click.echo(f"    node: {turn.action.node} | params: {json.dumps(turn.action.params, ensure_ascii=False)}")
    if turn.observation:
        click.echo(f"    observation: {turn.observation[:200]}")


@agent_loop.command("chat")
@click.option("--model", "-m", default="", help="fusion-mlx model (empty = auto)")
@click.option("--max-steps", default=20, help="max loop steps per turn")
def agent_loop_chat(model: str, max_steps: int):
    """Interactive multi-turn agent loop (REPL, type quit to exit)."""
    from .agent_loop import AgentLoop
    from .nodes import import_all_nodes

    import_all_nodes()

    loop = AgentLoop(mlx_client=_get_mlx_client(), model=model, max_steps=max_steps, on_turn=_print_agent_turn)
    console.print_header("🤖 Agent Loop chat mode (type quit to exit, stop to interrupt current turn)")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("\nBye")
            break
        if not line:
            continue
        if line.lower() == "quit":
            click.echo("Bye")
            break
        if line.lower() == "stop":
            loop.interrupt()
            continue
        result = asyncio.run(loop.run(line))
        if result.degraded:
            console.print_warning("⚠️  LLM unavailable, degraded mode")
        console.print_info(f"  completed={result.completed} turns={len(result.turns)}")


# ── Structured output commands ──


@cli.group("collab")
def collab():
    """Collaboration WebSocket bidirectional channel — realtime chat/cursor/presence."""
    pass


@collab.command("serve")
@click.option("--host", default="127.0.0.1", help="listen address")
@click.option("--port", default=11439, help="listen port")
def collab_serve(host: str, port: int):
    """Start the collaboration WS server (blocking)."""
    from .config_center import ConfigCenter
    from .server.collab_ws import CollabHub

    auth_token = ConfigCenter.get_instance().get("collab.auth_token") or None

    async def _run():
        hub = CollabHub(auth_token=auth_token)
        server = await hub.serve_ws(host=host, port=port)
        console.print_success(f"✅ collaboration WS server: ws://{host}:{port} (Ctrl+C to exit)")
        try:
            await asyncio.Future()
        finally:
            server.close()
            await server.wait_closed()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nStopped")


# ── Structured output commands ──


@cli.group("schema")
def schema():
    """Structured output — JSON Schema validation."""
    pass


@schema.command("validate")
@click.argument("data_file", type=click.Path(exists=True))
@click.argument("schema_file", type=click.Path(exists=True))
def schema_validate(data_file: str, schema_file: str):
    """Validate a data file against a schema."""
    from .engine.schema import OutputSchema

    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)
    with open(schema_file, encoding="utf-8") as f:
        schema = json.load(f)
    valid = OutputSchema.validate(data, schema)
    if valid:
        console.print_success("✅ data matches schema")
    else:
        console.print_error("❌ data does not match schema")
        errors = OutputSchema.validate_detailed(data, schema)
        for err in errors:
            click.echo(f"  - {err}")


@schema.command("check")
@click.argument("node_name")
def schema_check(node_name: str):
    """Check a node's output schema."""
    node_cls = NodeRegistry.get(node_name)
    if not node_cls:
        console.print_error(f"unknown node: {node_name}")
        return
    node = node_cls()
    params_schema = node.get_params_schema()
    click.echo(json.dumps(params_schema, indent=2, ensure_ascii=False))


# ── Benchmark ──


@cli.group("benchmark")
def benchmark():
    """Capability comparison and performance benchmark."""
    pass


@benchmark.command("report")
@click.option("--format", "fmt", default="markdown", type=click.Choice(["markdown", "html", "json"]))
@click.option("--output", "-o", default="", help="output file path")
def benchmark_report(fmt, output):
    """Generate the Claude Cowork vs Fusion-Cowork comparison report."""
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
        console.print_result(f"report saved: {output}")
    else:
        click.echo(content)


@benchmark.command("run")
@click.option("--node", "-n", multiple=True, help="node names to test (repeatable)")
@click.option("--repeats", default=3, type=int, help="repeats per node")
def benchmark_run(node, repeats):
    """Run node performance benchmark."""
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


@benchmark.command("orchestration")
@click.option("--scenario", "-s", multiple=True, help="run only the given scenarios (repeatable)")
def benchmark_orchestration(scenario):
    """Orchestration quality benchmark — deterministic scenarios run through the real engine, score=100 is the gate (issue #466)."""
    import json as _json

    from fusion_cowork.benchmark.orchestration import OrchestrationBenchmark

    result = OrchestrationBenchmark().run(list(scenario) if scenario else None)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    if result["score"] < 100.0:
        raise click.exceptions.Exit(1)


# ── Space collaboration ──


@cli.group("space")
def space():
    """Space management — create/list/archive/member management."""
    pass


@space.command("create")
@click.argument("name")
@click.option("--owner", "-o", default="local_user", help="owner user id")
@click.option("--description", "-d", default="", help="space description")
@click.option("--collab-mode", default="local", help="collab mode (local/p2p)")
def space_create(name: str, owner: str, description: str, collab_mode: str):
    """Create a space."""
    asyncio.run(_async_space_create(name, owner, description, collab_mode))


async def _async_space_create(name: str, owner: str, description: str, collab_mode: str):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        space_obj = await svc.create(name=name, owner_id=owner, description=description, collab_mode=collab_mode)
        console.print_success(f"space created: {space_obj.id}")
        console.print_result(f"  Name: {space_obj.name}")
        console.print_result(f"  Owner: {space_obj.owner_id}")
        console.print_result(f"  Status: {space_obj.status}")
    finally:
        await store.close()


@space.command("list")
@click.option("--status", "-s", default=None, help="filter by status (active/archived)")
@click.option("--owner", "-o", default=None, help="filter by owner")
@click.option("--limit", "-n", default=20, help="max results")
def space_list(status, owner, limit):
    """List spaces."""
    asyncio.run(_async_space_list(status, owner, limit))


async def _async_space_list(status, owner, limit):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        spaces = await svc.list(status=status, owner_id=owner, limit=limit)
        if not spaces:
            console.print_info("no spaces")
            return
        console.print_header(f"Space list ({len(spaces)})")
        rows = []
        for sp in spaces:
            rows.append([sp.id, sp.name, sp.owner_id, sp.status, sp.collab_mode])
        console.print_table(["ID", "Name", "Owner", "Status", "Collab Mode"], rows)
    finally:
        await store.close()


@space.command("get")
@click.argument("space_id")
def space_get(space_id: str):
    """Show space details."""
    asyncio.run(_async_space_get(space_id))


async def _async_space_get(space_id: str):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        sp = await svc.get(space_id)
        if not sp:
            console.print_error(f"space not found: {space_id}")
            return
        console.print_header(f"🚀 {sp.name}")
        click.echo(f"  ID: {sp.id}")
        click.echo(f"  Description: {sp.description}")
        click.echo(f"  Owner: {sp.owner_id}")
        click.echo(f"  Status: {sp.status}")
        click.echo(f"  Collab mode: {sp.collab_mode}")
        click.echo(f"  KB binding: {sp.kb_bind_mode} ({sp.kb_id or 'none'})")
        click.echo(f"  Created: {sp.created_at}")
        click.echo(f"  Updated: {sp.updated_at}")
        config = sp.config
        click.echo("\n  Config:")
        click.echo(f"    Max members: {config.max_members}")
        click.echo(f"    Web search: {'✅' if config.enable_web_search else '❌'}")
        click.echo(f"    Deep research: {'✅' if config.enable_deep_research else '❌'}")
        click.echo(f"    Computer Use: {'✅' if config.enable_computer_use else '❌'}")
        click.echo(f"    Streaming: {'✅' if config.stream_response else '❌'}")
        members = await store.list_members(space_id)
        click.echo(f"\n  Members ({len(members)}):")
        for m in members:
            click.echo(f"    ├─ {m.display_name} ({m.user_id}) — {m.role}")
    finally:
        await store.close()


@space.command("archive")
@click.argument("space_id")
def space_archive(space_id: str):
    """Archive a space."""
    asyncio.run(_async_space_archive(space_id))


async def _async_space_archive(space_id: str):
    from fusion_cowork.space import SpaceService, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        svc = SpaceService(store)
        result = await svc.archive(space_id)
        if result:
            console.print_success(f"space archived: {space_id}")
        else:
            console.print_error(f"archive failed: space not found {space_id}")
    finally:
        await store.close()


@space.group("member")
def space_member():
    """Space member management."""
    pass


@space_member.command("list")
@click.argument("space_id")
def space_member_list(space_id: str):
    """List space members."""
    asyncio.run(_async_space_member_list(space_id))


async def _async_space_member_list(space_id: str):
    from fusion_cowork.space import SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        members = await store.list_members(space_id)
        if not members:
            console.print_info("space has no members")
            return
        console.print_header(f"Member list ({len(members)})")
        rows = []
        for m in members:
            rows.append([m.user_id, m.display_name, m.role, m.joined_at[:19]])
        console.print_table(["User ID", "Display Name", "Role", "Joined At"], rows)
    finally:
        await store.close()


@space_member.command("invite")
@click.argument("space_id")
@click.option("--inviter", "-i", default="local_user", help="inviter id")
@click.option("--role", "-r", default="member", help="member role (member/admin/viewer)")
@click.option("--max-uses", default=0, type=int, help="max uses (0=unlimited)")
@click.option("--expires-hours", default=0, type=int, help="expiry (hours, 0=never expires)")
def space_member_invite(space_id: str, inviter: str, role: str, max_uses: int, expires_hours: int):
    """Generate an invite code."""
    asyncio.run(_async_space_member_invite(space_id, inviter, role, max_uses, expires_hours))


async def _async_space_member_invite(space_id: str, inviter: str, role: str, max_uses: int, expires_hours: int):
    from fusion_cowork.space import SpaceMemberService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        svc = SpaceMemberService(store, perm)
        code = await svc.invite(space_id, inviter, role=role, max_uses=max_uses, expires_hours=expires_hours)
        console.print_success(f"invite code generated: {code}")
        console.print_info(f"use 'fusion-cowork space member join {code}' to join the space")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_member.command("join")
@click.argument("invite_code")
@click.option("--user", "-u", default="local_user", help="user id")
@click.option("--display-name", "-n", default="", help="display name")
def space_member_join(invite_code: str, user: str, display_name: str):
    """Join a space via invite code."""
    asyncio.run(_async_space_member_join(invite_code, user, display_name))


async def _async_space_member_join(invite_code: str, user: str, display_name: str):
    from fusion_cowork.space import SpaceMemberService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        svc = SpaceMemberService(store, perm)
        member = await svc.join(invite_code, user_id=user, display_name=display_name)
        console.print_success(f"joined space: {member.space_id} (role: {member.role})")
    except ValueError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_member.command("remove")
@click.argument("space_id")
@click.argument("user_id")
@click.option("--operator", "-o", default="local_user", help="operator id")
def space_member_remove(space_id: str, user_id: str, operator: str):
    """Remove a space member."""
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
            console.print_success(f"member removed: {user_id}")
        else:
            console.print_error("removal failed: member not found")
    except (PermissionError, ValueError) as e:
        console.print_error(str(e))
    finally:
        await store.close()


# ── Space Chat ──


@space.command("chat")
@click.argument("space_id")
@click.option("--user", "-u", default="local_user", help="user id")
@click.option("--agent", "-a", default=None, help="agent id (triggers an agent reply)")
@click.option("--model", "-m", default=None, help="model name")
def space_chat(space_id: str, user: str, agent: str, model: str):
    """Interactive shared chat. Press enter to send a message, Ctrl+C to exit."""
    asyncio.run(_async_space_chat(space_id, user, agent, model))


async def _async_space_chat(space_id: str, user: str, agent: str, model: str):
    from fusion_cowork.ai.mlx_client import FusionMLXClient
    from fusion_cowork.space import SpaceChatService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        sp = await store.get_space(space_id)
        if not sp:
            console.print_error(f"space not found: {space_id}")
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
    """Space knowledge base management."""
    pass


@space_knowledge.command("bind")
@click.argument("space_id")
@click.option("--operator", "-o", default="local_user", help="operator id")
@click.option("--kb-id", "-k", default=None, help="bind an existing KB id (auto-creates one if omitted)")
def space_kb_bind(space_id: str, operator: str, kb_id: str):
    """Bind a knowledge base to the space."""
    asyncio.run(_async_space_kb_bind(space_id, operator, kb_id))


async def _async_space_kb_bind(space_id: str, operator: str, kb_id: str):
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_svc = SpaceKBService(store, None, perm)
        result = await kb_svc.bind_kb(space_id, operator, kb_id=kb_id)
        console.print_success(f"KB bound: {result}")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_knowledge.command("status")
@click.argument("space_id")
def space_kb_status(space_id: str):
    """Show space KB status."""
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
            console.print_header(f"KB bound: {status['kb_id']}")
            docs = status.get("document_count", 0)
            click.echo(f"  documents: {docs}")
        else:
            console.print_info("space has no KB bound")
    finally:
        await store.close()


@space_knowledge.command("upload")
@click.argument("space_id")
@click.argument("file_path")
@click.option("--operator", "-o", default="local_user", help="operator id")
def space_kb_upload(space_id: str, file_path: str, operator: str):
    """Upload a file to the space KB."""
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
        console.print_success(f"file uploaded: {result}")
    except PermissionError as e:
        console.print_error(str(e))
    except FileNotFoundError:
        console.print_error(f"file not found: {file_path}")
    finally:
        await store.close()


@space_knowledge.command("search")
@click.argument("space_id")
@click.argument("query")
@click.option("--top-k", "-k", default=5, help="number of results")
def space_kb_search(space_id: str, query: str, top_k: int):
    """Search in the space KB."""
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
            console.print_info("no search results")
            return
        console.print_header(f"search results ({len(results)})")
        for i, r in enumerate(results, 1):
            score = r.get("score", 0)
            content = r.get("content", "")[:150]
            click.echo(f"  {i}. [score={score:.2f}] {content}")
    finally:
        await store.close()


@space_knowledge.command("unbind")
@click.argument("space_id")
@click.option("--operator", "-o", default="local_user", help="operator id")
def space_kb_unbind(space_id: str, operator: str):
    """Unbind the space KB."""
    asyncio.run(_async_space_kb_unbind(space_id, operator))


async def _async_space_kb_unbind(space_id: str, operator: str):
    from fusion_cowork.space import SpaceKBService, SpacePermission, SpaceStore

    store = SpaceStore()
    await store.initialize()
    try:
        perm = SpacePermission(store)
        kb_svc = SpaceKBService(store, None, perm)
        await kb_svc.unbind_kb(space_id, operator)
        console.print_success(f"KB unbound: {space_id}")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


# ── Space Agent ──


@space.group("agent")
def space_agent():
    """Space agent management."""
    pass


@space_agent.command("list")
@click.argument("space_id")
def space_agent_list(space_id: str):
    """List space agents."""
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
            console.print_info("space has no agents")
            return
        console.print_header(f"Agent list ({len(agents)})")
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
        console.print_table(["ID", "Name", "Type", "RAG", "Creator"], rows)
    finally:
        await store.close()


@space_agent.command("add")
@click.argument("space_id")
@click.argument("name")
@click.option("--type", "-t", "agent_type", default="assistant", help="agent type")
@click.option("--prompt", "-p", default="", help="system prompt")
@click.option("--rag/--no-rag", default=False, help="enable RAG")
@click.option("--operator", "-o", default="local_user", help="operator id")
def space_agent_add(space_id: str, name: str, agent_type: str, prompt: str, rag: bool, operator: str):
    """Add an agent to the space."""
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
        console.print_success(f"agent added: {result['id']} ({result['name']})")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_agent.command("remove")
@click.argument("space_id")
@click.argument("agent_id")
@click.option("--operator", "-o", default="local_user", help="operator id")
def space_agent_remove(space_id: str, agent_id: str, operator: str):
    """Remove a space agent."""
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
            console.print_success(f"Agent removed: {agent_id}")
        else:
            console.print_error(f"agent not found: {agent_id}")
    except PermissionError as e:
        console.print_error(str(e))
    finally:
        await store.close()


@space_agent.command("call")
@click.argument("space_id")
@click.argument("agent_id")
@click.argument("message")
@click.option("--user", "-u", default="local_user", help="user id")
@click.option("--model", "-m", default="", help="model name")
def space_agent_call(space_id: str, agent_id: str, message: str, user: str, model: str):
    """Call a space agent."""
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
@click.option("--agents", "-a", required=True, help="agent id list (comma-separated)")
@click.option("--user", "-u", default="local_user", help="user id")
@click.option("--model", "-m", default="", help="model name")
def space_agent_relay(space_id: str, message: str, agents: str, user: str, model: str):
    """Multi-agent relay invocation."""
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
        console.print_header(f"agent relay results ({len(results)} steps)")
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


# ── Database commands (v0.4.0 Stage 3) ──


@cli.group()
def db():
    """Database backup/restore + migration management (postgres backend)."""


@db.command("backup")
@click.option("--dsn", default="", help="Postgres DSN (falls back to env DATABASE_URL / FUSION_PG_DSN)")
@click.option("--dest", default="", help="backup output path (.sql.gz)")
def db_backup(dsn: str, dest: str):
    """Back up with pg_dump to .sql.gz."""
    import os

    dsn = dsn or os.environ.get("DATABASE_URL", "") or os.environ.get("FUSION_PG_DSN", "")
    if not dsn:
        console.print_error("missing DSN — pass --dsn or set env DATABASE_URL")
        raise SystemExit(1)
    if not dest:
        from datetime import datetime

        dest = f"fusion-cowork-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sql.gz"
    from .db.backup import BackupManager

    mgr = BackupManager(dsn)
    out = mgr.backup(dest)
    console.print_success(f"backup complete: {out} ({out.stat().st_size} bytes)")


@db.command("restore")
@click.option("--dsn", default="", help="Postgres DSN (falls back to env DATABASE_URL / FUSION_PG_DSN)")
@click.option("--src", required=True, help="backup file (.sql.gz)")
@click.option("--confirm", is_flag=True, help="confirm overwrite of the existing database (destructive)")
def db_restore(dsn: str, src: str, confirm: bool):
    """Restore from .sql.gz (destructive, requires --confirm)."""
    import os

    dsn = dsn or os.environ.get("DATABASE_URL", "") or os.environ.get("FUSION_PG_DSN", "")
    if not dsn:
        console.print_error("missing DSN — pass --dsn or set env DATABASE_URL")
        raise SystemExit(1)
    from .db.backup import BackupManager

    mgr = BackupManager(dsn)
    mgr.restore(src, confirm=confirm)
    console.print_success(f"restore complete: {src}")


@db.command("migrate")
@click.option("--dsn", default="", help="Postgres DSN (falls back to env DATABASE_URL / FUSION_PG_DSN)")
def db_migrate(dsn: str):
    """Run pending database migrations (versioned, idempotent)."""
    import asyncio
    import os

    dsn = dsn or os.environ.get("DATABASE_URL", "") or os.environ.get("FUSION_PG_DSN", "")
    if not dsn:
        console.print_error("missing DSN — pass --dsn or set env DATABASE_URL")
        raise SystemExit(1)

    async def _run():
        import asyncpg

        from .db.migrations import MigrationRunner

        conn = await asyncpg.connect(dsn)
        try:
            # create base schema (migrations depend on the tables existing)
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
            console.print_success(f"migration complete: applied versions {applied or '(none pending)'}")
        finally:
            await conn.close()

    asyncio.run(_run())


# ── Main entry ──


@cli.command("serve")
@click.option("--host", default="", help="bind address (default reads FUSION_BIND_HOST, then 127.0.0.1 standalone)")
@click.option("--port", default=11438, show_default=True, help="HTTP port (/rpc /health /mcp /sse)")
@click.option("--json-log", "json_log", is_flag=True, help="structured JSON logs (structlog, production)")
@click.option("--metrics-port", default=0, help="prometheus /metrics port (0=disabled)")
def serve(host: str, port: int, json_log: bool, metrics_port: int):
    """Container/cloud deployment mode — HTTP single channel (no UDS), 0.0.0.0 binding, uvicorn handles SIGTERM graceful shutdown.

    Use this command as the Dockerfile CMD / k8s ENTRYPOINT.
    For local development use `desk rpc` (UDS + HTTP dual channel).
    """
    _set_process_title(f"fusion-cowork serve :{port}")
    import asyncio

    from .nodes import import_all_nodes
    from .observability.metrics import maybe_start_prometheus_endpoint
    from .server.mcp_server import MCPServer

    bind_host = host or os.environ.get("FUSION_BIND_HOST", "127.0.0.1")
    _json_env = os.environ.get("FUSION_JSON_LOG", "").strip().lower() in ("1", "true", "yes", "on")
    if json_log or _json_env:
        setup_logger(json=True)
    if metrics_port:
        maybe_start_prometheus_endpoint(metrics_port)

    import_all_nodes()
    rt = _build_runtime()
    mcp = MCPServer(
        host=bind_host,
        port=port,
        permission_manager=rt["permission_manager"],
        hook_manager=rt["hook_manager"],
    )
    console.print_success(f"fusion-cowork serve (container mode): {bind_host}:{port} (/rpc /health /mcp /sse)")
    try:
        asyncio.run(mcp.serve_http(event_emitter=rt["event_emitter"]))
    except KeyboardInterrupt:
        console.print_info("service stopped")


@cli.group()
def mailbox():
    """Support mailbox agent (IMAP poller + classify, issue #546)."""


@mailbox.command("serve")
@click.option("--interval", "-i", default=300, type=int, help="polling interval seconds (default 300)")
def mailbox_serve(interval):
    """Run the support mailbox agent loop (P0 observe-only)."""

    async def _serve():
        from .mailbox.service import MailboxAgent

        agent = MailboxAgent.from_env()
        if agent is None:
            console.print_error(
                "mailbox agent disabled: set FUSION_MAILBOX_ENABLED=1 and "
                "FUSION_MAILBOX_IMAP_HOST/USER/PASS (fusion-sv env injection)"
            )
            return
        agent._poll_interval_s = interval
        await agent.start()
        console.print_success("mailbox agent started (P0 observe-only, Ctrl-C to stop)")
        try:
            await agent._task
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await agent.stop()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        console.print_info("mailbox agent stopped")


def main():
    """Fusion-Cowork main entry."""
    cli()


if __name__ == "__main__":
    main()
