"""Headless runner — no CLI/GUI workflow execution.

Importer: test_m5.py (from fusion_cowork.sdk.headless import HeadlessRunner).
API: HeadlessRunner with run_template(), run_workflow(), cancel().
User instruction: continue M5.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class HeadlessRunner:
    """Run workflows and templates without CLI or GUI."""

    def __init__(self, engine=None, template_manager=None):
        if engine is None:
            from fusion_cowork.engine import WorkflowEngine

            engine = WorkflowEngine()
        if template_manager is None:
            from fusion_cowork.templates import TemplateManager

            template_manager = TemplateManager()
        self._engine = engine
        self._template_manager = template_manager
        self._running = False
        self._last_execution_id: str = ""

    async def run_template(self, template_id: str, params: dict = None) -> Dict[str, Any]:
        logger.info(f"HeadlessRunner: running template {template_id}")
        template = self._template_manager.get_template(template_id)
        if not template:
            logger.error(f"HeadlessRunner: template not found: {template_id}")
            return {"error": f"template not found: {template_id}"}
        from fusion_cowork.engine import Workflow

        wf_data = template.get("workflow", template)
        wf = Workflow.from_dict(wf_data)
        self._running = True
        try:
            result = await self._engine.execute(wf)
            self._last_execution_id = result.id
            logger.info(f"HeadlessRunner: template {template_id} done, status={result.status.value}")
            return {
                "status": result.status.value,
                "steps": len(result.steps),
                "total_time": result.total_time,
            }
        except asyncio.CancelledError:
            logger.info(f"HeadlessRunner: template {template_id} cancelled")
            return {"status": "cancelled"}
        finally:
            self._running = False

    async def run_workflow(self, workflow_def: dict, inputs: dict = None) -> Dict[str, Any]:
        logger.info("HeadlessRunner: running workflow")
        from fusion_cowork.engine import Workflow

        wf = Workflow.from_dict(workflow_def)
        self._running = True
        try:
            result = await self._engine.execute(wf)
            self._last_execution_id = result.id
            logger.info(f"HeadlessRunner: workflow done, status={result.status.value}")
            return {
                "status": result.status.value,
                "steps": len(result.steps),
                "total_time": result.total_time,
            }
        except asyncio.CancelledError:
            logger.info("HeadlessRunner: workflow cancelled")
            return {"status": "cancelled"}
        finally:
            self._running = False

    async def cancel(self, execution_id: str = "") -> None:
        logger.info("HeadlessRunner: cancelling current run")
        eid = execution_id or self._last_execution_id
        if eid:
            self._engine.cancel(eid)
        self._running = False
