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


def _module_key() -> str:
    # issue #168 (K4): 网关注册时签发的 fm- 模块密钥 (0600 落盘); 未签发返回空。
    try:
        from .shell_registry import load_module_key

        return load_module_key()
    except Exception:
        return ""


def _accepted_keys() -> List[str]:
    # 双活窗口: env 静态密钥与网关签发密钥同时有效 (24h grace 对齐 gateway K1)。
    keys = [k for k in (_api_key(), _module_key()) if k]
    return list(dict.fromkeys(keys))


def auth_configured() -> bool:
    return bool(_accepted_keys())


def check_static_key(key: str) -> Optional[Dict[str, Any]]:
    accepted = _accepted_keys()
    if not accepted or not key:
        return None
    if key == _api_key():
        return {"valid": True, "key_id": "static-env", "permissions": ["admin"]}
    if key == _module_key():
        return {"valid": True, "key_id": "module-issued", "permissions": ["admin"]}
    return None


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


_TEXT_EXTS = {".md", ".markdown", ".txt", ".html", ".htm", ".csv", ".json", ".yaml", ".yml"}
_MAX_ARTIFACT_BYTES = 64 * 1024
_MAX_ARTIFACTS = 20

# issue #142: 逐块审阅哨兵 — accepted_blocks 全拒 → confirm 返回 confirmed=False
_EDIT_ALL_REJECTED = object()


def _narrow_pending_edit(permission_manager: Any, action_id: str, accepted_raw: List[Any]) -> Any:
    # 逐块接受/拒绝 → 用待审批条目的原始 old/new 对重建仅含接受块的 new 值。
    # 返回 None 表示无窄化 (全接受/无编辑对); _EDIT_ALL_REJECTED 表示全部拒绝。
    try:
        entries = permission_manager.list_pending()
    except Exception as e:
        logger.warning(f"逐块审阅: 读取待审批队列失败: {e}")
        return None
    entry = next((e for e in entries if isinstance(e, dict) and e.get("action_id") == action_id), None)
    if not entry:
        return None
    raw_edit = entry.get("raw_edit") or {}
    old_val = raw_edit.get("old")
    new_val = raw_edit.get("new")
    if not isinstance(old_val, str) or not isinstance(new_val, str):
        return None
    try:
        from ..engine.ast_diff import apply_block_selection, text_diff_blocks

        blocks = text_diff_blocks(old_val, new_val)
        non_equal = {b["index"] for b in blocks if b["tag"] != "equal"}
        accepted = {int(i) for i in accepted_raw if isinstance(i, (int, float)) and float(i).is_integer()}
    except Exception as e:
        logger.warning(f"逐块审阅: 分块重建失败: {e}")
        return None
    selected = accepted & non_equal
    if not selected:
        return _EDIT_ALL_REJECTED
    if selected == non_equal:
        return None
    edited_new = apply_block_selection(old_val, new_val, sorted(selected))
    edited = dict(entry.get("params") or {})
    edited[str(raw_edit.get("new_key") or "new_text")] = edited_new
    logger.info(
        f"逐块审阅: action={action_id} 接受 {len(selected)}/{len(non_equal)} 块, 窄化 {raw_edit.get('new_key')}"
    )
    return edited


def _iso_to_epoch(value: Any) -> float:
    """ISO 时间字符串转 epoch; 非法/空值返回 0。"""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _artifact_type(suffix: str) -> str:
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix in (".html", ".htm"):
        return "html"
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix in (".diff", ".patch"):
        # issue #142: diff/patch 文件以 unified diff 渲染
        return "diff"
    return "text"


def _step_diff_content(step: Any, out: Dict[str, Any]) -> str:
    # issue #142: diff 产物来源 — 输出自带 diff 字段, 或步骤输入含编辑对 (old/new)
    if isinstance(out.get("diff"), str) and out["diff"].strip():
        return out["diff"]
    inp = getattr(step, "input_data", None) or {}
    if not isinstance(inp, dict):
        return ""
    from ..engine.ast_diff import unified_diff_text

    for old_key, new_key in (("old_text", "new_text"), ("old_string", "new_string")):
        old_val = inp.get(old_key)
        new_val = inp.get(new_key)
        if isinstance(old_val, str) and isinstance(new_val, str) and (old_val or new_val):
            target = next(
                (inp.get(k) for k in ("file_path", "path") if isinstance(inp.get(k), str) and inp.get(k)),
                "",
            )
            return unified_diff_text(old_val, new_val, fromfile=target or old_key, tofile=target or new_key)
    return ""


def _collect_artifacts(steps: List[Any]) -> List[Dict[str, Any]]:
    """W10: 从步骤输出提取可预览产物 (文件路径优先, 其次内联文本)。"""
    artifacts: List[Dict[str, Any]] = []
    for step in steps or []:
        if len(artifacts) >= _MAX_ARTIFACTS:
            break
        out = getattr(step, "output_data", None) or {}
        if not isinstance(out, dict):
            continue
        node_id = getattr(step, "node_id", "")
        node_name = getattr(step, "node_display_name", "") or getattr(step, "node_name", "")
        # issue #150: 步骤输出的结构化引用 (file/message refs) 随产物透出
        citations = out.get("citations") if isinstance(out.get("citations"), list) else []
        # issue #142: 编辑型步骤产出 unified diff 卡 — apply_edit 的 old/new 对 → diff 产物
        diff_content = _step_diff_content(step, out)
        if diff_content:
            artifacts.append(
                {
                    "artifact_id": f"{node_id}:diff",
                    "node_id": node_id,
                    "node_name": node_name,
                    "name": f"{node_name} · diff",
                    "type": "diff",
                    "path": "",
                    "content": diff_content[:_MAX_ARTIFACT_BYTES],
                    "truncated": len(diff_content) > _MAX_ARTIFACT_BYTES,
                }
            )
        path_val = next(
            (out[k] for k in ("file_path", "output_path", "path") if isinstance(out.get(k), str) and out.get(k)),
            "",
        )
        if path_val:
            p = Path(path_val).expanduser()
            suffix = p.suffix.lower()
            item: Dict[str, Any] = {
                "artifact_id": f"{node_id}:path",
                "node_id": node_id,
                "node_name": node_name,
                "name": p.name,
                "type": _artifact_type(suffix),
                "path": str(p),
                "content": "",
                "truncated": False,
                "citations": citations,
            }
            if suffix in _TEXT_EXTS:
                try:
                    raw = p.read_text(encoding="utf-8", errors="replace")
                    item["content"] = raw[:_MAX_ARTIFACT_BYTES]
                    item["truncated"] = len(raw) > _MAX_ARTIFACT_BYTES
                except OSError as e:
                    logger.warning(f"产物读取失败 {p}: {e}")
            artifacts.append(item)
            continue
        for key in ("markdown", "html", "content", "report", "text"):
            val = out.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            artifacts.append(
                {
                    "artifact_id": f"{node_id}:{key}",
                    "node_id": node_id,
                    "node_name": node_name,
                    "name": f"{node_name} · {key}",
                    "type": "markdown" if key in ("markdown", "report") else key,
                    "path": "",
                    "content": val[:_MAX_ARTIFACT_BYTES],
                    "truncated": len(val) > _MAX_ARTIFACT_BYTES,
                    "citations": citations,
                }
            )
            break
    return artifacts


