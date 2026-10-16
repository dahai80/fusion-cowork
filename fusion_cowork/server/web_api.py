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


def create_web_app(engine: Any = None, dist_dir: Optional[Path] = None, store_root: Optional[Path] = None):
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

    app = FastAPI(title="Fusion-Cowork Web API", version="0.1.0")

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
