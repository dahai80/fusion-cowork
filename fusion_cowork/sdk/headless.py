"""Headless runner — no CLI/GUI workflow execution.

Importer: test_m5.py (from fusion_cowork.sdk.headless import HeadlessRunner).
API: HeadlessRunner with run_template(), run_workflow(), run_workflow_stream(), cancel().
User instruction: continue M5 + P2-4 stream-json/print/json-schema/budget/input-format.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

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

    async def run_workflow(
        self,
        workflow_def: dict,
        inputs: dict = None,
        output_format: str = "json",
        json_schema: Optional[Dict] = None,
        max_budget_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """Execute a workflow, supporting output_format/json_schema/budget.

        output_format: json (default, returns dict) | print (returns {"text": ...}) | stream-json (calls run_workflow_stream).
        json_schema: JSON Schema validating the final output; on failure returns status=schema_error.
        max_budget_usd: enables the budget cap when >0 (reuses BudgetTracker).
        """
        logger.info(f"HeadlessRunner: running workflow format={output_format}")
        from fusion_cowork.engine import Workflow

        if max_budget_usd > 0:
            from fusion_cowork.ai import get_budget_tracker

            get_budget_tracker(max_budget_usd=max_budget_usd, enforce=True)

        wf = Workflow.from_dict(workflow_def)
        self._running = True
        try:
            result = await self._engine.execute(wf)
            self._last_execution_id = result.id
            logger.info(f"HeadlessRunner: workflow done, status={result.status.value}")
            final_data = self._extract_final_output(result)
            payload = {
                "status": result.status.value,
                "steps": len(result.steps),
                "total_time": result.total_time,
                "output": final_data,
            }

            if json_schema is not None:
                valid, err = self._validate_schema(final_data, json_schema)
                if not valid:
                    logger.warning(f"HeadlessRunner: final output schema validation failed: {err}")
                    payload["status"] = "schema_error"
                    payload["schema_error"] = err

            if output_format == "print":
                payload = {"text": json.dumps(payload, ensure_ascii=False, indent=2)}
            elif output_format == "stream-json":
                # Streaming callers should use run_workflow_stream directly; this returns the final frame
                payload = {"type": "result", "data": payload}

            return payload
        except asyncio.CancelledError:
            logger.info("HeadlessRunner: workflow cancelled")
            return {"status": "cancelled"}
        finally:
            self._running = False

    async def run_workflow_stream(
        self,
        workflow_def: dict,
        inputs: dict = None,
        max_budget_usd: float = 0.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Streaming execution - yields stream-json event frames per node.

        Event frame type: workflow_start | node_start | node_end | workflow_end | error.
        Each frame is one JSON line (stream-json format), suited for stdout pipe consumption.
        """
        from fusion_cowork.engine import Workflow

        if max_budget_usd > 0:
            from fusion_cowork.ai import get_budget_tracker

            get_budget_tracker(max_budget_usd=max_budget_usd, enforce=True)

        wf = Workflow.from_dict(workflow_def)
        self._running = True

        # asyncio.Queue bridging sync progress callback -> async generator
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        node_events: list = []

        def _on_step(execution, step) -> None:
            frame = {
                "type": "node_end",
                "node_id": step.node_id,
                "node_name": step.node_name,
                "status": step.status.value if hasattr(step.status, "value") else str(step.status),
                "summary": step.summary,
                "error": step.error,
            }
            node_events.append(frame)
            try:
                loop.call_soon_threadsafe(queue.put_nowait, frame)
            except Exception as e:
                logger.debug(f"HeadlessRunner stream callback enqueue failed: {e}")

        self._engine.on_progress(_on_step)

        yield {
            "type": "workflow_start",
            "nodes": [n.get("id", str(i)) for i, n in enumerate(workflow_def.get("nodes", []))],
        }
        try:
            exec_task = asyncio.ensure_future(self._engine.execute(wf))
            # Emit frames while executing, until execution ends
            while not exec_task.done():
                try:
                    frame = await asyncio.wait_for(asyncio.shield(queue.get()), timeout=0.2)
                    yield frame
                except TimeoutError:
                    continue
            result = await exec_task
            self._last_execution_id = result.id
            # Drain leftover frames
            while not queue.empty():
                yield queue.get_nowait()
            yield {
                "type": "workflow_end",
                "status": result.status.value,
                "steps": len(result.steps),
                "total_time": result.total_time,
                "output": self._extract_final_output(result),
            }
        except asyncio.CancelledError:
            yield {"type": "error", "error": "cancelled"}
        except Exception as e:
            logger.error(f"HeadlessRunner: stream exception: {e}")
            yield {"type": "error", "error": str(e)}
        finally:
            # Remove callback to prevent accumulation
            try:
                self._engine._progress_callbacks.remove(_on_step)
            except (AttributeError, ValueError):
                pass
            self._running = False

    def _extract_final_output(self, result) -> Dict[str, Any]:
        """Extract the final output from the execution result (last successful node's output_data)."""
        try:
            # LO-2: the original nested ternary `x == "success" if hasattr(...) else False` had ambiguous precedence;
            # split into explicit checks: take the status value, compare as string
            for step in reversed(result.steps):
                status_val = step.status.value if hasattr(step.status, "value") else str(step.status)
                if status_val == "success":
                    return step.output_data or {}
            # No successful node: take the last one with output_data
            for step in reversed(result.steps):
                if getattr(step, "output_data", None):
                    return step.output_data
        except Exception as e:
            logger.debug(f"HeadlessRunner _extract_final_output exception: {e}")
        return {}

    def _make_progress_callback(self):
        """Deprecated - streaming now uses on_progress + asyncio.Queue bridging. Kept as a no-op for compatibility."""

        def _noop(*args, **kwargs) -> None:
            return None

        return _noop

    def _validate_schema(self, data: Dict, schema: Dict) -> tuple:
        try:
            from fusion_cowork.engine.schema import OutputSchema

            ok = OutputSchema.validate(data, schema)
            return (bool(ok), "" if ok else "schema validation failed")
        except Exception as e:
            return (False, str(e))

    async def cancel(self, execution_id: str = "") -> None:
        logger.info("HeadlessRunner: cancelling current run")
        eid = execution_id or self._last_execution_id
        if eid:
            self._engine.cancel(eid)
        self._running = False