class RunRegistry:
    def __init__(self, engine: Any, event_emitter: Any = None, run_store: Any = None):
        self._engine = engine
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # B2: 运行事件订阅 — 每个 SSE 客户端一个队列
        self._subs: Dict[str, List[asyncio.Queue]] = {}
        self._emitter = event_emitter
        # issue #191: 运行控制状态持久层 (未注入 = 纯内存, 零行为变化)
        self.run_store = run_store
        # issue #154: webhook push 出口 (未注入 = 不推送, 零行为变化)
        self.push = None
        if event_emitter is not None:
            event_emitter.on_event(self._on_event)

    @staticmethod
    def _public(record: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in record.items() if k != "workflow_def"}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        queues = self._subs.get(run_id)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._subs.pop(run_id, None)

    def _broadcast(self, run_id: str, item: Dict[str, Any]) -> None:
        for queue in list(self._subs.get(run_id, [])):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning(f"SSE 队列已满, 丢帧: {run_id}")

    def _on_event(self, event: Any) -> None:
        run_id = getattr(event, "execution_id", "")
        record = self._runs.get(run_id)
        if record is None:
            return
        try:
            execution = self._engine.get_execution(run_id)
        except Exception:
            execution = None
        if execution is not None:
            record["steps"] = [RunRegistry._step_dict(s) for s in execution.steps]
            status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
            # issue #191: paused 记录不被运行事件翻回 running —
            # 引擎暂停检查点/HITL 边界不改 execution.status, 状态翻转只由 resume() 显式驱动
            if record.get("status") != "paused":
                record["status"] = status
        self._broadcast(
            run_id,
            {
                "event": getattr(event, "event_type", "run_update"),
                "node_id": getattr(event, "node_id", ""),
                "at": getattr(event, "timestamp", time.time()),
                "run": self._public(record),
            },
        )
        # issue #191: 事件即检查点 — 每个步骤事件后持久化进度+控制状态
        self._persist(run_id)

    def _persist(self, run_id: str) -> None:
        # issue #191: 活跃 run 落 SQLite; 失败只 WARN 不影响执行 (持久化是旁路)
        if self.run_store is None:
            return
        record = self._runs.get(run_id)
        if record is None or record.get("status") not in ("running", "paused"):
            return
        paused_reason = str(record.get("paused_reason") or "")
        if record.get("status") == "running":
            try:
                if self._engine.hitl_pending(run_id):
                    paused_reason = "hitl"
            except Exception:
                pass
        try:
            self.run_store.save(record, paused_reason=paused_reason)
        except Exception as e:
            logger.warning(f"运行状态持久化失败 (忽略) {run_id}: {e}")

    @staticmethod
    def _step_dict(s: Any) -> Dict[str, Any]:
        return {
            "node_id": s.node_id,
            "node_name": s.node_name,
            "node_display_name": s.node_display_name,
            "status": s.status.value if hasattr(s.status, "value") else str(s.status),
            "execution_time": s.execution_time,
            "summary": s.summary,
            "error": s.error,
            "output_data": s.output_data,
            "input_data": s.input_data,
            # issue #143: per-node token 用量 (None = 节点无 LLM 调用)
            "tokens": getattr(s, "tokens", None),
            # issue #182: 重试可观测 — 最终尝试序号 + 逐次尝试错误
            "attempt": getattr(s, "attempt", 1),
            "attempt_errors": getattr(s, "attempt_errors", []),
        }

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
                "review": r.get("review"),
            }
            for r in sorted(self._runs.values(), key=lambda x: -x["created_at"])
        ]

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._runs.get(run_id)

    def start(
        self,
        workflow_def: Dict[str, Any],
        workflow_id: str = "",
        workflow_name: str = "",
        memory: bool = True,
    ) -> Dict[str, Any]:
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
            "review": None,
            "workflow_def": workflow_def,
            "artifacts": [],
            # issue #157: per-run 记忆开关 (任务启动时设定), 默认开
            "memory": bool(memory),
            # issue #191: 暂停原因 (manual|hitl|crash), 恢复语义用
            "paused_reason": "",
        }
        self._runs[run_id] = record
        self._persist(run_id)
        task = asyncio.create_task(self._execute(run_id, wf))
        self._tasks[run_id] = task
        logger.info(f"工作流运行已启动: {run_id} ({record['workflow_name']}) memory={record['memory']}")
        return {"run_id": run_id, "status": "running"}

    async def _execute(
        self,
        run_id: str,
        wf: Any,
        start_paused: bool = False,
        resume_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        record = self._runs[run_id]
        try:
            # issue #157: run 级记忆集成 — 启动召回注入首节点 / 结束轨迹写回。
            # 桥不可用 (未配 key) 或开关关 → 行为与旧版完全一致; 桥失败零冲击 run。
            bridge = None
            if record.get("memory", True):
                from ..memory_bridge import MemoryBridge

                bridge = MemoryBridge.from_env()
            initial_input: Optional[Dict[str, Any]] = None
            # issue #191: 恢复续跑不再重复召回 — 首轮已注入过, 再注入会覆盖已完成首节点的快照输出
            if bridge is not None and not resume_steps:
                query = f"{wf.name} {getattr(wf, 'description', '') or ''}".strip()
                ctx = await bridge.retrieve_context(query)
                if ctx:
                    record["memory_context"] = ctx
                    initial_input = {"memory_context": ctx}
                    logger.info(f"[{run_id}] 记忆上下文已注入首节点输入 ({len(ctx)} chars)")
            # issue #191: 仅在恢复场景传新参 — 注入的旧版/测试引擎无这两个 kwarg 也不破
            engine_kwargs: Dict[str, Any] = {}
            if resume_steps is not None:
                engine_kwargs["resume_steps"] = resume_steps
            if start_paused:
                engine_kwargs["start_paused"] = True
            execution = await self._engine.execute(
                wf,
                initial_input=initial_input,
                execution_id=run_id,
                **engine_kwargs,
            )
            record["steps"] = [RunRegistry._step_dict(s) for s in execution.steps]
            record["artifacts"] = _collect_artifacts(execution.steps)
            status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
            record["status"] = status
            record["result"] = {
                "summary": execution.result_summary,
            }
            if execution.error:
                record["error"] = execution.error
            # issue #157: 成功 run 结束写回 (失败/取消不入库 — 只沉淀有效产出)
            if bridge is not None and status == "success":
                from ..memory_bridge import MemoryBridge

                interaction = MemoryBridge.build_interaction(
                    session_id=run_id,
                    workflow_id=record["workflow_id"],
                    workflow_name=record["workflow_name"],
                    description=getattr(wf, "description", "") or "",
                    steps=execution.steps,
                    result_summary=execution.result_summary or "",
                    execution_id=run_id,
                )
                ids = await bridge.write_back(interaction)
                record["memory_writeback"] = "ok" if ids is not None else "failed"
                if ids is not None:
                    record["memory_ids"] = ids
        except asyncio.CancelledError:
            record["status"] = "cancelled"
        except Exception as e:
            logger.error(f"工作流运行失败 {run_id}: {e}")
            record["status"] = "failed"
            record["error"] = str(e)
        finally:
            record["finished_at"] = time.time()
            # issue #191: 终态出库 — run 结束后不再需要恢复, 持久层只留活跃 run
            if self.run_store is not None:
                try:
                    self.run_store.delete(run_id)
                except Exception as e:
                    logger.warning(f"运行状态清理失败 (忽略) {run_id}: {e}")
            # W9: 成功完成的运行进入交付审查队列; 失败/取消不进入
            if record["status"] == "success" and record.get("review") is None:
                record["review"] = {"status": "pending_review", "note": "", "reviewed_at": None}
                logger.info(f"运行待交付审查: {run_id}")
            self._broadcast(run_id, {"event": "run_end", "at": time.time(), "run": self._public(record)})
            self._tasks.pop(run_id, None)
            # issue #154: run 终态 webhook 掬点 (未注入 = 零行为变化)
            if self.push is not None:
                push = self.push
                record_ref = record
                asyncio.create_task(
                    push.push_event(
                        "run_finished",
                        {
                            "run_id": run_id,
                            "workflow_name": record_ref.get("workflow_name", ""),
                            "status": record_ref.get("status", ""),
                            "error": record_ref.get("error"),
                        },
                    )
                )

    def review(self, run_id: str, approved: bool, note: str = "") -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        """W9: 交付审查 — pending_review → approved/rejected (带意见)。"""
        record = self._runs.get(run_id)
        if not record:
            return "not_found", None
        review = record.get("review")
        if not review or review.get("status") != "pending_review":
            return "not_pending", None
        review["status"] = "approved" if approved else "rejected"
        review["note"] = note
        review["reviewed_at"] = time.time()
        record["review"] = review
        self._broadcast(run_id, {"event": "run_reviewed", "at": time.time(), "run": self._public(record)})
        logger.info(f"运行交付审查: {run_id} status={review['status']} note_len={len(note)}")
        result: Dict[str, Any] = {"run_id": run_id, "review": review}
        if not approved:
            # B1: 退回附工作流定义, 供客户端直接编辑迭代
            result["workflow"] = record.get("workflow_def")
        return None, result

    def cancel(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if not record or record["status"] not in ("running", "paused"):
            return False
        self._engine.cancel(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        logger.info(f"工作流运行已取消: {run_id}")
        return True

    def pause(self, run_id: str) -> bool:
        # W8: 暂停 — 引擎挂起在下一节点检查点, 记录状态 paused (取消亦允许自 paused)
        record = self._runs.get(run_id)
        if not record or record["status"] != "running":
            return False
        if not self._engine.pause(run_id):
            return False
        record["status"] = "paused"
        record["paused_reason"] = "manual"
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_paused", "at": time.time(), "run": self._public(record)})
        logger.info(f"工作流运行已暂停: {run_id}")
        return True

    def resume(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if not record or record["status"] != "paused":
            return False
        if not self._engine.resume(run_id):
            record["status"] = "running"
            logger.warning(f"引擎无暂停事件, 记录状态回退 running: {run_id}")
            self._persist(run_id)
            return True
        record["status"] = "running"
        record["paused_reason"] = ""
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_resumed", "at": time.time(), "run": self._public(record)})
        logger.info(f"工作流运行已恢复: {run_id}")
        return True

    DIRECTIVE_ACTIONS = ("set_params", "insert_node", "skip_remaining")

    def directive(self, run_id: str, directive: Dict[str, Any]) -> Optional[str]:
        """W8/B1: 排队运行改向指令; 返回 None 表示已排队, 否则错误码。"""
        record = self._runs.get(run_id)
        if not record or record["status"] not in ("running", "paused"):
            return "not_running"
        action = str((directive or {}).get("action", ""))
        if action not in self.DIRECTIVE_ACTIONS:
            return "bad_action"
        if not self._engine.add_directive(run_id, directive):
            return "rejected"
        # issue #191: 全量 payload 落 record — 恢复重放需要完整指令 (仅 action 不够)
        record.setdefault("directives", []).append({"action": action, "at": time.time(), "payload": dict(directive)})
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_directive", "at": time.time(), "run": self._public(record)})
        logger.info(f"运行改向指令已排队: {run_id} action={action}")
        return None

    async def recover_persisted(self) -> int:
        # issue #191: daemon 重启恢复 — SQLite 活跃行 → 重建 record → start_paused 续跑。
        # 唯一恢复路径: 步骤进度 (resume_steps) 与控制状态 (paused/directives) 同路还原。
        if self.run_store is None:
            return 0
        try:
            rows = self.run_store.load_active()
        except Exception as e:
            logger.error(f"加载持久化运行状态失败 (跳过恢复): {e}")
            return 0
        recovered = 0
        for row in rows:
            run_id = str(row.get("run_id", ""))
            if not run_id or run_id in self._runs:
                continue
            try:
                wf_def = json.loads(row.get("workflow_def") or "{}")
                steps = json.loads(row.get("steps") or "[]")
                directives = json.loads(row.get("directives") or "[]")
                from ..engine import Workflow

                wf = Workflow.from_dict(wf_def)
                if not wf.nodes:
                    raise ValueError("workflow_def 无有效节点")
            except Exception as e:
                logger.error(f"恢复运行 {run_id} 失败: 持久化数据无法解析, 已废弃该行: {e}")
                self.run_store.delete(run_id)
                continue
            # 持久化状态 running = 上次会话非正常退出 (kill -9/崩溃) — loud 恢复为 paused
            crashed = str(row.get("status", "")) != "paused"
            record: Dict[str, Any] = {
                "run_id": run_id,
                "workflow_id": row.get("workflow_id", ""),
                "workflow_name": row.get("workflow_name", ""),
                "status": "paused",
                "paused_reason": "crash" if crashed else str(row.get("paused_reason") or "manual"),
                "created_at": float(row.get("created_at") or time.time()),
                "finished_at": None,
                "steps": steps,
                "result": None,
                "error": None,
                "review": None,
                "workflow_def": wf_def,
                "artifacts": [],
                "memory": bool(row.get("memory", 1)),
                "recovered": True,
                "directives": [],
            }
            self._runs[run_id] = record
            task = asyncio.create_task(self._execute(run_id, wf, start_paused=True, resume_steps=steps))
            self._tasks[run_id] = task
            # 等引擎登记 pause 事件后再重放指令 (add_directive 校验 _pause_events 存在)
            for _ in range(200):
                if run_id in getattr(self._engine, "_pause_events", {}):
                    break
                await asyncio.sleep(0.02)
            # 恢复期间重放持久化的改向指令 — resume 后首个节点边界生效
            replayed = 0
            for d in directives:
                if not isinstance(d, dict):
                    continue
                payload = d.get("payload") or d
                if self._engine.add_directive(run_id, payload):
                    record.setdefault("directives", []).append(d)
                    replayed += 1
            if replayed:
                logger.info(f"[{run_id}] 恢复期间重放改向指令 {replayed} 条")
            if crashed:
                logger.warning(
                    f"[{run_id}] 上次会话非正常退出 (持久化状态 running), 已恢复为 paused "
                    f"({len(steps)} 步快照), 等待人工 resume/审批"
                )
            else:
                logger.info(f"[{run_id}] 恢复暂停中的运行: paused_reason={record['paused_reason']}")
            self._persist(run_id)
            recovered += 1
        return recovered

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
    # W13/B2: chat 服务与 SSE 端点共用同一事件总线 (此前 chat 自建 emitter,
    # /stream 的 _event_emitter 为 None, SSE 只会发 ping)
    from ..engine.events import EventEmitter

    emitter = EventEmitter()
    chat_svc = SpaceChatService(store, mlx, perm, event_emitter=emitter)
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
        event_emitter=emitter,
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


def _make_pending_push_callback(push_egress):
    """issue #154: 审批入队 → webhook 推送回调工厂 (best-effort, 不抛)。

    在事件循环内被调用 (engine 权限检查 await 链), 直接 create_task;
    无事件循环时跳过 — webhook 是 best-effort, 不为它开新循环。
    """

    def _cb(info: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("push 回调无事件 loop, 跳过 (best-effort)")
            return
        loop.create_task(push_egress.push_event("approval_requested", info))

    return _cb


def _otlp_logs(records: List[Dict[str, Any]], scope: str = "fusion-cowork.audit") -> Dict[str, Any]:
    """issue #156: 审计记录 → OTLP/JSON LogsData (SIEM collector 可直接消费)。"""
    log_records: List[Dict[str, Any]] = []
    for rec in records:
        attrs = [{"key": k, "value": {"stringValue": str(v)}} for k, v in (rec.get("attributes") or {}).items()]
        log_records.append(
            {
                "timeUnixNano": str(int(float(rec.get("time_unix_nano") or 0))),
                "severityText": str(rec.get("severity") or "INFO"),
                "body": {"stringValue": str(rec.get("body") or "")},
                "attributes": attrs,
            }
        )
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "fusion-cowork"}},
                    ]
                },
                "scopeLogs": [{"scope": {"name": scope}, "logRecords": log_records}],
            }
        ]
    }


