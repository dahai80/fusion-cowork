"""Union Web 模块 API — issue #94 (PRD N-6)。

fusion-cowork web/ UI 包的后端 HTTP 面:
- REST: 节点目录 / 工作流定义 CRUD (文件存储) / 运行生命周期 (异步 run + cancel)
- 静态托管: web/dist SPA (gateway /shell/fusion-cowork/ 代理到本服务)
- 鉴权: FUSION_COWORK_WEB_API_KEY 静态密钥 (Bearer / x-api-key), gateway 代理注入;
  未配置时放行 (默认仅 127.0.0.1 绑定, 本地优先) — 对齐 agent-studio #327 模式
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_DEFAULT_SPACE_DATA_DIR = str(Path.home() / ".fusion-cowork" / "spaces")
_RUN_TTL_SECONDS = 3600


def _api_key() -> str:
    return os.environ.get("FUSION_COWORK_WEB_API_KEY", "")


def auth_configured() -> bool:
    return bool(_api_key())


def check_static_key(key: str) -> Optional[Dict[str, Any]]:
    expected = _api_key()
    if not expected or not key or key != expected:
        return None
    return {"valid": True, "key_id": "static-env", "permissions": ["admin"]}


def extract_caller_key(headers: Any) -> str:
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("x-api-key") or "").strip()


class WorkflowDefStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root) if root else Path.home() / ".fusion-cowork" / "web" / "workflows"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, workflow_id: str) -> Path:
        safe = "".join(c for c in workflow_id if c.isalnum() or c in "-_")
        if not safe or safe != workflow_id:
            raise ValueError(f"非法工作流 id: {workflow_id}")
        return self._root / f"{safe}.json"

    def list(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for p in sorted(self._root.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                items.append(
                    {
                        "id": data.get("id", p.stem),
                        "name": data.get("name", p.stem),
                        "description": data.get("description", ""),
                        "updated_at": data.get("updated_at", 0),
                        "node_count": len((data.get("workflow") or {}).get("nodes") or []),
                    }
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"工作流定义读取失败 {p.name}: {e}")
        return items

    def get(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._path(workflow_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return None

    def save(self, workflow_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = {
            "id": workflow_id,
            "name": payload.get("name", workflow_id),
            "description": payload.get("description", ""),
            "workflow": payload.get("workflow", {}),
            "updated_at": time.time(),
        }
        path = self._path(workflow_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=4), encoding="utf-8")
        os.replace(tmp, path)
        logger.info(f"工作流定义已保存: {workflow_id}")
        return record

    def delete(self, workflow_id: str) -> bool:
        try:
            self._path(workflow_id).unlink()
            logger.info(f"工作流定义已删除: {workflow_id}")
            return True
        except (FileNotFoundError, ValueError):
            return False


class RunRegistry:
    def __init__(self, engine: Any):
        self._engine = engine
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def list(self) -> List[Dict[str, Any]]:
        self._gc()
        return [
            {
                "run_id": r["run_id"],
                "workflow_id": r["workflow_id"],
                "workflow_name": r["workflow_name"],
                "status": r["status"],
                "created_at": r["created_at"],
                "finished_at": r.get("finished_at"),
                "error": r.get("error"),
            }
            for r in sorted(self._runs.values(), key=lambda x: -x["created_at"])
        ]

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._runs.get(run_id)

    def start(self, workflow_def: Dict[str, Any], workflow_id: str = "", workflow_name: str = "") -> Dict[str, Any]:
        from ..engine import Workflow

        wf = Workflow.from_dict(workflow_def)
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        record: Dict[str, Any] = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name or wf.name or workflow_id,
            "status": "running",
            "created_at": time.time(),
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
        }
        self._runs[run_id] = record
        task = asyncio.create_task(self._execute(run_id, wf))
        self._tasks[run_id] = task
        logger.info(f"工作流运行已启动: {run_id} ({record['workflow_name']})")
        return {"run_id": run_id, "status": "running"}

    async def _execute(self, run_id: str, wf: Any) -> None:
        record = self._runs[run_id]
        try:
            execution = await self._engine.execute(wf, execution_id=run_id)
            record["steps"] = [
                {
                    "node_id": s.node_id,
                    "node_name": s.node_name,
                    "node_display_name": s.node_display_name,
                    "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                    "execution_time": s.execution_time,
                    "summary": s.summary,
                    "error": s.error,
                    "output_data": s.output_data,
                }
                for s in execution.steps
            ]
            status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
            record["status"] = status
            record["result"] = {
                "summary": execution.result_summary,
            }
            if execution.error:
                record["error"] = execution.error
        except asyncio.CancelledError:
            record["status"] = "cancelled"
        except Exception as e:
            logger.error(f"工作流运行失败 {run_id}: {e}")
            record["status"] = "failed"
            record["error"] = str(e)
        finally:
            record["finished_at"] = time.time()
            self._tasks.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if not record or record["status"] != "running":
            return False
        self._engine.cancel(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        logger.info(f"工作流运行已取消: {run_id}")
        return True

    def _gc(self) -> None:
        now = time.time()
        for rid in [
            r for r, v in self._runs.items() if v.get("finished_at") and now - v["finished_at"] > _RUN_TTL_SECONDS
        ]:
            self._runs.pop(rid, None)


def _build_space_stack(data_dir: Optional[Path] = None):
    """组装 space 域 (store + 4 svc + FastAPI 子应用) — web serve 与测试共用。

    Returns (space_app, artifact_svc, space_store) — store 供 serve 关停时 close()。
    """
    from ..ai.mlx_client import FusionMLXClient, KBClient
    from ..space import (
        NotificationService,
        SpaceChatService,
        SpaceKBService,
        SpaceMemberService,
        SpaceService,
        SpaceStore,
    )
    from ..space.api import create_space_api
    from ..space.artifact import SpaceArtifactService
    from ..space.permission import SpacePermission

    store = SpaceStore(data_dir=str(data_dir) if data_dir else _DEFAULT_SPACE_DATA_DIR)
    perm = SpacePermission(store)
    mlx = FusionMLXClient()
    chat_svc = SpaceChatService(store, mlx, perm)
    kb_svc = SpaceKBService(store, KBClient(), perm)
    artifact_svc = SpaceArtifactService(store, perm)
    notification_svc = NotificationService(store)
    space_svc = SpaceService(store)
    member_svc = SpaceMemberService(store, perm)
    app = create_space_api(
        space_svc,
        member_svc,
        chat_svc,
        kb_svc,
        artifact_svc=artifact_svc,
        notification_svc=notification_svc,
        space_store=store,
    )
    return app, artifact_svc, store


def _serialize_session(s: Any) -> Dict[str, Any]:
    return {
        "id": s.id,
        "workflow_id": s.workflow_id,
        "workflow_name": s.workflow_name,
        "status": s.status,
        "initial_input": s.initial_input,
        "execution_id": s.execution_id,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "completed_at": s.completed_at,
        "metadata": s.metadata,
    }


def create_web_app(
    engine: Any = None,
    dist_dir: Optional[Path] = None,
    store_root: Optional[Path] = None,
    scheduler: Any = None,
    nl_generator: Any = None,
    session_store: Any = None,
    space_api: Any = None,
    space_data_dir: Optional[Path] = None,
):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise ImportError("web API 需要 [web] 依赖: pip install fusion-cowork[web]") from e

    from ..nodes import import_all_nodes

    import_all_nodes()
    store = WorkflowDefStore(root=store_root)
    if engine is None:
        from ..engine import WorkflowEngine

        engine = WorkflowEngine()
    runs = RunRegistry(engine)
    if scheduler is None:
        from ..engine.scheduler import TaskScheduler

        scheduler = TaskScheduler()
    if nl_generator is None:
        from ..ai.mlx_client import FusionMLXClient
        from ..ai.nl_parser import NLWorkflowGenerator

        nl_generator = NLWorkflowGenerator(FusionMLXClient())
        logger.info("web API NL 生成器未注入, 使用默认 FusionMLXClient")
    if session_store is None:
        from ..engine.session import SessionStore

        session_store = SessionStore()
    if space_api is None:
        space_api, artifact_svc, space_store = _build_space_stack(space_data_dir)
        logger.info("web API space 栈已构建 (默认路径)")
    else:
        artifact_svc, space_store = None, None
    app = FastAPI(title="Fusion-Cowork Web API", version="0.1.0")
    app.state.scheduler = scheduler
    app.state.nl_generator = nl_generator
    app.state.session_store = session_store
    app.state.space_api = space_api
    app.state.artifact_svc = artifact_svc
    app.state.space_store = space_store

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        if request.url.path in ("/health",):
            return await call_next(request)
        key = extract_caller_key(request.headers)
        if not check_static_key(key):
            logger.warning(f"web API 鉴权拒绝: {request.url.path} (key {'缺失' if not key else '无效'})")
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "missing or invalid api key"}},
            )
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "module": "fusion-cowork", "version": _version()}

    @app.get("/api/nodes")
    async def nodes_list():
        from ..engine.node import NodeRegistry

        nodes = NodeRegistry.list()
        return {"nodes": nodes, "count": len(nodes)}

    @app.get("/api/workflows")
    async def workflows_list():
        items = store.list()
        return {"workflows": items, "count": len(items)}

    @app.post("/api/workflows", status_code=201)
    async def workflows_create(body: Dict[str, Any]):
        workflow_id = body.get("id") or f"wf_{uuid.uuid4().hex[:10]}"
        try:
            record = store.save(workflow_id, body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
        return record

    @app.get("/api/workflows/{workflow_id}")
    async def workflows_get(workflow_id: str):
        record = store.get(workflow_id)
        if not record:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"}
            )
        return record

    @app.put("/api/workflows/{workflow_id}")
    async def workflows_put(workflow_id: str, body: Dict[str, Any]):
        if not store.get(workflow_id):
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"}
            )
        return store.save(workflow_id, body)

    @app.delete("/api/workflows/{workflow_id}")
    async def workflows_delete(workflow_id: str):
        if not store.delete(workflow_id):
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"}
            )
        return {"deleted": True}

    @app.post("/api/runs", status_code=202)
    async def runs_create(body: Dict[str, Any]):
        workflow_def = body.get("workflow") or {}
        workflow_id = body.get("workflow_id", "")
        if not workflow_def and workflow_id:
            record = store.get(workflow_id)
            if not record:
                raise HTTPException(
                    status_code=404, detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"}
                )
            workflow_def = record.get("workflow") or {}
            workflow_name = record.get("name", workflow_id)
        else:
            workflow_name = body.get("name", "")
        if not workflow_def:
            raise HTTPException(
                status_code=400, detail={"code": "bad_request", "message": "workflow 或 workflow_id 必填"}
            )
        return runs.start(workflow_def, workflow_id=workflow_id, workflow_name=workflow_name)

    @app.get("/api/runs")
    async def runs_list():
        return {"runs": runs.list(), "count": len(runs.list())}

    @app.get("/api/runs/{run_id}")
    async def runs_get(run_id: str):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"})
        return record

    @app.post("/api/runs/{run_id}/cancel")
    async def runs_cancel(run_id: str):
        if not runs.cancel(run_id):
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不可取消: {run_id}"})
        return {"status": "cancelling", "run_id": run_id}

    # ── W5 模板目录 (migration doc §6 P0) ──
    @app.get("/api/templates")
    async def templates_list(category: str = ""):
        from ..templates import TemplateManager

        items = TemplateManager().list_templates(category=category or "")
        return {"templates": items, "count": len(items)}

    @app.get("/api/templates/{template_id}")
    async def templates_get(template_id: str):
        from ..templates import TemplateManager

        tpl = TemplateManager().get_template(template_id)
        if not tpl:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"模板不存在: {template_id}"})
        return tpl

    # ── W6 调度任务 (migration doc §6 P0) ──
    def _make_task_executor(workflow_id: str):
        async def _exec(task_id: str) -> None:
            record = store.get(workflow_id)
            if not record:
                logger.error(f"定时任务执行器找不到工作流: {workflow_id}")
                return
            runs.start(
                record.get("workflow") or {}, workflow_id=workflow_id, workflow_name=record.get("name", workflow_id)
            )
            logger.info(f"定时任务 {task_id} 已触发工作流 {workflow_id}")

        return _exec

    @app.get("/api/scheduler/tasks")
    async def scheduler_tasks_list():
        tasks = scheduler.list_tasks()
        return {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "workflow_id": t.workflow_id,
                    "trigger_type": t.trigger_type,
                    "trigger_config": t.trigger_config,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "created_at": t.created_at,
                    "last_run": t.last_run,
                    "next_run": t.next_run,
                    "run_count": t.run_count,
                    "fail_count": t.fail_count,
                    "description": t.description,
                    "tags": list(t.tags),
                }
                for t in tasks
            ],
            "count": len(tasks),
        }

    @app.post("/api/scheduler/tasks", status_code=201)
    async def scheduler_tasks_create(body: Dict[str, Any]):
        name = str(body.get("name") or "").strip()
        workflow_id = str(body.get("workflow_id") or "").strip()
        if not name or not workflow_id:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "name 与 workflow_id 必填"})
        if not store.get(workflow_id):
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"}
            )
        cron = str(body.get("cron") or "").strip()
        interval_minutes = body.get("interval_minutes")
        if cron and isinstance(interval_minutes, (int, float)) and interval_minutes > 0:
            raise HTTPException(
                status_code=400, detail={"code": "bad_request", "message": "cron 与 interval_minutes 只能二选一"}
            )
        try:
            if cron:
                task_id = scheduler.add_cron_task(
                    name,
                    workflow_id,
                    cron,
                    _make_task_executor(workflow_id),
                    description=str(body.get("description") or ""),
                )
            elif isinstance(interval_minutes, (int, float)) and interval_minutes > 0:
                task_id = scheduler.add_interval_task(
                    name,
                    workflow_id,
                    minutes=int(interval_minutes),
                    executor=_make_task_executor(workflow_id),
                    description=str(body.get("description") or ""),
                )
            else:
                raise HTTPException(
                    status_code=400, detail={"code": "bad_request", "message": "cron 或 interval_minutes 必填其一"}
                )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": f"触发器无效: {e}"}) from e
        t = scheduler.get_task(task_id)
        return {"id": task_id, "name": name, "workflow_id": workflow_id, "status": t.status.value if t else "active"}

    @app.post("/api/scheduler/tasks/{task_id}/pause")
    async def scheduler_tasks_pause(task_id: str):
        if not scheduler.pause_task(task_id):
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"任务不存在: {task_id}"})
        return {"id": task_id, "status": "paused"}

    @app.post("/api/scheduler/tasks/{task_id}/resume")
    async def scheduler_tasks_resume(task_id: str):
        if not scheduler.resume_task(task_id):
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"任务不存在: {task_id}"})
        return {"id": task_id, "status": "active"}

    @app.delete("/api/scheduler/tasks/{task_id}")
    async def scheduler_tasks_delete(task_id: str):
        if not scheduler.remove_task(task_id):
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"任务不存在: {task_id}"})
        return {"deleted": True}

    # ── W7 NL 委托 (migration doc §6 P1) ──
    @app.post("/api/ai/generate-workflow")
    async def ai_generate_workflow(body: Dict[str, Any]):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "prompt 必填"})
        context = body.get("context") if isinstance(body.get("context"), dict) else None
        # W17: 可选 model 覆盖 — 用请求自带 mlx 客户端构造临时生成器 (不改共享实例状态)
        model = str(body.get("model") or "").strip()
        generator = nl_generator
        if model:
            from ..ai.nl_parser import NLWorkflowGenerator

            generator = NLWorkflowGenerator(getattr(nl_generator, "mlx", None), model=model)
        logger.info(f"web API NL 生成请求: {prompt[:80]} model={model or '(auto)'}")
        try:
            workflow = await generator.generate(prompt, context)
        except Exception as e:
            logger.error(f"web API NL 生成失败: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": f"生成失败: {e}"}) from e
        if not isinstance(workflow, dict) or not workflow.get("nodes"):
            raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": "生成结果缺少 nodes"})
        return {"workflow": workflow}

    # ── W17 模型目录 (migration doc §6 P3) ──
    @app.get("/api/models")
    async def models_list():
        mlx = getattr(nl_generator, "mlx", None)
        if mlx is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "模型客户端不可用"})
        try:
            models = await mlx.list_models()
        except Exception as e:
            logger.warning(f"web API 模型列表获取失败 (fusion-mlx 不在线?): {e}")
            raise HTTPException(
                status_code=503, detail={"code": "upstream_error", "message": f"模型列表获取失败: {e}"}
            ) from e
        return {"models": models, "count": len(models)}

    # ── W20 插件面板 — /rpc 端点 (rpc_bridge plugins/* 委托, 依赖缺则 -32603) ──
    @app.post("/rpc")
    async def rpc_endpoint(request: Request):
        from .rpc_bridge import dispatch_rpc

        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"/rpc JSON 解析失败: {e}")
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                status_code=400,
            )
        return JSONResponse(await dispatch_rpc(body))

    # ── W11 会话统一列表 (migration doc §6 P1) ──
    @app.get("/api/sessions")
    async def sessions_list(status: str = "", limit: int = 50):
        sessions = session_store.list_sessions(status=status or None, limit=min(max(limit, 1), 200))
        return {"sessions": [_serialize_session(s) for s in sessions], "count": len(sessions)}

    @app.get("/api/sessions/{session_id}")
    async def sessions_get(session_id: str):
        s = session_store.get(session_id)
        if not s:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"会话不存在: {session_id}"})
        return _serialize_session(s)

    # ── P2 W12/W13 空间协作面 — space 域子应用挂载 (含 SSE / WS / artifacts) ──
    app.mount("/api/space", space_api)

    dist = Path(dist_dir) if dist_dir else _WEB_DIR / "dist"
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="spa")
        logger.info(f"web SPA 已挂载: {dist}")
    else:
        logger.info(f"web/dist 未构建, 静态托管跳过: {dist}")

    return app


def _version() -> str:
    from .. import __version__

    return __version__