async def _takeover_session(ws, run_id: str, runs: Any, permission_manager: Any) -> None:
    """issue #154: 接管会话主循环 — 订阅 run 事件 + 处理转向帧。

    帧: {"method": "directive"|"pause"|"resume"|"cancel"|"confirm", "params": {...}}
    """
    queue = runs.subscribe(run_id)
    record = runs.get(run_id)
    snapshot = RunRegistry._public(record) if record is not None else {}
    await ws.send_json({"event": "snapshot", "run": snapshot})
    pump = asyncio.create_task(_pump_run_events(ws, queue))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"event": "error", "message": "invalid json"})
                continue
            method = str(frame.get("method", ""))
            params = frame.get("params") or {}
            reply = await _takeover_apply(method, params, run_id, runs, permission_manager)
            await ws.send_json(reply)
    finally:
        runs.unsubscribe(run_id, queue)
        pump.cancel()


async def _takeover_apply(method: str, params: dict, run_id: str, runs: Any, permission_manager: Any) -> dict:
    """issue #154: 转向帧分发 — 复用 RunRegistry + permission manager, 返回回执帧。"""
    if method == "directive":
        action = str(params.get("action", ""))
        if action not in ("set_params", "insert_node", "skip_remaining"):
            return {"event": "error", "message": f"directive rejected: unknown action {action}"}
        err = runs.directive(run_id, params)
        if err:
            return {"event": "error", "message": f"directive rejected: {err}"}
        return {"event": "directive_queued", "action": str(params.get("action", ""))}
    if method == "pause":
        ok = runs.pause(run_id)
        return {"event": "paused"} if ok else {"event": "error", "message": "not running"}
    if method == "resume":
        ok = runs.resume(run_id)
        return {"event": "resumed"} if ok else {"event": "error", "message": "not paused"}
    if method == "cancel":
        ok = runs.cancel(run_id)
        return {"event": "cancelled"} if ok else {"event": "error", "message": "not cancelable"}
    if method == "confirm":
        if permission_manager is None:
            return {"event": "error", "message": "权限管理器不可用"}
        action_id = str(params.get("action_id", ""))
        approved = bool(params.get("approved"))
        confirmed = await permission_manager.confirm_guard(
            action_id, approved, approved_by="takeover", scope=str(params.get("scope") or "*")
        )
        if confirmed:
            try:
                saver = getattr(permission_manager, "save", None)
                if callable(saver):
                    saver()
            except Exception as e:
                logger.warning(f"takeover confirm 规则保存失败: {e}")
        return {"event": "confirm_result", "action_id": action_id, "approved": approved, "confirmed": confirmed}
    return {"event": "error", "message": f"unknown method: {method}"}


async def _pump_run_events(ws, queue) -> None:
    """issue #154: run 事件泵 — RunRegistry 订阅队列 → WS 帧。断连/取消即退。"""
    while True:
        item = await queue.get()
        try:
            await ws.send_json(item)
        except Exception:
            return


def create_web_app(
    engine: Any = None,
    dist_dir: Optional[Path] = None,
    store_root: Optional[Path] = None,
    scheduler: Any = None,
    nl_generator: Any = None,
    session_store: Any = None,
    space_api: Any = None,
    space_data_dir: Optional[Path] = None,
    permission_manager: Any = None,
    cross_sync: Any = None,
    skills_dir: Optional[Path] = None,
    rag_base_url: Optional[str] = None,
    rag_wired_path: Optional[Path] = None,
    run_store: Any = None,
):
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket
        from fastapi.responses import JSONResponse, Response, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise ImportError("web API 需要 [web] 依赖: pip install fusion-cowork[web]") from e

    from ..nodes import import_all_nodes

    import_all_nodes()
    # ai-hr-automation: load trusted plugins so plugin nodes (hr_http) join the
    # serve process registry. deny-closed: only plugins.trusted whitelist passes
    # the loader's CR-21 check; load failures are logged, not fatal.
    try:
        from ..plugins import get_plugin_loader

        for _name, _plugin_nodes in get_plugin_loader().load_all().items():
            logger.info(f"插件节点已加载: {_name} -> {[n.name for n in _plugin_nodes]}")
    except Exception as e:
        logger.warning(f"插件加载失败 (忽略): {e}")
    store = WorkflowDefStore(root=store_root)
    if permission_manager is None:
        from ..engine import PermissionManager

        permission_manager = PermissionManager()
        # issue #138: 审批规则持久化 — serve 启动加载已批准/拒绝规则
        loader = getattr(permission_manager, "load", None)
        if callable(loader):
            try:
                loader()
            except Exception as e:
                logger.warning(f"web API 权限规则加载失败 (继续空规则): {e}")
    if engine is None:
        from ..engine import WorkflowEngine
        from ..engine.events import EventEmitter

        emitter = EventEmitter()
        engine = WorkflowEngine(permission_manager=permission_manager, event_emitter=emitter)
    else:
        permission_manager = getattr(engine, "_permission_manager", None) or permission_manager
        emitter = getattr(engine, "_event_emitter", None)
    # issue #191: 运行控制状态持久层 — SQLite 落盘, daemon 重启可恢复
    from ..engine.run_persistence import RunStateStore

    if run_store is None:
        run_store = RunStateStore(Path(store_root) / "run_states.db" if store_root else None)
    runs = RunRegistry(engine, event_emitter=emitter, run_store=run_store)
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
    app.state.permission_manager = permission_manager
    app.state.run_registry = runs
    # W22: 跨设备同步引擎接缝 — 注入后 /api/deliver 可投递; 默认 None (503 诚实不可用)
    app.state.cross_sync = cross_sync

    # issue #154: webhook push 出口 (未配 FUSION_COWORK_PUSH_WEBHOOK_URL = no-op)
    from .push import PushEgress

    push_egress = PushEgress.from_env()
    app.state.push_egress = push_egress
    runs.push = push_egress
    # issue #154: 审批入队 → webhook 推送 (permission manager 回调, best-effort)
    if permission_manager is not None:
        permission_manager.on_pending_created = _make_pending_push_callback(push_egress)

    # issue #191: 启动即恢复 — SQLite 里的活跃 run (paused/崩溃残留 running) 重建为 paused, 等人工 resume/审批
    @app.on_event("startup")
    async def _recover_runs():
        recovered = await runs.recover_persisted()
        if recovered:
            logger.warning(f"启动恢复: 从持久层重建 {recovered} 个活跃 run (均以 paused 起跑, 等待人工 resume/审批)")

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        if request.url.path in ("/health",) or request.url.path.startswith("/ws/"):
            return await call_next(request)
        # #146: 未配置密钥 = 放行 (对齐 cli.py/README "unset = allow" 契约) —
        # 旧行为无条件 401 导致 shell 登录弹回循环。
        if not auth_configured():
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
        try:
            # issue #157: per-run 记忆开关, 默认开 (未配 FUSION_MEMORY_API_KEY 时桥自动关闭)
            memory = body.get("memory", True)
            if not isinstance(memory, bool):
                raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "memory 须为 boolean"})
            return runs.start(workflow_def, workflow_id=workflow_id, workflow_name=workflow_name, memory=memory)
        except ValueError as exc:
            # issue #135 F-2: 坏边形状/含环等工作流定义错误 → 400 可读信息, 不 500
            raise HTTPException(
                status_code=400, detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"}
            ) from exc

    @app.get("/api/runs")
    async def runs_list():
        return {"runs": runs.list(), "count": len(runs.list())}

    @app.get("/api/runs/{run_id}")
    async def runs_get(run_id: str):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"})
        # issue #181: HITL 等待中标记 — 前端提示"等待人工审批"而非卡死
        # issue #187: 附带截止时间 — 审批卡倒计时; 状态服务端持久, 页面刷新不丢
        if record.get("status") == "running":
            record["hitl_pending"] = engine.hitl_pending(run_id)
            if record["hitl_pending"]:
                deadline_getter = getattr(engine, "hitl_deadline", None)
                deadline = deadline_getter(run_id) if callable(deadline_getter) else None
                if deadline is not None:
                    record["hitl_deadline"] = deadline
        return record

    # ── W10/B5 运行产物预览 (右栏, 实时随步骤输出刷新) ──
    @app.get("/api/runs/{run_id}/artifacts")
    async def runs_artifacts(run_id: str):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"})
        artifacts = record.get("artifacts") or []
        return {"artifacts": artifacts, "count": len(artifacts), "review": record.get("review")}

    # ── B2 运行步骤日志 SSE — events.py 总线 → 前端实时步骤 (轮询为降级) ──
    @app.get("/api/runs/{run_id}/events")
    async def runs_events(run_id: str, request: Request):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"})

        async def event_stream():
            queue = runs.subscribe(run_id)
            try:
                snapshot = json.dumps({"run": runs._public(record)}, ensure_ascii=False)
                yield f"event: snapshot\ndata: {snapshot}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=20)
                    except TimeoutError:
                        yield "event: ping\ndata: \n\n"
                        continue
                    yield f"event: {item.get('event', 'run_update')}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                runs.unsubscribe(run_id, queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def runs_cancel(run_id: str):
        if not runs.cancel(run_id):
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不可取消: {run_id}"})
        return {"status": "cancelling", "run_id": run_id}

    # ── W8 运行改向: 暂停/恢复 (追加指令需 orchestrator 交互协议, 见 migration doc I2) ──
    @app.post("/api/runs/{run_id}/pause")
    async def runs_pause(run_id: str):
        if not runs.pause(run_id):
            raise HTTPException(status_code=409, detail={"code": "not_running", "message": f"运行不可暂停: {run_id}"})
        return {"status": "paused", "run_id": run_id}

    @app.post("/api/runs/{run_id}/resume")
    async def runs_resume(run_id: str):
        if not runs.resume(run_id):
            raise HTTPException(status_code=409, detail={"code": "not_paused", "message": f"运行不可恢复: {run_id}"})
        return {"status": "running", "run_id": run_id}

    # ── W9 交付审查: 成功运行 pending_review → approved/rejected (节点级 guard 审批另属 W19) ──
    @app.post("/api/runs/{run_id}/review")
    async def runs_review(run_id: str, body: Dict[str, Any]):
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "approved 必须是布尔值"})
        err, result = runs.review(run_id, approved, str(body.get("note") or ""))
        if err == "not_found":
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"})
        if err == "not_pending":
            raise HTTPException(
                status_code=409, detail={"code": "not_pending", "message": f"运行不在待审查状态: {run_id}"}
            )
        return result

    # ── W8/B1 运行改向: 暂停态追加指令 (换参/插节点/跳过剩余) ──
    @app.post("/api/runs/{run_id}/directive", status_code=202)
    async def runs_directive(run_id: str, body: Dict[str, Any]):
        err = runs.directive(run_id, body)
        if err == "bad_action":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "bad_action",
                    "message": f"action 必须是 {', '.join(RunRegistry.DIRECTIVE_ACTIONS)}",
                },
            )
        if err == "not_running":
            raise HTTPException(
                status_code=409, detail={"code": "not_running", "message": f"运行不在运行/暂停态: {run_id}"}
            )
        if err:
            raise HTTPException(status_code=409, detail={"code": err, "message": f"运行改向被拒绝: {run_id}"})
        return {"status": "queued", "run_id": run_id, "action": body.get("action")}

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

    # ── issue #148: chat-first 委托 — 一句话直接出交付物, 不强制进画布 ──
    # 确定性启发 (Rule: 决策走代码不走模型): 计划 ≤2 节点且不含高风险节点 →
    # mode=auto 直接执行并返回 run_id (前端流式展示进度/交付物); 否则
    # mode=plan 返回内部执行计划 (仍保存为工作流), 一键批准执行; DAG 编辑
    # 永远可选 (workflow_id 可进画布)。
    @app.post("/api/ai/chat-delegate")
    async def ai_chat_delegate(body: Dict[str, Any]):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "prompt 必填"})
        context = body.get("context") if isinstance(body.get("context"), dict) else None
        model = str(body.get("model") or "").strip()
        memory = body.get("memory", True)
        generator = nl_generator
        if model:
            from ..ai.nl_parser import NLWorkflowGenerator

            generator = NLWorkflowGenerator(getattr(nl_generator, "mlx", None), model=model)
        logger.info(f"web API chat-delegate: {prompt[:80]} model={model or '(auto)'}")
        try:
            workflow = await generator.generate(prompt, context)
        except Exception as e:
            logger.error(f"web API chat-delegate 生成失败: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": f"生成失败: {e}"}) from e
        if not isinstance(workflow, dict) or not workflow.get("nodes"):
            raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": "生成结果缺少 nodes"})

        from ..engine.node import NodeRegistry
        from ..engine.permission import HIGH_RISK_NODES

        nodes = workflow.get("nodes") or []
        node_names = [str(n.get("name") or "") for n in nodes if isinstance(n, dict)]
        plan_steps = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            reg = NodeRegistry.get(str(n.get("name") or ""))
            display = getattr(reg, "display_name", "") or str(n.get("name") or "")
            plan_steps.append(
                {"node": n.get("name"), "display": display, "label": (n.get("config") or {}).get("label", "")}
            )
        simple = len(nodes) <= 2 and not any(name in HIGH_RISK_NODES for name in node_names)

        workflow_id = f"wf_{uuid.uuid4().hex[:10]}"
        record = store.save(
            workflow_id,
            {
                "name": workflow.get("name") or prompt[:40],
                "description": workflow.get("description") or prompt[:120],
                "workflow": {"name": workflow.get("name") or "", "nodes": nodes, "edges": workflow.get("edges") or []},
            },
        )
        plan = {
            "name": record.get("name", workflow_id),
            "description": record.get("description", ""),
            "steps": plan_steps,
        }

        if simple:
            try:
                run = runs.start(
                    {"name": workflow.get("name") or "", "nodes": nodes, "edges": workflow.get("edges") or []},
                    workflow_id=workflow_id,
                    workflow_name=record.get("name", workflow_id),
                    memory=bool(memory),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"}
                ) from exc
            logger.info(f"chat-delegate 自动执行: workflow={workflow_id} run={run['run_id']} ({len(nodes)} 节点)")
            return {"mode": "auto", "workflow_id": workflow_id, "run_id": run["run_id"], "plan": plan}
        logger.info(
            f"chat-delegate 计划待批准: workflow={workflow_id} ({len(nodes)} 节点, 含高风险={any(n in HIGH_RISK_NODES for n in node_names)})"
        )
        return {"mode": "plan", "workflow_id": workflow_id, "plan": plan}

    # ── issue #150: 引用溯源 — 按路径读取文本文件 (沙箱范围内, 供引用卡展示) ──
    @app.get("/api/files/read")
    async def files_read(path: str = "", line_start: int = 0, line_end: int = 0):
        if not path:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "path 必填"})
        from ..security import get_scoped_folder_manager

        p = Path(path).expanduser()
        if not get_scoped_folder_manager().ensure_allowed(p):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": f"路径不在授权范围内: {path}"})
        if not p.is_file():
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"文件不存在: {path}"})
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(status_code=400, detail={"code": "read_error", "message": f"读取失败: {e}"}) from e
        lines = raw.splitlines()
        total_lines = len(lines)
        lo = max(int(line_start or 1), 1)
        hi = min(int(line_end or total_lines), total_lines) if line_end else total_lines
        selected = "\n".join(lines[lo - 1 : hi])[:_MAX_ARTIFACT_BYTES]
        logger.info(f"web API 文件读取: {p} [{lo}-{hi}/{total_lines}]")
        return {
            "path": str(p),
            "name": p.name,
            "content": selected,
            "line_start": lo,
            "line_end": hi,
            "total_lines": total_lines,
            "truncated": len(selected) >= _MAX_ARTIFACT_BYTES,
        }

    # ── W17 模型目录 (migration doc §6 P3; #126 起默认只回 chat 模型) ──
    # #147: 模型列表最后成功缓存 — 上游断链时返回 stale 结果而非 503,
    # 消除前端重试风暴; 下次成功自动刷新 ("服务恢复后自动回填")。
    # #172: 冷缓存 (上游一直离线) 同样不再 503 — 返回 200 空列表 + unavailable=true
    # envelope, 前端轮询拿到成功响应即不重试, 渲染空态而非刷屏报错。
    _models_cache: Dict[str, Any] = {"models": None, "ts": 0.0}

    @app.get("/api/models")
    async def models_list(request: Request):
        mlx = getattr(nl_generator, "mlx", None)
        if mlx is None:
            logger.warning("web API 模型列表: 模型客户端不可用, 返回空列表 envelope")
            return {"models": [], "count": 0, "total": 0, "stale": False, "unavailable": True}
        try:
            models = await mlx.list_models()
            _models_cache["models"] = models
            _models_cache["ts"] = time.time()
        except Exception as e:
            cached = _models_cache["models"]
            if cached is not None:
                age = int(time.time() - _models_cache["ts"])
                logger.warning(f"web API 模型列表获取失败, 返回 {age}s 前缓存 ({len(cached)} 条): {e}")
                models = cached
                stale = True
            else:
                logger.warning(f"web API 模型列表获取失败 (fusion-mlx 不在线, 无缓存): {e}")
                return {"models": [], "count": 0, "total": 0, "stale": False, "unavailable": True}
        else:
            stale = False
        # #126: 顶层模型选择器 (W17 picker / DelegatePage) 只能选 chat 模型 —
        # video/embedding 等选中后在生成期才失败。默认过滤, 原始列表经 ?all=1 获取。
        from ..ai.nl_parser import _is_chat_model

        if request.query_params.get("all") != "1":
            chat_models = [m for m in models if _is_chat_model(m)]
            logger.info(f"web API 模型列表: chat {len(chat_models)}/{len(models)} (默认过滤非 chat)")
            return {"models": chat_models, "count": len(chat_models), "total": len(models), "stale": stale}
        return {"models": models, "count": len(models), "total": len(models), "stale": stale}

    # ── issue #155: markdown skill 包 — 发现 / 参数表单 schema / 执行 ──
    @app.get("/api/skills")
    async def skills_list():
        from ..skills import get_package_registry

        packages = get_package_registry().list_packages()
        return {"skills": [p.to_dict() for p in packages], "count": len(packages)}

    @app.post("/api/skills/{name:path}/run")
    async def skills_run(name: str, request: Request):
        from ..skills import get_package_registry

        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"/api/skills/{name}/run JSON 解析失败: {e}")
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        values = body.get("params") if isinstance(body, dict) and "params" in body else body
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid_params", "message": "params 必须是对象"})
        registry = get_package_registry()
        if registry.get(name) is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知 skill 包: {name}"})
        result = await registry.run(name, values)
        if "error" in result:
            status = 400 if result.get("errors") else 500
            return JSONResponse(result, status_code=status)
        return result

    @app.get("/api/skills/{name:path}")
    async def skills_get(name: str):
        from ..skills import get_package_registry

        pkg = get_package_registry().get(name)
        if pkg is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知 skill 包: {name}"})
        return pkg.to_dict()

    # ── issue #170: Extensions console — Skills + Knowledge (f-ecommerce E2 迁移) ──
    # Skills: 用户 JSON skill 包 (~/.fusion-cowork/skills/<name>/skill.json,
    #   handler = workflow|node|script), 安装即注册进 SkillRegistry (MCP skill_run
    #   可调), 脚本包仅静态 env 密钥 (本地未配鉴权 = 本机操作员) 可装。
    # Knowledge: fusion-rag (:11436) KB 控制台 — 复用 SpaceKB 同款 KBClient 绑定,
    #   wire 状态落 ~/.fusion-cowork/web/rag_wired.json, 能力 id knowledge.<kb>.search。
    def _extensions_rag():
        from ..ai.mlx_client import KBClient

        return KBClient(base_url=(rag_base_url or os.environ.get("FUSION_RAG_URL", "http://127.0.0.1:11436")))

    def _load_wired() -> List[str]:
        path = rag_wired_path or (Path.home() / ".fusion-cowork" / "web" / "rag_wired.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            wired = data.get("wired") if isinstance(data, dict) else None
            return [str(k) for k in wired] if isinstance(wired, list) else []
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"rag wired 状态读取失败 {path}: {e}")
            return []

    def _save_wired(wired: List[str]) -> None:
        path = rag_wired_path or (Path.home() / ".fusion-cowork" / "web" / "rag_wired.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"wired": sorted(set(wired))}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _script_install_allowed(request: Request) -> bool:
        # script 包 = 任意代码 — 仅静态 env 密钥 (或本机未配鉴权) 可装;
        # 网关签发 fm- 模块密钥走浏览器面, 永远不可装脚本包。
        if not auth_configured():
            return True
        return extract_caller_key(request.headers) == _api_key() and bool(_api_key())

    def _validate_skill_doc(doc: Dict[str, Any]) -> tuple:
        from ..skills.persistence import VALID_TYPES

        problems: List[str] = []
        name = str(doc.get("name") or "").strip().strip("/")
        if not name or "/" in name or name.startswith("."):
            problems.append("missing/invalid name")
        stype = str(doc.get("type") or "node")
        if stype not in VALID_TYPES:
            problems.append(f"type must be one of {VALID_TYPES}")
        if stype == "script" and not str(doc.get("script") or "").strip():
            problems.append("script skills require the script field")
        if stype == "workflow" and not str(doc.get("template_id") or "").strip():
            problems.append("workflow skills require template_id")
        if stype == "node" and not str(doc.get("node") or "").strip():
            problems.append("node skills require the node field")
        return name, stype, problems

    @app.get("/api/extensions/skills")
    async def extensions_skills_list():
        from ..skills.persistence import list_skill_packs
        from ..skills.registry import SkillRegistry

        registered = {s.name for s in SkillRegistry().list_skills()}
        packs = list_skill_packs(skills_dir=skills_dir)
        out = [
            {
                "name": p.name,
                "description": p.description,
                "type": p.type,
                "category": p.category,
                "capability": f"skill.{p.name}",
                "cap_status": "active" if p.name in registered else "missing",
            }
            for p in packs
        ]
        return {"skills": out, "count": len(out)}

    @app.post("/api/extensions/skills")
    async def extensions_skills_install(request: Request):
        from ..skills.persistence import SkillPack, make_pack_handler, save_skill_pack
        from ..skills.registry import Skill, SkillRegistry

        try:
            doc = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        if not isinstance(doc, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid_body", "message": "body 必须是 JSON 对象"})
        name, stype, problems = _validate_skill_doc(doc)
        if problems:
            return JSONResponse({"installed": False, "problems": problems}, status_code=400)
        if stype == "script" and not _script_install_allowed(request):
            logger.warning("[extensions] script 包安装被拒 (非静态管理密钥): %s", name)
            return JSONResponse(
                {"installed": False, "problems": ["script skills are admin-only (arbitrary code)"]},
                status_code=403,
            )
        pack = SkillPack.from_manifest(doc)
        pack.name = name
        try:
            save_skill_pack(pack, skills_dir=skills_dir)
        except ValueError as e:
            return JSONResponse({"installed": False, "problems": [str(e)]}, status_code=400)
        registry = SkillRegistry()
        registry.register(
            Skill(
                name=pack.name,
                description=pack.description,
                handler=make_pack_handler(pack, skills_dir),
                category=pack.category,
                aliases=pack.aliases,
            )
        )
        logger.info("[extensions] skill %s installed (type=%s, capability=skill.%s)", name, stype, name)
        return {"installed": True, "name": name, "capability": f"skill.{name}"}

    @app.post("/api/extensions/skills/{name}/test")
    async def extensions_skills_test(name: str, request: Request):
        import time as _time

        from ..skills.persistence import list_skill_packs, make_pack_handler

        try:
            body = await request.json()
        except Exception:
            body = {}
        args = str(body.get("args", "")) if isinstance(body, dict) else ""
        pack = next((p for p in list_skill_packs(skills_dir=skills_dir) if p.name == name), None)
        if pack is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知 skill 包: {name}"})
        started = _time.monotonic()
        result = await make_pack_handler(pack, skills_dir)(args)
        failed = isinstance(result, dict) and "error" in result
        duration_ms = int((_time.monotonic() - started) * 1000)
        logger.info("[extensions] skill %s test run: ok=%s (%dms)", name, not failed, duration_ms)
        return {"ok": not failed, "result": result, "duration_ms": duration_ms}

    @app.delete("/api/extensions/skills/{name}")
    async def extensions_skills_delete(name: str):
        from ..skills.persistence import delete_skill_pack
        from ..skills.registry import SkillRegistry

        deleted = delete_skill_pack(name, skills_dir=skills_dir)
        removed = SkillRegistry().unregister(name)
        logger.info("[extensions] skill %s removed (deleted=%s, capability_removed=%s)", name, deleted, removed)
        return {"deleted": deleted, "capability_removed": removed}

    def _norm_kb_id(b: Dict[str, Any]) -> str:
        return str(b.get("id") or b.get("kb_id") or b.get("name") or "")

    @app.get("/api/extensions/rag/health")
    async def extensions_rag_health():
        client = _extensions_rag()
        try:
            resp = await client.client.get("/health")
            data = resp.json() if resp.status_code < 400 else {}
            if resp.status_code >= 400:
                return {"ok": False, "error": f"fusion-rag {resp.status_code}"}
            return {"ok": True, "version": data.get("version", ""), "status": data.get("status", "")}
        except Exception as e:
            logger.warning("[extensions] fusion-rag 不可达: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            await client.close()

    @app.get("/api/extensions/rag/kb")
    async def extensions_rag_kb_list(request: Request):
        client = _extensions_rag()
        try:
            resp = await client.client.get("/kb/bases")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("[extensions] fusion-rag KB 列表失败: %s", e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        bases = data if isinstance(data, list) else (data.get("bases") or data.get("items") or [])
        q = (request.query_params.get("q") or "").strip().lower()
        if q:
            bases = [
                b for b in bases if q in str(b.get("name", "")).lower() or q in str(b.get("description", "")).lower()
            ]
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            page_size = max(1, min(int(request.query_params.get("page_size", "10")), 50))
        except ValueError:
            page, page_size = 1, 10
        total = len(bases)
        window = bases[(page - 1) * page_size : page * page_size]
        wired = set(_load_wired())

        async def _doc_count(b: Dict[str, Any]) -> Any:
            kb_id = _norm_kb_id(b)
            c = _extensions_rag()
            try:
                r = await c.client.get(f"/kb/bases/{kb_id}/documents")
                if r.status_code >= 400:
                    return None
                docs = r.json()
                docs = docs if isinstance(docs, list) else (docs.get("documents") or docs.get("items") or [])
                return len(docs)
            except Exception:
                return None
            finally:
                await c.close()

        import asyncio as _asyncio

        counts = await _asyncio.gather(*(_doc_count(b) for b in window))
        rows = []
        for b, count in zip(window, counts):
            kb_id = _norm_kb_id(b)
            is_wired = kb_id in wired
            rows.append(
                {
                    "kb_id": kb_id,
                    "name": b.get("name", kb_id),
                    "description": b.get("description", ""),
                    "document_count": count,
                    "capability": f"knowledge.{kb_id}.search" if is_wired else None,
                    "cap_status": "active" if is_wired else None,
                }
            )
        return {"bases": rows, "total": total, "page": page, "page_size": page_size}

    @app.post("/api/extensions/rag/kb")
    async def extensions_rag_kb_create(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
        description = str(body.get("description") or "").strip() if isinstance(body, dict) else ""
        wire = bool(body.get("wire")) if isinstance(body, dict) else False
        if not name:
            raise HTTPException(status_code=400, detail={"code": "invalid_name", "message": "name 必填"})
        client = _extensions_rag()
        try:
            kb_id = await client.create_kb(name, description)
            if wire:
                _save_wired(_load_wired() + [kb_id])
        except Exception as e:
            logger.error("[extensions] fusion-rag KB 创建失败: %s", e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        logger.info("[extensions] KB %s created (wired=%s)", kb_id, wire)
        return {"kb_id": kb_id, "capability": f"knowledge.{kb_id}.search" if wire else None}

    @app.post("/api/extensions/rag/kb/{kb_id}/wire")
    async def extensions_rag_kb_wire(kb_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        wired_flag = bool(body.get("wired")) if isinstance(body, dict) else False
        wired = set(_load_wired())
        capabilities_disabled: List[str] = []
        if wired_flag:
            wired.add(kb_id)
        else:
            wired.discard(kb_id)
            capabilities_disabled = [f"knowledge.{kb_id}.search"]
        _save_wired(sorted(wired))
        logger.info("[extensions] KB %s wired=%s", kb_id, wired_flag)
        return {
            "wired": wired_flag,
            "capability": f"knowledge.{kb_id}.search" if wired_flag else None,
            "capabilities_disabled": capabilities_disabled,
        }

    @app.get("/api/extensions/rag/kb/{kb_id}/documents")
    async def extensions_rag_kb_documents(kb_id: str):
        client = _extensions_rag()
        try:
            resp = await client.list_documents(kb_id)
        except Exception as e:
            logger.warning("[extensions] fusion-rag 文档列表失败 %s: %s", kb_id, e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        docs = resp if isinstance(resp, list) else (resp.get("documents") or resp.get("items") or [])
        return {"documents": docs}

    @app.post("/api/extensions/rag/kb/{kb_id}/documents")
    async def extensions_rag_kb_upload(kb_id: str, request: Request):
        # multipart 上传: 浏览器文件先落 ~/.fusion-cowork/web/rag_uploads/<kb>/,
        # 再经 KBClient.upload_file (SpaceKB 同款绑定) 走 fusion-rag 摄取。
        try:
            form = await request.form()
        except Exception as e:
            raise HTTPException(
                status_code=400, detail={"code": "invalid_form", "message": "multipart form 解析失败"}
            ) from e
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(status_code=400, detail={"code": "missing_file", "message": "file 字段必填"})
        filename = Path(upload.filename or "").name
        if not filename or Path(filename).suffix.lower() not in (".md", ".txt"):
            raise HTTPException(status_code=400, detail={"code": "invalid_type", "message": "仅接受 .md/.txt"})
        if not kb_id or any(c in kb_id for c in "/\\") or kb_id.startswith("."):
            raise HTTPException(status_code=400, detail={"code": "invalid_kb", "message": "非法 kb_id"})
        upload_dir = Path.home() / ".fusion-cowork" / "web" / "rag_uploads" / kb_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / filename
        size = 0
        try:
            with dest.open("wb") as f:
                while chunk := await upload.read(1 << 20):
                    size += len(chunk)
                    if size > 10 << 20:
                        raise HTTPException(
                            status_code=400, detail={"code": "too_large", "message": "文件超过 10MB 上限"}
                        )
                    f.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        except OSError as e:
            logger.error("[extensions] 上传落盘失败 %s: %s", dest, e)
            raise HTTPException(status_code=500, detail={"code": "write_failed", "message": str(e)}) from e
        client = _extensions_rag()
        try:
            result = await client.upload_file(kb_id, str(dest), file_name=filename)
        except Exception as e:
            logger.error("[extensions] fusion-rag 摄取失败 %s/%s: %s", kb_id, filename, e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        result = result if isinstance(result, dict) else {}
        logger.info("[extensions] KB %s 摄取 %s (%d bytes): %s", kb_id, filename, size, result.get("chunks", "?"))
        return {"chunks": result.get("chunks"), **{k: v for k, v in result.items() if k != "chunks"}}

    @app.post("/api/extensions/rag/kb/{kb_id}/search")
    async def extensions_rag_kb_search(kb_id: str, request: Request):
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        query = str(body.get("query") or "").strip() if isinstance(body, dict) else ""
        if not query:
            raise HTTPException(status_code=400, detail={"code": "invalid_query", "message": "query 必填"})
        try:
            top_k = int(body.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(top_k, 20))
        client = _extensions_rag()
        try:
            hits = await client.search(kb_id, query, top_k=top_k)
        except Exception as e:
            logger.warning("[extensions] fusion-rag 检索失败 %s: %s", kb_id, e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        hits = hits if isinstance(hits, list) else []
        return [
            {"text": h.get("text", ""), "doc_path": h.get("doc_path", ""), "score": h.get("score")}
            for h in hits
            if isinstance(h, dict)
        ]

    # ── issue #153: 托管浏览器 — 启动 / 关闭 / 状态 / 实时截图侧栏 ──
    @app.post("/api/browser/launch")
    async def browser_launch(request: Request):
        from ..managed_browser import ManagedBrowserError, get_managed_browser

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid_body", "message": "body 必须是对象"})
        try:
            info = await get_managed_browser().launch(
                url=str(body.get("url") or "about:blank"),
                port=int(body.get("port") or 0),
                headless=bool(body.get("headless")),
                import_cookies=str(body.get("import_cookies") or ""),
            )
        except ManagedBrowserError as e:
            logger.error(f"托管浏览器启动失败: {e}")
            raise HTTPException(status_code=503, detail={"code": "browser_unavailable", "message": str(e)}) from e
        except Exception as e:
            logger.error(f"托管浏览器启动异常: {e}")
            raise HTTPException(status_code=500, detail={"code": "browser_error", "message": str(e)}) from e
        return info

    @app.post("/api/browser/close")
    async def browser_close():
        from ..managed_browser import get_managed_browser

        closed = await get_managed_browser().close()
        return {"closed": closed}

    @app.get("/api/browser/status")
    async def browser_status():
        from ..managed_browser import get_managed_browser

        return get_managed_browser().status()

    @app.get("/api/browser/screenshot")
    async def browser_screenshot():
        from ..managed_browser import get_managed_browser
        from ..nodes.browser.cdp_client import CDPClient

        info = get_managed_browser().status()
        if not info.get("running"):
            raise HTTPException(status_code=409, detail={"code": "not_running", "message": "托管浏览器未运行"})
        client = CDPClient(port=int(info["port"]))
        try:
            await client.connect()
            data = await client.screenshot()
        except Exception as e:
            logger.warning(f"托管浏览器截图失败: {e}")
            raise HTTPException(status_code=502, detail={"code": "screenshot_failed", "message": str(e)}) from e
        finally:
            await client.disconnect()
        return Response(content=data, media_type="image/png")

    # ── issue #162: fusion-browser 引擎 — 每运行 profile / 登录态导入 / 面板截图流 ──
    def _fusion_browser():
        from ..fusion_browser import get_fusion_browser

        return get_fusion_browser()

    async def _fusion_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid_body", "message": "body 必须是对象"})
        return body

    @app.post("/api/fusion-browser/launch")
    async def fusion_browser_launch(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        browser = _fusion_browser()
        origins = body.get("allowed_origins")
        if isinstance(origins, list):
            browser.allowed_origins = [str(o) for o in origins if str(o).strip()]
        try:
            return await asyncio.to_thread(browser.launch, str(body.get("url") or "") or None)
        except FusionBrowserError as e:
            logger.error(f"fusion-browser 启动失败: {e}")
            raise HTTPException(status_code=503, detail={"code": "browser_unavailable", "message": str(e)}) from e

    @app.get("/api/fusion-browser/status")
    async def fusion_browser_status():
        return _fusion_browser().status()

    @app.post("/api/fusion-browser/session")
    async def fusion_browser_session(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        browser = _fusion_browser()
        try:
            return await asyncio.to_thread(
                browser.create_session,
                run_id=str(body.get("run_id") or "") or None,
                profile_id=str(body.get("profile_id") or "") or None,
                initial_url=str(body.get("initial_url") or body.get("url") or "") or None,
                mode=str(body.get("mode") or "") or None,
            )
        except FusionBrowserError as e:
            status = 409 if "未运行" in str(e) else 502
            raise HTTPException(status_code=status, detail={"code": "session_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/import-cookies")
    async def fusion_browser_import_cookies(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        session_id = str(body.get("session_id") or "")
        if not session_id:
            raise HTTPException(status_code=400, detail={"code": "missing_session", "message": "session_id 必填"})
        try:
            return await asyncio.to_thread(
                _fusion_browser().import_cookies,
                session_id,
                str(body.get("browser") or "firefox"),
                str(body.get("profile") or "") or None,
            )
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "import_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/execute")
    async def fusion_browser_execute(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        session_id = str(body.get("session_id") or "")
        action = str(body.get("action") or "")
        if not session_id or not action:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_params", "message": "session_id 与 action 必填"},
            )
        try:
            return await asyncio.to_thread(
                _fusion_browser().execute,
                session_id,
                action,
                str(body.get("target_node_id") or "") or None,
                str(body.get("payload_text") or "") or None,
            )
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "execute_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/finish")
    async def fusion_browser_finish(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        try:
            return await asyncio.to_thread(
                _fusion_browser().finish,
                str(body.get("run_id") or "") or None,
                not bool(body.get("keep_profile")),
            )
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "finish_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/close")
    async def fusion_browser_close():
        return await asyncio.to_thread(_fusion_browser().close)

    @app.get("/api/fusion-browser/screenshot")
    async def fusion_browser_screenshot(session_id: str = ""):
        from ..fusion_browser import FusionBrowserError

        if not session_id:
            raise HTTPException(status_code=400, detail={"code": "missing_session", "message": "session_id 必填"})
        try:
            data = await asyncio.to_thread(_fusion_browser().screenshot_png, session_id)
        except FusionBrowserError as e:
            status = 409 if "未运行" in str(e) else 502
            raise HTTPException(status_code=status, detail={"code": "screenshot_failed", "message": str(e)}) from e
        return Response(content=data, media_type="image/png")

    @app.get("/api/fusion-browser/stream")
    async def fusion_browser_stream(request: Request, session_id: str = ""):
        from ..fusion_browser import FusionBrowserError

        if not session_id:
            raise HTTPException(status_code=400, detail={"code": "missing_session", "message": "session_id 必填"})
        browser = _fusion_browser()
        if not browser.socket_path:
            raise HTTPException(status_code=409, detail={"code": "not_running", "message": "fusion-browser 未运行"})
        try:
            frames = browser.stream_frames(session_id)
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "stream_failed", "message": str(e)}) from e
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)

        def _pump() -> None:
            try:
                for frame in frames:
                    while True:
                        try:
                            loop.call_soon_threadsafe(queue.put_nowait, frame)
                            break
                        except asyncio.QueueFull:
                            time.sleep(0.05)
            except Exception as e:
                logger.warning(f"fusion-browser 面板流中断: {e}")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        pump = asyncio.create_task(asyncio.to_thread(_pump))

        async def _events():
            try:
                while True:
                    frame = await queue.get()
                    if frame is None:
                        yield "event: stream_end\ndata: {}\n\n"
                        return
                    payload = {
                        "seq": frame.get("seq"),
                        "url": frame.get("url", ""),
                        "title": frame.get("title", ""),
                        "ts_ms": frame.get("ts_ms"),
                        "png": frame.get("png", ""),
                    }
                    yield f"event: screenshot_frame\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if await request.is_disconnected():
                        return
            finally:
                pump.cancel()

        return StreamingResponse(_events(), media_type="text/event-stream")

    # ── issue #139: manifest ext 后端 — ⌘K 跨模块搜索 + 通知中心轮询源 ──
    @app.get("/api/search")
    async def search_all(q: str = ""):
        query = (q or "").strip().lower()
        if not query:
            return {"items": []}
        items: List[Dict[str, Any]] = []
        for wf in store.list():
            name = str(wf.get("name") or wf.get("id") or "")
            desc = str(wf.get("description") or "")
            if query in name.lower() or query in desc.lower():
                items.append({"title": name, "route": f"/canvas/{wf['id']}", "kind": "workflow"})
        for r in runs.list():
            name = str(r.get("workflow_name") or r.get("run_id") or "")
            if query in name.lower():
                items.append({"title": f"运行: {name}", "route": "/runs", "kind": "run"})
        if space_store is not None:
            try:
                spaces = await space_store.list_spaces(limit=100)
            except Exception as e:
                logger.warning(f"搜索空间失败 (跳过): {e}")
                spaces = []
            for s in spaces:
                name = str(getattr(s, "name", "") or getattr(s, "id", ""))
                if query in name.lower():
                    items.append({"title": name, "route": "/spaces", "kind": "space"})
        items = items[:32]
        logger.info(f"web API 搜索 q={q!r} 命中 {len(items)} 条")
        return {"items": items}

    @app.get("/api/notifications/approvals")
    async def notifications_approvals():
        if permission_manager is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "权限管理器不可用"})
        list_merged = getattr(permission_manager, "list_pending", None)
        approvals = list_merged() if callable(list_merged) else permission_manager.list_pending_guard()
        items = [
            {
                "id": a.get("action_id", ""),
                "title": f"审批: {a.get('tool_name', '')}",
                "route": "/approvals",
                "level": "warning",
            }
            for a in approvals
        ][:32]
        logger.info(f"web API 通知源 approvals 轮询: {len(items)} 条待审批")
        return {"items": items}

    @app.get("/api/notifications/runs")
    async def notifications_runs():
        terminal = {"completed", "failed", "canceled"}
        rows = [r for r in runs.list() if r.get("status") in terminal]
        rows.sort(key=lambda x: x.get("finished_at") or x.get("created_at") or 0, reverse=True)
        items = [
            {
                "id": r.get("run_id", ""),
                "title": f"运行结束: {r.get('workflow_name') or r.get('run_id')} ({r.get('status')})",
                "route": "/runs",
                "level": "error" if r.get("status") == "failed" else "info",
            }
            for r in rows[:32]
        ]
        logger.info(f"web API 通知源 runs 轮询: {len(items)} 条终态运行")
        return {"items": items}

    # ── issue #138 G10: 空间知识层 — space 级 instructions (system-prompt 注入) ──
    @app.get("/api/space/spaces/{space_id}/instructions")
    async def instructions_get(space_id: str):
        if space_store is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "空间存储不可用"})
        sp = await space_store.get_space(space_id)
        if not sp:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "空间不存在"})
        return {"space_id": space_id, "instructions": getattr(sp.config, "instructions", "") or ""}

    @app.put("/api/space/spaces/{space_id}/instructions")
    async def instructions_put(space_id: str, body: Dict[str, Any]):
        if space_store is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "空间存储不可用"})
        sp = await space_store.get_space(space_id)
        if not sp:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "空间不存在"})
        existing = sp.config.to_dict()
        existing["instructions"] = str(body.get("instructions") or "")
        await space_store.update_space(space_id, config=existing)
        logger.info(f"web API space instructions 已更新: {space_id} ({len(existing['instructions'])} 字符)")
        return {"space_id": space_id, "instructions": existing["instructions"]}

    # ── W19 guard L3 审批队列 (migration doc §6 P3; #124 起 guard+本地 CONFIRM 合并) ──
    @app.get("/api/permissions/pending")
    async def permissions_pending():
        if permission_manager is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "权限管理器不可用"})
        list_merged = getattr(permission_manager, "list_pending", None)
        approvals = list_merged() if callable(list_merged) else permission_manager.list_pending_guard()
        return {"approvals": approvals}

    @app.post("/api/permissions/pending/{action_id}/confirm")
    async def permissions_confirm(action_id: str, body: Dict[str, Any]):
        if permission_manager is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "权限管理器不可用"})
        approved = bool(body.get("approved"))
        # issue #142: 逐块审阅 — accepted_blocks 指定接受的变更块, 重建窄化后的编辑参数
        narrowed = None
        if approved and isinstance(body.get("accepted_blocks"), list):
            narrowed = _narrow_pending_edit(permission_manager, action_id, body["accepted_blocks"])
            if narrowed is _EDIT_ALL_REJECTED:
                logger.info(f"逐块审阅: {action_id} 全部变更块被拒绝, 不落批准规则")
                return {
                    "action_id": action_id,
                    "approved": True,
                    "confirmed": False,
                    "reason": "所有变更块均被拒绝, 无可批准内容",
                }
        # issue #181: confirm 前读取待审批详情 — 本地待审批携带 execution_id,
        # confirm 落规则后唤醒 HITL 暂停中的 run 原地续跑 (guard 侧无 execution_id, 不受影响)
        hitl_exec = None
        details_getter = getattr(permission_manager, "get_pending_details", None)
        if callable(details_getter):
            hitl_exec = (details_getter(action_id) or {}).get("execution_id") or None
        # issue #187: "Approve once" — 只出队该待审批项, 不落持久规则, 直接唤醒暂停中的 run
        if body.get("once") and hitl_exec:
            releaser = getattr(permission_manager, "release_pending_once", None)
            released = releaser(action_id) if callable(releaser) else False
            if released and approved:
                resolver = getattr(engine, "resolve_hitl", None)
                if callable(resolver):
                    try:
                        resolver(hitl_exec, True, once=True)
                    except TypeError:
                        # 兼容无 once 参数的旧引擎 — 规则未落, 复查会拒绝但 run 被唤醒
                        resolver(hitl_exec, True)
                logger.info(f"HITL 一次性批准已唤醒 run: {hitl_exec} (action_id={action_id}, 不落规则)")
                return {"action_id": action_id, "approved": True, "confirmed": True, "once": True}
        confirmed = await permission_manager.confirm_guard(
            action_id, approved, approved_by="web", scope=str(body.get("scope") or "*")
        )
        if confirmed and hitl_exec:
            resolver = getattr(engine, "resolve_hitl", None)
            if callable(resolver):
                resolver(hitl_exec, approved)
                logger.info(f"HITL confirm 已唤醒 run: {hitl_exec} approved={approved}")
        # issue #138: 确认落规则后立即持久化 (重启不丢)
        try:
            saver = getattr(permission_manager, "save", None)
            if confirmed and callable(saver):
                saver()
        except Exception as e:
            logger.warning(f"web API 权限规则保存失败: {e}")
        resp: Dict[str, Any] = {"action_id": action_id, "approved": approved, "confirmed": confirmed}
        if narrowed is not None:
            resp["edited_params"] = narrowed
        return resp

    # ── W21 集群面板 (migration doc §6 P3, FUSION_CLUSTER_ENABLED opt-in) ──
    @app.get("/api/cluster/state")
    async def cluster_state():
        from ..distributed_state import get_cluster_state_store

        cluster_store = get_cluster_state_store()
        if cluster_store is None:
            return {
                "cluster_enabled": False,
                "nodes": [],
                "vram": {},
                "total_allocated_mb": 0,
            }
        nodes = [n.to_dict() for n in cluster_store.list_all_nodes()]
        return {
            "cluster_enabled": True,
            "nodes": nodes,
            "vram": cluster_store.cluster_vram_usage(),
            "total_allocated_mb": cluster_store.total_vram_allocated_mb(),
        }

    # ── W18 用量/配额指示 — 空间域计数 + QuotaEnforcer 配额 (migration doc §6 P3) ──
    @app.get("/api/usage")
    async def usage_overview():
        quotas_usage = {
            "spaces": 0,
            "messages": 0,
            "artifacts": 0,
            "agents": 0,
        }
        tenant_id = "default"
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            from ..tenant import get_current_tenant

            tenant_id = get_current_tenant() or "default"
            try:
                row = await space_store._fetchone("SELECT COUNT(*) AS c FROM spaces WHERE tenant_id = ?", (tenant_id,))
                quotas_usage["spaces"] = int(row["c"] or 0)
                row = await space_store._fetchone(
                    "SELECT COUNT(*) AS c FROM space_messages WHERE tenant_id = ?", (tenant_id,)
                )
                quotas_usage["messages"] = int(row["c"] or 0)
                row = await space_store._fetchone(
                    "SELECT COUNT(*) AS c FROM space_artifacts WHERE tenant_id = ?", (tenant_id,)
                )
                quotas_usage["artifacts"] = int(row["c"] or 0)
                row = await space_store._fetchone(
                    "SELECT COUNT(*) AS c FROM space_agents WHERE tenant_id = ?", (tenant_id,)
                )
                quotas_usage["agents"] = int(row["c"] or 0)
            except Exception as e:
                logger.warning(f"用量计数失败 (返回零值): {e}")
        from ..security.quotas import get_default_quota_enforcer

        try:
            quotas = get_default_quota_enforcer()._load_quotas(tenant_id)
            quota_fields = {
                "max_spaces": quotas.max_spaces,
                "max_messages_per_space": quotas.max_messages_per_space,
                "max_artifacts_per_space": quotas.max_artifacts_per_space,
                "max_agents_per_space": quotas.max_agents_per_space,
                "max_storage_mb": quotas.max_storage_mb,
            }
        except Exception as e:
            logger.warning(f"配额读取失败 (返回 unlimited): {e}")
            quota_fields = {
                "max_spaces": -1,
                "max_messages_per_space": -1,
                "max_artifacts_per_space": -1,
                "max_agents_per_space": -1,
                "max_storage_mb": -1,
            }
        return {
            "tenant_id": tenant_id,
            "usage": quotas_usage,
            "quotas": quota_fields,
            "unlimited": all(v < 0 for v in quota_fields.values()),
        }

    # ── W22 移动只读概览 + 投递到桌面 (remote/sync 底座, cross_sync 注入接缝) ──
    @app.get("/api/mobile/overview")
    async def mobile_overview():
        recent = runs.list()[:5]
        return {
            "runs": [
                {
                    "run_id": r["run_id"],
                    "workflow_name": r["workflow_name"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "finished_at": r.get("finished_at"),
                    "error": r.get("error"),
                }
                for r in recent
            ],
            "pending_approvals": getattr(permission_manager, "list_pending", permission_manager.list_pending_guard)(),
            "generated_at": time.time(),
        }

    @app.post("/api/deliver")
    async def deliver_to_desktop(request: Request):
        cross = getattr(app.state, "cross_sync", None)
        if cross is None:
            return JSONResponse({"error": "跨设备同步未启用 (cross_sync 未注入)"}, status_code=503)
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"/api/deliver JSON 解析失败: {e}")
            return JSONResponse({"error": "invalid json"}, status_code=400)
        run_id = str(body.get("run_id") or "")
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"})
        receiver = str(body.get("target_device_id") or "broadcast")
        from .sync import SyncMessage

        msg = SyncMessage(
            msg_id=f"del_{uuid.uuid4().hex[:8]}",
            msg_type="cowork_deliver",
            sender="fusion-cowork-web",
            receiver=receiver,
            payload={
                "run_id": run_id,
                "workflow_name": record.get("workflow_name", ""),
                "status": record.get("status", ""),
                "deep_link": f"fusion-cowork://runs/{run_id}",
            },
        )
        try:
            delivered = await cross.send_message(msg)
        except Exception as e:
            logger.warning(f"/api/deliver 发送失败 (诚实返回未送达): {e}")
            delivered = False
        return {"delivered": delivered, "run_id": run_id, "receiver": receiver}

    # ── issue #156: 管理台 — 成员 / 用量 / 审计导出。FUSION_COWORK_ADMIN_KEY 门禁 ──
    def _require_admin(request: Request) -> None:
        import os as _os

        expected = _os.environ.get("FUSION_COWORK_ADMIN_KEY", "")
        if not expected:
            raise HTTPException(
                status_code=503,
                detail={"code": "admin_disabled", "message": "管理台未启用 (未配置 FUSION_COWORK_ADMIN_KEY)"},
            )
        provided = request.headers.get("x-admin-key", "")
        if provided != expected:
            logger.warning(f"admin API 鉴权拒绝: {request.url.path}")
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "invalid admin key"})

    @app.get("/api/admin/members")
    async def admin_members(request: Request):
        _require_admin(request)
        members: List[Dict[str, Any]] = []
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            try:
                rows = await space_store._fetchall(
                    "SELECT space_id, user_id, role, display_name, joined_at, tenant_id FROM space_members ORDER BY tenant_id, space_id"
                )
                members = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"admin members 查询失败: {e}")
        from ..auth import is_identity_enabled

        return {
            "mode": "identity" if is_identity_enabled() else "local",
            "members": members,
            "count": len(members),
        }

    @app.get("/api/admin/usage")
    async def admin_usage(request: Request):
        _require_admin(request)
        totals = {"spaces": 0, "messages": 0, "artifacts": 0, "agents": 0}
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            for key, table in (
                ("spaces", "spaces"),
                ("messages", "space_messages"),
                ("artifacts", "space_artifacts"),
                ("agents", "space_agents"),
            ):
                try:
                    row = await space_store._fetchone(f"SELECT COUNT(*) AS c FROM {table}")
                    totals[key] = int(row["c"] or 0)
                except Exception as e:
                    logger.warning(f"admin usage 计数失败 {table}: {e}")
        run_counts: Dict[str, int] = {}
        total_tokens = 0
        for record in runs._runs.values():
            status = str(record.get("status", ""))
            run_counts[status] = run_counts.get(status, 0) + 1
            for step in record.get("steps") or []:
                tok = step.get("tokens") or {}
                if isinstance(tok, dict):
                    total_tokens += int(tok.get("total_tokens") or 0)
        return {"spaces": totals, "runs": {"by_status": run_counts, "total": len(runs._runs)}, "tokens": total_tokens}

    @app.get("/api/admin/audit/export")
    async def admin_audit_export(request: Request, since: float = 0.0, limit: int = 500):
        _require_admin(request)
        lim = min(max(limit, 1), 2000)
        records: List[Dict[str, Any]] = []
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            try:
                rows = await space_store._fetchall(
                    "SELECT tenant_id, actor, action, resource, detail_hash, entry_hash, created_at "
                    "FROM audit_log ORDER BY created_at ASC, id ASC LIMIT ?",
                    (lim,),
                )
                for row in rows:
                    ts = _iso_to_epoch(str(row["created_at"] or ""))
                    if since and ts < since:
                        continue
                    records.append(
                        {
                            "time_unix_nano": ts * 1e9,
                            "severity": "INFO",
                            "body": f"audit {row['action']} {row['resource']}",
                            "attributes": {
                                "event.name": "audit",
                                "tenant_id": row["tenant_id"],
                                "actor": row["actor"],
                                "action": row["action"],
                                "resource": row["resource"],
                                "detail_hash": row["detail_hash"],
                                "entry_hash": row["entry_hash"],
                            },
                        }
                    )
            except Exception as e:
                logger.warning(f"admin audit 查询失败: {e}")
        for record in runs._runs.values():
            finished = record.get("finished_at")
            if not finished:
                continue
            if since and float(finished) < since:
                continue
            records.append(
                {
                    "time_unix_nano": float(finished) * 1e9,
                    "severity": "ERROR" if record.get("status") == "failed" else "INFO",
                    "body": f"run.finished {record.get('workflow_name', '')}",
                    "attributes": {
                        "event.name": "run.finished",
                        "run_id": record.get("run_id", ""),
                        "workflow_id": record.get("workflow_id", ""),
                        "status": record.get("status", ""),
                        "steps": len(record.get("steps") or []),
                        "error": record.get("error") or "",
                    },
                }
            )
        return _otlp_logs(records[:lim])

    @app.post("/api/admin/push/test")
    async def admin_push_test(request: Request):
        _require_admin(request)
        egress = getattr(app.state, "push_egress", None)
        if egress is None or not getattr(egress, "enabled", False):
            return {"enabled": False, "delivered": False, "hint": "配置 FUSION_COWORK_PUSH_WEBHOOK_URL 后可用"}
        delivered = await egress.push_event("test", {"source": "admin_console"})
        return {"enabled": True, "delivered": delivered}

    # ── issue #154: 会话接管 WS — 任意已认证设备 attach 运行中的 run, 事件流 + 转向帧 ──
    @app.websocket("/ws/takeover/{run_id}")
    async def takeover_ws(websocket: WebSocket, run_id: str):
        from fastapi import WebSocketDisconnect

        # 浏览器 WS 无法带自定义头 — token 走 query 参数; 未配 key = 放行 (#146 契约)
        from ..auth import verify_any_token

        if auth_configured():
            token = str(websocket.query_params.get("token", ""))
            if not any(verify_any_token(token, expected) is not None for expected in _accepted_keys()):
                await websocket.accept()
                await websocket.close(code=4401)
                return
        await websocket.accept()
        record = runs.get(run_id)
        if record is None:
            await websocket.send_json({"event": "error", "message": f"运行不存在: {run_id}"})
            await websocket.close(code=4404)
            return
        try:
            await _takeover_session(websocket, run_id, runs, permission_manager)
        except WebSocketDisconnect:
            logger.info(f"takeover WS 断开: run={run_id}")
        return

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
    # ── W11/A12 统一会话列表: engine session + space chat + 调度历史 ──
    @app.get("/api/sessions")
    async def sessions_list(status: str = "", source: str = "", limit: int = 50):
        if source not in ("", "engine", "space", "scheduler"):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "source 必须是 engine/space/scheduler"},
            )
        lim = min(max(limit, 1), 200)
        items: List[Dict[str, Any]] = []
        if source in ("", "engine"):
            for s in session_store.list_sessions(status=status or None, limit=lim):
                item = _serialize_session(s)
                item.update(
                    {
                        "source": "engine",
                        "title": s.workflow_name or s.id,
                        "space_id": "",
                        "space_name": "",
                        "href": "#/runs",
                    }
                )
                items.append(item)
        if source in ("", "space"):
            space_store = getattr(app.state, "space_store", None)
            if space_store is not None:
                from ..tenant import resolve_tenant_id

                tid = resolve_tenant_id(None)
                try:
                    for sp in await space_store.list_spaces(limit=lim):
                        last = await space_store._fetchone(
                            "SELECT content, role, created_at FROM space_messages "
                            "WHERE space_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT 1",
                            (sp.id, tid),
                        )
                        last_ts = _iso_to_epoch(last["created_at"]) if last else 0.0
                        created = _iso_to_epoch(sp.created_at)
                        item = {
                            "id": f"space_{sp.id}",
                            "workflow_id": "",
                            "workflow_name": sp.name,
                            "status": sp.status,
                            "initial_input": {},
                            "execution_id": "",
                            "created_at": created,
                            "updated_at": last_ts or _iso_to_epoch(sp.updated_at) or created,
                            "completed_at": None,
                            "metadata": {
                                "space_id": sp.id,
                                "last_message": (last["content"][:200] if last else ""),
                                "last_role": (last["role"] if last else ""),
                            },
                            "tenant_id": sp.tenant_id,
                            "source": "space",
                            "title": sp.name,
                            "space_id": sp.id,
                            "space_name": sp.name,
                            "href": "#/spaces",
                        }
                        items.append(item)
                except Exception as e:
                    logger.warning(f"space 会话合并失败 (跳过): {e}")
        if source in ("", "scheduler") and scheduler is not None:
            try:
                for task in scheduler.list_tasks():
                    item = {
                        "id": f"sched_{task.id}",
                        "workflow_id": task.workflow_id,
                        "workflow_name": task.name,
                        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                        "initial_input": {},
                        "execution_id": "",
                        "created_at": task.created_at,
                        "updated_at": task.last_run or task.created_at,
                        "completed_at": None,
                        "metadata": {
                            "task_id": task.id,
                            "trigger_type": task.trigger_type,
                            "run_count": task.run_count,
                            "fail_count": task.fail_count,
                        },
                        "tenant_id": getattr(task, "tenant_id", "default"),
                        "source": "scheduler",
                        "title": task.name,
                        "space_id": "",
                        "space_name": "",
                        "href": "#/schedule",
                    }
                    items.append(item)
            except Exception as e:
                logger.warning(f"调度会话合并失败 (跳过): {e}")
        if status:
            items = [i for i in items if i.get("status") == status]
        items.sort(key=lambda i: i.get("updated_at") or i.get("created_at") or 0, reverse=True)
        items = items[:lim]
        return {"sessions": items, "count": len(items)}

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
