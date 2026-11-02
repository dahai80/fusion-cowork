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
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def _team_poll_interval() -> float:
    # issue #190: 无推送能力对端的兜底轮询间隔 (秒)
    raw = os.environ.get("FUSION_INBOX_POLL_INTERVAL", "5")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(f"FUSION_INBOX_POLL_INTERVAL 非法: {raw!r}, 回落 5s")
        return 5.0
    return value if value > 0 else 5.0


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


ARTIFACT_REF_PATTERN = re.compile(r"\[Artifact:\s*(?P<name>[^|\]]+?)\s*\|\s*ID:\s*(?P<id>[A-Za-z0-9_\-]+)[^\]]*\]")

_ARTIFACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def find_artifact_refs(text: Any) -> List[Dict[str, str]]:
    # issue #233: 从文本按序提取 artifact 轻量引用 (保序去重); payload 保持 ~30 token ref 不膨胀
    if not isinstance(text, str) or not text:
        return []
    seen: set = set()
    refs: List[Dict[str, str]] = []
    for m in ARTIFACT_REF_PATTERN.finditer(text):
        ref_id = m.group("id")
        if ref_id in seen:
            continue
        seen.add(ref_id)
        refs.append({"artifact_id": ref_id, "name": m.group("name").strip()})
    return refs


def _artifact_ref_text(card: Dict[str, Any]) -> str:
    # issue #233: 重建引擎轻量 ref_text (artifact.get 的 ~30 token 格式)
    return (
        f"[Artifact: {card.get('name', '')} | ID: {card.get('artifact_id', '')} | "
        f"Version: v{card.get('current_version') or 1} | Type: {card.get('type', '')} | "
        f"Summary: {card.get('summary', '')}]"
    )


async def _fetch_artifact_card_with_status(
    artifact_id: str, transport: Any = None
) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    # issue #237: 返回 (卡片, 上游 HTTP 状态)。状态让调用方区分「不存在/无权限」与
    # 「引擎不可达」; transport 异常/非法 id 返回 (None, None)。
    if not artifact_id or not _ARTIFACT_ID_PATTERN.match(artifact_id):
        return None, None
    base_url = os.environ.get("FUSION_ARTIFACTS_URL", "http://127.0.0.1:11451").rstrip("/")
    api_key = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
            resp = await client.get(f"{base_url}/api/v1/artifacts/{artifact_id}", headers=headers)
        if resp.status_code != 200:
            logger.warning("[artifacts] card fetch %s -> HTTP %s", artifact_id, resp.status_code)
            return None, resp.status_code
        data = resp.json()
    except Exception as exc:
        logger.warning("[artifacts] card fetch %s failed: %s", artifact_id, exc)
        return None, None
    payload: Any = data
    if isinstance(data, dict):
        # issue #243: 上游信封三形 — {data:{…}} (网关风格) / {artifact:{…}} (引擎实测)
        # / 平铺。首见 {artifact:{…}} 使卡片字段全空, 深链预填只剩空名。
        for key in ("data", "artifact"):
            wrapped = data.get(key)
            if isinstance(wrapped, dict):
                payload = wrapped
                break
    if not isinstance(payload, dict):
        return None, 200
    card_id = str(payload.get("id") or artifact_id)
    card = {
        "artifact_id": card_id,
        "name": str(payload.get("name") or ""),
        "type": str(payload.get("type") or ""),
        "kind": str(payload.get("kind") or ""),
        "current_version": payload.get("current_version") or 1,
        "summary": str(payload.get("summary") or ""),
        "source_module": str(payload.get("source_module") or ""),
        "is_starred": bool(payload.get("is_starred")),
        "deep_link": f"/shell/fusion-artifacts/#/a/{card_id}",
    }
    return card, 200


async def _fetch_artifact_card(artifact_id: str, transport: Any = None) -> Optional[Dict[str, Any]]:
    # issue #233 兼容入口: 时间线卡片只关心成功/失败, 状态由 #237 路由消费。
    card, _ = await _fetch_artifact_card_with_status(artifact_id, transport=transport)
    return card


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
        # issue #228: 能力用量入账回调 (未注入 = 不记账, 零行为变化)
        self.capability_recorder = None
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
            # issue #227: 用户修正标记步 (运行中指令注入的落点)
            "user_correction": bool(getattr(s, "user_correction", False)),
        }

    def list(self, space_id: str = "") -> List[Dict[str, Any]]:
        self._gc()
        # issue #223: space_id 过滤 — 首页 "最近任务 (本目录)" 按工作区收口
        items = sorted(self._runs.values(), key=lambda x: -x["created_at"])
        if space_id:
            items = [r for r in items if str(r.get("space_id") or "") == space_id]
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
                # issue #207: 来源/标签随列表透出 (缺省 None, 老调用方零变化)
                "source": r.get("source"),
                "labels": r.get("labels"),
                # issue #223: 归属工作区 (缺省 "" = 未绑定目录, 老数据零变化)
                "space_id": str(r.get("space_id") or ""),
            }
            for r in items
        ]

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._runs.get(run_id)

    def start(
        self,
        workflow_def: Dict[str, Any],
        workflow_id: str = "",
        workflow_name: str = "",
        memory: bool = True,
        message_input: Optional[Dict[str, Any]] = None,
        source: str = "",
        labels: Optional[Dict[str, str]] = None,
        space_id: str = "",
        resume_steps: Optional[List[Dict[str, Any]]] = None,
        ancestor_summary: Optional[List[Dict[str, Any]]] = None,
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
            # issue #207: 来源/标签 (调用方归因), 缺省空
            "source": str(source or ""),
            "labels": dict(labels or {}),
            # issue #223: run 归属工作区 (Space) — 首页按目录过滤/聚合
            "space_id": str(space_id or ""),
            # issue #249: 祖先 run 滚动摘要 (resume 链) — 摘要行进 run 记录, 整链步体不入上下文
            "ancestor_summary": list(ancestor_summary or []),
        }
        self._runs[run_id] = record
        self._persist(run_id)
        task = asyncio.create_task(self._execute(run_id, wf, message_input=message_input, resume_steps=resume_steps))
        self._tasks[run_id] = task
        logger.info(
            f"工作流运行已启动: {run_id} ({record['workflow_name']}) memory={record['memory']} "
            f"source={record['source'] or '-'} labels={len(record['labels'])}"
        )
        return {"run_id": run_id, "status": "running"}

    async def _execute(
        self,
        run_id: str,
        wf: Any,
        start_paused: bool = False,
        resume_steps: Optional[List[Dict[str, Any]]] = None,
        message_input: Optional[Dict[str, Any]] = None,
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
            # issue #190: 邮箱派发的消息体作为首节点输入 (记忆召回据此让位)
            if message_input:
                initial_input = dict(message_input)
                logger.info(f"[{run_id}] 消息输入已注入首节点 ({len(initial_input)} 字段)")
            # issue #191: 恢复续跑不再重复召回 — 首轮已注入过, 再注入会覆盖已完成首节点的快照输出
            if bridge is not None and not resume_steps:
                query = f"{wf.name} {getattr(wf, 'description', '') or ''}".strip()
                ctx = await bridge.retrieve_context(query)
                if ctx:
                    record["memory_context"] = ctx
                    base = dict(initial_input or {})
                    base["memory_context"] = ctx
                    initial_input = base
                    logger.info(f"[{run_id}] 记忆上下文已注入首节点输入 ({len(ctx)} chars)")
            # issue #191: 仅在恢复场景传新参 — 注入的旧版/测试引擎无这两个 kwarg 也不破
            engine_kwargs: Dict[str, Any] = {}
            if resume_steps is not None:
                engine_kwargs["resume_steps"] = resume_steps
            if start_paused:
                engine_kwargs["start_paused"] = True
            # issue #228: 运行工作区传给引擎 — 能力闸门按工作区能力池判定
            setter = getattr(self._engine, "set_run_workspace", None)
            if callable(setter):
                setter(run_id, str(record.get("space_id") or ""))
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
            # issue #228: 能力用量入账 (调用次数/最近使用/审计), 仅记录已装能力
            if self.capability_recorder is not None:
                try:
                    self.capability_recorder(record)
                except Exception as e:
                    logger.warning(f"能力用量入账失败 (忽略) {run_id}: {e}")
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
                            # issue #207: 归因字段随事件外发, webhook 接收方可按来源路由
                            "source": record_ref.get("source", ""),
                            "labels": dict(record_ref.get("labels") or {}),
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

    # issue #227: inject = 运行中指令注入 (下一节点边界并入目标节点输入, 时间线落修正标记)
    DIRECTIVE_ACTIONS = ("set_params", "insert_node", "skip_remaining", "inject")

    def resume_from(self, run_id: str, node_id: str) -> Dict[str, Any]:
        """issue #226: 从历史 run 的指定步骤重跑 — 新 run 链回原 run, 已完成步骤快照续跑。"""
        record = self._runs.get(run_id)
        if not record:
            raise KeyError(run_id)
        if record["status"] in ("running", "pending", "paused"):
            raise ValueError("not_terminal")
        wf_def = record.get("workflow_def") or {}
        if not wf_def:
            raise ValueError("no_workflow_def")
        node_ids = {n.get("id") for n in wf_def.get("nodes", [])}
        if node_id not in node_ids:
            raise ValueError("unknown_node")
        # 目标步骤在时间线中的位置 — 其之前已完成 (success + 断点续跑 skipped 副本) 的步骤作为续跑快照,
        # 失败/被拒步骤不含 → 下游从目标节点真正重跑 (重校验由 Workflow.from_dict 完成)
        steps = record.get("steps") or []
        idx = next(
            (i for i, s in enumerate(steps) if s.get("node_id") == node_id and not s.get("user_correction")), None
        )
        # issue #249: 快照带预算 — 近 N 步保全量, 更早步收摘要 + step:// 引用;
        # 祖先摘要滚动续接 (链深增长时上下文亚线性)
        from ..engine.run_context import build_resume_snapshot, compose_ancestor_summary

        prior_ancestor = list(record.get("ancestor_summary") or [])
        if idx is None:
            # 目标节点从未执行 → 全量重跑, 快照为空, 祖先摘要原样续接
            resume_steps: List[Dict[str, Any]] = []
            ancestor_new: List[Dict[str, Any]] = []
            stats: Dict[str, int] = {"eligible": 0, "snapshot_steps": 0, "estimated_tokens": 0}
        else:
            resume_steps, ancestor_new, stats = build_resume_snapshot(run_id, steps, idx)
        ancestor_summary, truncated = compose_ancestor_summary(prior_ancestor, ancestor_new)
        stats["ancestor_total"] = len(ancestor_summary)
        if truncated:
            stats["ancestor_truncated"] = truncated
        labels = dict(record.get("labels") or {})
        labels["resume_of"] = run_id
        result = self.start(
            wf_def,
            workflow_id=record.get("workflow_id", ""),
            workflow_name=record.get("workflow_name", ""),
            memory=bool(record.get("memory", True)),
            source="resume",
            labels=labels,
            space_id=record.get("space_id", ""),
            resume_steps=resume_steps,
            ancestor_summary=ancestor_summary,
        )
        new_id = result["run_id"]
        logger.info(
            f"resume-from: {run_id} → {new_id} node={node_id} snapshot={len(resume_steps)} 步 "
            f"(链回原 run) context={stats.get('estimated_tokens', 0)}tok "
            f"ancestor={stats.get('ancestor_total', 0)}"
        )
        return {
            "run_id": new_id,
            "resumed_from": node_id,
            "resume_of": run_id,
            "snapshot_steps": len(resume_steps),
            "context": stats,
        }

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
                # issue #223: 恢复保留工作区归属 (v1 行无此列 → 空串, 语义 = 未绑定)
                "space_id": str(row.get("space_id") or ""),
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


def _make_pending_push_callback(push_egress, runs: Any = None):
    """issue #154: 审批入队 → webhook 推送回调工厂 (best-effort, 不抛)。

    在事件循环内被调用 (engine 权限检查 await 链), 直接 create_task;
    无事件循环时跳过 — webhook 是 best-effort, 不为它开新循环。
    issue #207: 注入 RunRegistry 时按 execution_id 附 run 归因 (run_source/run_labels)。
    """

    def _cb(info: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("push 回调无事件 loop, 跳过 (best-effort)")
            return
        payload = dict(info)
        if runs is not None:
            try:
                record = runs.get(str(info.get("execution_id") or ""))
                if record:
                    payload["run_source"] = record.get("source", "")
                    payload["run_labels"] = dict(record.get("labels") or {})
            except Exception as e:
                logger.debug(f"审批事件 run 归因失败 (忽略): {e}")
        loop.create_task(push_egress.push_event("approval_requested", payload))

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
        if action not in RunRegistry.DIRECTIVE_ACTIONS:
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
    team_db: Optional[str] = None,
    team_roster: Any = None,
    team_mailbox: Any = None,
    runner_pool: Any = None,
    capabilities_store: Any = None,
    capabilities_path: Optional[Path] = None,
    connector_dir: Optional[Path] = None,
    studio_base_url: Optional[str] = None,
    studio_api_key: Optional[str] = None,
    capabilities_transport: Any = None,
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

    # ── issue #228: 能力市场 — 工作区能力池 + 授权闸门 + 用量入账 ──
    from ..capabilities import OAuthFlow, build_catalog, make_capability_gate, record_run_usage
    from ..capabilities.store import CapabilityStore

    capability_store = capabilities_store or CapabilityStore(capabilities_path)
    app.state.capability_store = capability_store
    _gate_kwargs = {
        "connector_dir": connector_dir,
        "rag_wired_path": rag_wired_path,
    }
    if getattr(permission_manager, "capability_gate", None) is None:
        permission_manager.capability_gate = make_capability_gate(capability_store, **_gate_kwargs)

    def _capability_recorder(record: Dict[str, Any]) -> None:
        record_run_usage(capability_store, record, **_gate_kwargs)

    runs.capability_recorder = _capability_recorder
    oauth_flow = OAuthFlow(transport=capabilities_transport)

    # issue #190: 团队消息消费栈 — 花名册 + durable 信箱 + 常驻消费池
    from ..orchestrator.runner_pool import RunnerPool
    from ..orchestrator.team_mailbox import CoworkRoster, TeamMailbox

    if team_roster is None:
        team_roster = CoworkRoster(team_db)
    if team_mailbox is None:
        team_mailbox = TeamMailbox(team_db)

    async def _team_dispatch(agent: Dict[str, Any], message: Dict[str, Any]) -> str:
        workflow_id = str(agent.get("workflow_id") or "")
        record = store.get(workflow_id)
        if not record:
            raise RuntimeError(f"工作流定义不存在: {workflow_id}")
        body = message.get("body") or ""
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = {"input": body}
        if not isinstance(payload, dict):
            payload = {"input": body}
        payload.setdefault("input", body)
        payload["sender"] = message.get("sender", "")
        payload["message_id"] = message.get("message_id", "")
        payload["team"] = message.get("team", "")
        started = runs.start(
            record.get("workflow") or {},
            workflow_id=workflow_id,
            workflow_name=record.get("name", workflow_id),
            memory=False,
            message_input=payload,
        )
        logger.info(f"团队派发: agent={agent.get('agent_id')} workflow={workflow_id} run={started['run_id']}")
        return started["run_id"]

    async def _team_get_run(run_id: str) -> Optional[Dict[str, Any]]:
        record = runs.get(run_id)
        if not record:
            return None
        return {
            "status": record.get("status"),
            "last_result": (record.get("result") or {}).get("summary"),
            "last_error": record.get("error"),
        }

    if runner_pool is None:
        runner_pool = RunnerPool(
            roster=team_roster,
            mailbox=team_mailbox,
            dispatch=_team_dispatch,
            get_run=_team_get_run,
            poll_interval=_team_poll_interval(),
        )
    app.state.team_roster = team_roster
    app.state.team_mailbox = team_mailbox
    app.state.runner_pool = runner_pool

    # issue #154: webhook push 出口 (未配 FUSION_COWORK_PUSH_WEBHOOK_URL = no-op)
    from .push import PushEgress

    push_egress = PushEgress.from_env()
    app.state.push_egress = push_egress
    runs.push = push_egress
    # issue #154: 审批入队 → webhook 推送 (permission manager 回调, best-effort)
    if permission_manager is not None:
        permission_manager.on_pending_created = _make_pending_push_callback(push_egress, runs)

    # issue #191: 启动即恢复 — SQLite 里的活跃 run (paused/崩溃残留 running) 重建为 paused, 等人工 resume/审批
    @app.on_event("startup")
    async def _recover_runs():
        recovered = await runs.recover_persisted()
        if recovered:
            logger.warning(f"启动恢复: 从持久层重建 {recovered} 个活跃 run (均以 paused 起跑, 等待人工 resume/审批)")

    @app.on_event("startup")
    async def _start_team_pool():
        # issue #190: 花名册有启用 agent 即拉起消费池; FUSION_COWORK_RUNNER_POOL=1 强制常驻
        try:
            enabled = len(app.state.team_roster.list(enabled_only=True))
        except Exception as e:
            logger.error(f"团队消费池启动失败 (花名册不可读): {e}")
            return
        forced = os.environ.get("FUSION_COWORK_RUNNER_POOL", "").strip().lower() in ("1", "true", "yes", "on")
        if enabled or forced:
            await app.state.runner_pool.start()
            logger.info(f"团队消费池已启动: {enabled} 个 consumer, 轮询 {app.state.runner_pool.poll_interval}s")
        else:
            logger.info("团队消费池未启动: 花名册无启用 agent (fusion-cowork team register 登记)")

    @app.on_event("shutdown")
    async def _stop_team_pool():
        await app.state.runner_pool.stop()

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
            # issue #209: per-run 输入 — 与邮箱派发同语义 (message_input 注入首节点),
            # inputs 优先, initial_input 兼容别名; 非 dict 一律 400 不再静默丢弃
            inputs_obj = body.get("inputs", body.get("initial_input"))
            if inputs_obj is not None and not isinstance(inputs_obj, dict):
                raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "inputs 须为 JSON 对象"})
            # issue #207: per-run 来源/标签 — 全部可选, 校验类型后落 run 记录
            source = body.get("source", "")
            if not isinstance(source, str):
                raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "source 须为字符串"})
            labels = body.get("labels")
            if labels is None:
                labels = {}
            if not isinstance(labels, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
            ):
                raise HTTPException(
                    status_code=400, detail={"code": "bad_request", "message": "labels 须为字符串到字符串的对象"}
                )
            # issue #223: run 归属工作区 — 首页在活动目录上下文里启动的 run 带目录 id
            space_id = body.get("space_id", "")
            if not isinstance(space_id, str):
                raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "space_id 须为字符串"})
            return runs.start(
                workflow_def,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                memory=memory,
                message_input=inputs_obj,
                source=source,
                labels=labels,
                space_id=space_id,
            )
        except ValueError as exc:
            # issue #135 F-2: 坏边形状/含环等工作流定义错误 → 400 可读信息, 不 500
            raise HTTPException(
                status_code=400, detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"}
            ) from exc

    @app.get("/api/runs")
    async def runs_list(space_id: str = ""):
        # issue #223: ?space_id= 工作区过滤 — 缺省返回全部 (老调用方零变化)
        items = runs.list(space_id=space_id)
        return {"runs": items, "count": len(items)}

    # ── issue #223: 首页目录选择态聚合 — Space 卡片 + 成员数 + 任务数 + 最近活动 ──
    @app.get("/api/workspaces")
    async def workspaces_list():
        if space_store is None:
            # space 栈未装配 (无 [web] data dir) — 诚实告知不可用, 前端态零收起
            return {"workspaces": [], "available": False}
        try:
            spaces = await space_store.list_spaces(limit=100)
        except Exception as e:
            logger.error(f"workspaces 聚合失败: {e}")
            raise HTTPException(
                status_code=502, detail={"code": "upstream_error", "message": f"工作区列表失败: {e}"}
            ) from e
        run_items = runs.list()
        out: List[Dict[str, Any]] = []
        for sp in spaces:
            d = sp.to_dict() if hasattr(sp, "to_dict") else dict(sp)
            sid = str(d.get("id") or "")
            try:
                member_count = await space_store.count_members(sid)
            except Exception as e:
                logger.warning(f"workspaces 成员计数失败 space={sid}: {e}")
                member_count = 0
            ws_runs = [r for r in run_items if str(r.get("space_id") or "") == sid]
            out.append(
                {
                    "id": sid,
                    "name": d.get("name", ""),
                    "description": d.get("description", ""),
                    "owner_id": d.get("owner_id", ""),
                    "status": d.get("status", ""),
                    "created_at": d.get("created_at"),
                    "updated_at": d.get("updated_at"),
                    "member_count": int(member_count),
                    "task_count": len(ws_runs),
                    "last_run_at": max((r.get("created_at") or 0 for r in ws_runs), default=None),
                }
            )
        return {"workspaces": out, "available": True}

    # ── issue #251: 系统工作目录枚举 — 新建目录名必须来自真实目录选择, 不收手输;
    # ?path= 相对浏览支持子目录层级 (弹窗逐级下钻), 越界路径 400 ──
    @app.get("/api/workspaces/system-dirs")
    async def workspaces_system_dirs(path: str = ""):
        root = Path(os.environ.get("FUSION_COWORK_WORKSPACE_ROOT") or Path.home()).expanduser()
        if not root.is_dir():
            # 根不存在 → 诚实告知不可用, 前端选择器收起并提示
            logger.warning(f"system-dirs 根目录不存在: {root}")
            return {"root": str(root), "root_name": root.name, "relative": "", "directories": [], "available": False}
        rel = (path or "").strip().strip("/")
        current = root
        if rel and rel != ".":
            resolved_root = root.resolve()
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail={"code": "bad_path", "message": f"路径越界: {path}"}
                ) from None
            if not candidate.is_dir():
                raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"目录不存在: {path}"})
            current = candidate
            rel = candidate.relative_to(resolved_root).as_posix()
        try:
            entries = sorted(
                (e for e in current.iterdir() if e.is_dir() and not e.name.startswith(".")),
                key=lambda e: e.name.lower(),
            )
        except OSError as e:
            logger.error(f"system-dirs 枚举失败 root={root} rel={rel}: {e}")
            raise HTTPException(
                status_code=502, detail={"code": "upstream_error", "message": f"目录枚举失败: {e}"}
            ) from e
        child_rel = (rel + "/") if rel else ""
        directories = [{"name": e.name, "path": str(e), "relative": child_rel + e.name} for e in entries[:200]]
        return {
            "root": str(root),
            "root_name": root.name,
            "relative": rel,
            "directories": directories,
            "available": True,
        }

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

    # ── issue #226: 终态 run 从指定步骤重跑 (新 run 链回原 run) ──
    @app.post("/api/runs/{run_id}/resume-from")
    async def runs_resume_from(run_id: str, body: Dict[str, Any]):
        node_id = str(body.get("node_id") or "").strip()
        if not node_id:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "node_id 必填"})
        try:
            return runs.resume_from(run_id, node_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": f"运行不存在: {run_id}"}
            ) from None
        except ValueError as exc:
            msg = str(exc)
            if msg == "not_terminal":
                raise HTTPException(
                    status_code=409, detail={"code": "not_terminal", "message": f"运行未到终态: {run_id}"}
                ) from exc
            if msg == "unknown_node":
                raise HTTPException(
                    status_code=400, detail={"code": "unknown_node", "message": f"步骤不存在于工作流: {node_id}"}
                ) from exc
            # no_workflow_def / from_dict 校验失败 → 400
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": f"无法重跑: {msg}"}) from exc

    # ── issue #233: artifact 卡片代理 (fusion-artifacts-engine v0.6.0 contract) ──
    @app.get("/api/artifacts/refs/{artifact_id}")
    async def artifacts_ref_card(artifact_id: str):
        # issue #237: 区分「id 非法 / artifact 删除或无权限 / 引擎不可达」三类失败,
        # 前端据此给出明确提示, 不再静默注入假 ref。
        card, upstream_status = await _fetch_artifact_card_with_status(artifact_id)
        if card is None:
            if not artifact_id or not _ARTIFACT_ID_PATTERN.match(artifact_id):
                raise HTTPException(
                    status_code=404, detail={"code": "bad_id", "message": f"非法 artifact id: {artifact_id}"}
                )
            if upstream_status in (401, 403, 404):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "artifact_not_found", "message": f"产物不存在或无权访问: {artifact_id}"},
                )
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_unavailable", "message": "fusion-artifacts-engine 不可用"},
            )
        card["ref_text"] = _artifact_ref_text(card)
        return {"artifact": card}

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
        # issue #218: TaskScheduler._run_task 以零参调用 executor (await executor()),
        # 旧签名 _exec(task_id) 每次触发都 TypeError, 连败 5 次后任务被置 FAILED。
        async def _exec() -> None:
            record = store.get(workflow_id)
            if not record:
                logger.error(f"定时任务执行器找不到工作流: {workflow_id}")
                return
            runs.start(
                record.get("workflow") or {}, workflow_id=workflow_id, workflow_name=record.get("name", workflow_id)
            )
            logger.info(f"定时任务已触发工作流 {workflow_id}")

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

    # ── issue #228: 能力市场 — Skills / Connectors / Sub-agents 统一生命周期 ──
    from ..capabilities import load_connector_config, normalize_workspace, search_items

    _CAP_KIND = {"skill": "skills", "connector": "connectors", "subagent": "subagents"}

    async def _cap_workspace(raw: Any) -> str:
        ws = normalize_workspace(raw)
        if ws == normalize_workspace("") or space_store is None:
            return ws
        try:
            space = await space_store.get_space(ws)
        except Exception as e:
            logger.warning(f"能力市场: 工作区校验失败 {ws}: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)}) from e
        if space is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"工作区不存在: {ws}"})
        return ws

    async def _cap_catalog(ws: str, types: Optional[List[str]] = None) -> Dict[str, Any]:
        return await build_catalog(
            ws,
            store=capability_store,
            skills_dir=skills_dir,
            connector_dir=connector_dir,
            rag_wired_path=rag_wired_path,
            studio_base_url=studio_base_url,
            studio_api_key=studio_api_key,
            transport=capabilities_transport,
            types=types,
        )

    async def _cap_item(ws: str, cap_type: str, cap_id: str) -> Optional[Dict[str, Any]]:
        catalog = await _cap_catalog(ws, [cap_type])
        return next((i for i in catalog[_CAP_KIND[cap_type]] if i["id"] == cap_id), None)

    def _cap_type_or_400(cap_type: str) -> str:
        if cap_type not in _CAP_KIND:
            raise HTTPException(status_code=400, detail={"code": "bad_type", "message": f"未知能力类型: {cap_type}"})
        return cap_type

    async def _cap_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"能力市场请求体解析失败: {e}")
            return {}
        return body if isinstance(body, dict) else {}

    @app.get("/api/capabilities")
    async def capabilities_list(workspace_id: str = "", q: str = "", type: str = ""):
        ws = await _cap_workspace(workspace_id)
        types = [type] if type in _CAP_KIND else None
        catalog = await _cap_catalog(ws, types)
        query = q.strip().lower()
        if query:
            for key in _CAP_KIND.values():
                catalog[key] = [
                    i
                    for i in catalog[key]
                    if query
                    in " ".join(
                        str(i.get(k) or "") for k in ("name", "title", "title_en", "description", "description_en")
                    ).lower()
                ]
            catalog["count"] = sum(len(catalog[k]) for k in _CAP_KIND.values())
        return catalog

    @app.post("/api/capabilities/{cap_type}/{cap_id}/install")
    async def capabilities_install(cap_type: str, cap_id: str, request: Request):
        _cap_type_or_400(cap_type)
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        item = await _cap_item(ws, cap_type, cap_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知能力: {cap_id}"})
        entry = capability_store.install(
            ws,
            cap_type,
            cap_id,
            name=str(item.get("title") or item.get("name") or cap_id),
            source=str(item.get("source") or ""),
            spawn_policy=item.get("spawn_policy") if cap_type == "subagent" else None,
            actor=str(body.get("actor") or ""),
        )
        logger.info(f"能力市场安装: ws={ws} {cap_id} source={item.get('source')}")
        return {"installed": True, "workspace_id": ws, "capability": entry.to_public()}

    @app.delete("/api/capabilities/{cap_type}/{cap_id}")
    async def capabilities_uninstall(cap_type: str, cap_id: str, workspace_id: str = ""):
        _cap_type_or_400(cap_type)
        ws = normalize_workspace(workspace_id)
        removed = capability_store.uninstall(ws, cap_type, cap_id)
        return {"uninstalled": removed, "workspace_id": ws, "id": cap_id}

    @app.patch("/api/capabilities/{cap_type}/{cap_id}/enabled")
    async def capabilities_enabled(cap_type: str, cap_id: str, request: Request):
        _cap_type_or_400(cap_type)
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        enabled = bool(body.get("enabled", True))
        entry = capability_store.set_enabled(ws, cap_type, cap_id, enabled, actor=str(body.get("actor") or ""))
        if entry is None:
            raise HTTPException(status_code=404, detail={"code": "not_installed", "message": f"未安装: {cap_id}"})
        return {"workspace_id": ws, "capability": entry.to_public()}

    @app.post("/api/capabilities/connectors/{cap_id}/scopes")
    async def capabilities_scopes(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        item = await _cap_item(ws, "connector", cap_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知连接器: {cap_id}"})
        known = {str(s.get("id")): bool(s.get("sensitive")) for s in item.get("scopes") or []}
        requested = [str(s) for s in body.get("scopes") or [] if str(s)]
        unknown = [s for s in requested if s not in known]
        if unknown:
            raise HTTPException(status_code=400, detail={"code": "unknown_scope", "message": f"未知 scope: {unknown}"})
        sensitive = [s for s in requested if known.get(s)]
        if sensitive and body.get("approve") is not True:
            raise HTTPException(
                status_code=409,
                detail={"code": "sensitive_scope_requires_approval", "message": f"敏感 scope 需显式批准: {sensitive}"},
            )
        entry = capability_store.grant_scopes(ws, cap_id, requested, actor=str(body.get("actor") or ""))
        if entry is None:
            raise HTTPException(status_code=404, detail={"code": "not_installed", "message": f"未安装: {cap_id}"})
        return {"workspace_id": ws, "capability": entry.to_public(), "sensitive_approved": sensitive}

    @app.post("/api/capabilities/connectors/{cap_id}/oauth/start")
    async def capabilities_oauth_start(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        item = await _cap_item(ws, "connector", cap_id)
        if item is None or (item.get("auth") or {}).get("kind") != "oauth":
            raise HTTPException(
                status_code=404, detail={"code": "oauth_not_configured", "message": f"无 OAuth 配置: {cap_id}"}
            )
        raw_id = str(item.get("name") or "")
        cfg = load_connector_config(connector_dir, raw_id) or {}
        oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else None
        if not oauth_cfg:
            raise HTTPException(
                status_code=404, detail={"code": "oauth_not_configured", "message": f"无 OAuth 配置: {cap_id}"}
            )
        try:
            started = oauth_flow.start(cap_id, oauth_cfg, workspace_id=ws)
        except Exception as e:
            logger.warning(f"OAuth 授权启动失败 {cap_id}: {e}")
            raise HTTPException(status_code=400, detail={"code": "oauth_not_configured", "message": str(e)}) from e
        return started

    @app.get("/api/capabilities/oauth/callback")
    async def capabilities_oauth_callback(code: str = "", state: str = "", error: str = ""):
        from ..security.encryption import encrypt_at_rest

        if error:
            logger.warning(f"OAuth 回调返回错误: {error}")
            return JSONResponse({"connected": False, "error": error}, status_code=400)
        pending = oauth_flow.pop_state(state)
        if pending is None:
            raise HTTPException(
                status_code=400, detail={"code": "oauth_state_invalid", "message": "state 未知或已过期"}
            )
        if not code:
            raise HTTPException(status_code=400, detail={"code": "oauth_code_missing", "message": "缺少 code"})
        cap_id = str(pending.get("connector_id") or "")
        ws = str(pending.get("workspace_id") or "")
        item = await _cap_item(ws, "connector", cap_id)
        raw_id = str((item or {}).get("name") or "")
        cfg = load_connector_config(connector_dir, raw_id) or {}
        oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
        try:
            tokens = await oauth_flow.exchange(pending, code, oauth_cfg)
        except Exception as e:
            logger.warning(f"OAuth token 交换失败 {cap_id}: {e}")
            raise HTTPException(status_code=502, detail={"code": "oauth_exchange_failed", "message": str(e)}) from e
        encrypted = {k: encrypt_at_rest(str(v)) for k, v in tokens.items()}
        capability_store.set_credentials(ws, cap_id, encrypted)
        scopes = [str(s) for s in pending.get("scopes") or []]
        if scopes:
            capability_store.grant_scopes(ws, cap_id, scopes)
        logger.info(f"OAuth 连接完成: ws={ws} {cap_id} scopes={scopes}")
        return {"connected": True, "workspace_id": ws, "connector_id": cap_id, "scopes": scopes}

    @app.post("/api/capabilities/connectors/{cap_id}/revoke")
    async def capabilities_oauth_revoke(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        revoked = capability_store.clear_credentials(ws, cap_id)
        return {"revoked": revoked, "workspace_id": ws, "id": cap_id}

    @app.get("/api/capabilities/{cap_type}/{cap_id}/usage")
    async def capabilities_usage(cap_type: str, cap_id: str, workspace_id: str = ""):
        _cap_type_or_400(cap_type)
        ws = normalize_workspace(workspace_id)
        usage = capability_store.usage(ws, cap_type, cap_id)
        if usage is None:
            raise HTTPException(status_code=404, detail={"code": "not_installed", "message": f"未安装: {cap_id}"})
        usage["workspace_id"] = ws
        return usage

    @app.get("/api/capabilities/subagents/{cap_id}/definition")
    async def capabilities_definition(cap_id: str, workspace_id: str = ""):
        ws = await _cap_workspace(workspace_id)
        item = await _cap_item(ws, "subagent", cap_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知子代理: {cap_id}"})
        kind = str((item.get("detail") or {}).get("kind") or "")
        if kind == "template":
            from ..templates import TemplateManager

            tpl = TemplateManager().get_template(str(item["detail"]["template_id"]))
            if tpl is None:
                raise HTTPException(status_code=404, detail={"code": "not_found", "message": "模板不存在"})
            return {"kind": "template", "definition": tpl}
        import httpx

        base = (studio_base_url or os.environ.get("FUSION_AGENT_STUDIO_URL", "http://127.0.0.1:11442")).rstrip("/")
        key = (studio_api_key or os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "")).strip()
        headers = {"x-api-key": key} if key else {}
        detail = item.get("detail") or {}
        path = (
            f"/v1/graphs/{detail.get('graph_id')}"
            if kind == "graph"
            else f"/v1/agents/{detail.get('agent_id')}/definition"
        )
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=headers, timeout=5.0, transport=capabilities_transport
            ) as client:
                resp = await client.get(path)
                resp.raise_for_status()
                return {"kind": kind, "definition": resp.json()}
        except Exception as e:
            logger.warning(f"子代理定义拉取失败 {cap_id}: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_unavailable", "message": str(e)}) from e

    @app.post("/api/capabilities/subagents/{cap_id}/spawn")
    async def capabilities_spawn(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        entry = capability_store.get(ws, "subagent", cap_id)
        if entry is None or not entry.enabled:
            raise HTTPException(status_code=409, detail={"code": "not_enabled", "message": f"子代理未启用: {cap_id}"})
        item = await _cap_item(ws, "subagent", cap_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"未知子代理: {cap_id}"})
        detail = item.get("detail") or {}
        policy = entry.spawn_policy or item.get("spawn_policy") or {}
        if policy.get("requires_approval") and body.get("approve") is not True:
            raise HTTPException(
                status_code=409,
                detail={"code": "spawn_approval_required", "message": f"子代理派生需显式批准: {cap_id}"},
            )
        text = str(body.get("input") or body.get("goal") or "")
        kind = str(detail.get("kind") or "")
        if kind == "template":
            from ..templates import TemplateManager

            wf = TemplateManager().template_to_workflow(str(detail.get("template_id") or ""))
            if wf is None:
                raise HTTPException(status_code=404, detail={"code": "not_found", "message": "模板不存在"})
            started = runs.start(
                wf.to_dict(),
                workflow_id=f"capability:{cap_id}",
                workflow_name=item.get("title") or wf.name,
                source="capability",
                labels={"capability": cap_id},
                space_id=ws,
            )
            logger.info(f"子代理派生 (template): ws={ws} {cap_id} run={started['run_id']}")
            return {"kind": "template", "run_id": started["run_id"], "route": f"/runs/{started['run_id']}"}
        import httpx

        base = (studio_base_url or os.environ.get("FUSION_AGENT_STUDIO_URL", "http://127.0.0.1:11442")).rstrip("/")
        key = (studio_api_key or os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "")).strip()
        headers = {"x-api-key": key} if key else {}
        timeout = float(policy.get("timeout_s") or 120)
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=headers, timeout=timeout, transport=capabilities_transport
            ) as client:
                if kind == "graph":
                    graph_id = str(detail.get("graph_id") or "")
                    resp = await client.post(
                        f"/v1/graphs/{graph_id}/execute",
                        json={"graph_id": graph_id, "input_text": text},
                    )
                else:
                    resp = await client.post(
                        "/v1/runs",
                        json={"goal": text, "agent_id": str(detail.get("agent_id") or "")},
                    )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            logger.warning(f"子代理派生失败 {cap_id}: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_unavailable", "message": str(e)}) from e
        capability_store.record_usage(
            ws, "subagent", cap_id, workflow=str(item.get("title") or ""), detail=text[:80], verdict="spawned"
        )
        logger.info(f"子代理派生 ({kind}): ws={ws} {cap_id}")
        return {"kind": kind, "result": payload, "spawn_policy": policy}

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
        # issue #228: 能力市场条目并入 ⌘K 搜索 (技能/连接器/子代理)
        try:
            for hit in search_items(
                q,
                normalize_workspace(""),
                store=capability_store,
                skills_dir=skills_dir,
                connector_dir=connector_dir,
                rag_wired_path=rag_wired_path,
            ):
                items.append(
                    {
                        "title": hit.get("title") or hit.get("name") or hit["id"],
                        "route": f"/capabilities/{hit['type']}",
                        "kind": "capability",
                        "capability_id": hit["id"],
                    }
                )
        except Exception as e:
            logger.warning(f"搜索能力市场失败 (跳过): {e}")
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
                    # issue #225: 审批卡 "编辑参数" — edited_params 随批准并入被闸节点输入
                    edited = body.get("edited_params")
                    edited = edited if isinstance(edited, dict) and edited else None
                    try:
                        resolver(hitl_exec, True, once=True, params=edited)
                    except TypeError:
                        try:
                            resolver(hitl_exec, True, once=True)
                        except TypeError:
                            # 兼容无 once 参数的旧引擎 — 规则未落, 复查会拒绝但 run 被唤醒
                            resolver(hitl_exec, True)
                logger.info(f"HITL 一次性批准已唤醒 run: {hitl_exec} (action_id={action_id}, 不落规则)")
                return {"action_id": action_id, "approved": True, "confirmed": True, "once": True}
            if released and not approved:
                # issue #216: 一次性拒绝同样要唤醒 run (拒绝路径无 once-grant, 不落规则),
                # 否则卡片出队后 confirm_guard 查不到条目返回 False, run 挂到 HITL 超时自动拒绝
                resolver = getattr(engine, "resolve_hitl", None)
                if callable(resolver):
                    resolver(hitl_exec, False)
                logger.info(f"HITL 一次性拒绝已唤醒 run: {hitl_exec} (action_id={action_id}, 不落规则)")
                return {"action_id": action_id, "approved": False, "confirmed": True, "once": True}
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

    # ── issue #190: 团队消息面 — 花名册 / 信箱 / 消费池生命周期 ──
    @app.get("/api/team/status")
    async def team_status():
        return app.state.runner_pool.status()

    @app.get("/api/team/roster")
    async def team_roster_list():
        items = app.state.team_roster.list()
        return {"agents": items, "count": len(items), "db": app.state.team_mailbox.path}

    @app.post("/api/team/roster", status_code=201)
    async def team_roster_register(request: Request):
        body = await request.json()
        try:
            row = app.state.team_roster.register(
                agent_id=str(body.get("agent_id") or ""),
                workflow_id=str(body.get("workflow_id") or ""),
                name=str(body.get("name") or ""),
                team=str(body.get("team") or ""),
                enabled=bool(body.get("enabled", True)),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_roster", "message": str(e)}) from e
        app.state.runner_pool.wake()
        return row

    @app.delete("/api/team/roster/{agent_id}")
    async def team_roster_remove(agent_id: str):
        if not app.state.team_roster.remove(agent_id):
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"agent 未登记: {agent_id}"})
        return {"removed": agent_id}

    @app.post("/api/team/send", status_code=201)
    async def team_send(request: Request):
        body = await request.json()
        try:
            message = app.state.team_mailbox.send(
                sender=str(body.get("sender") or ""),
                recipient=str(body.get("recipient") or ""),
                body=str(body.get("body") or ""),
                kind=str(body.get("kind") or "message"),
                team=str(body.get("team") or ""),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_message", "message": str(e)}) from e
        return message

    @app.get("/api/team/inbox")
    async def team_inbox(agent_id: str, unread: int = 1, limit: int = 100):
        if not agent_id:
            raise HTTPException(status_code=400, detail={"code": "missing_agent", "message": "agent_id 必填"})
        messages = app.state.team_mailbox.inbox(agent_id, unread_only=bool(unread), limit=limit)
        return {"agent_id": agent_id, "messages": messages, "count": len(messages)}

    @app.post("/api/team/notify")
    async def team_notify():
        # 推送路径: 对端 daemon 写信箱后调用本接口, 消费池立即醒来 (而非等轮询)
        app.state.runner_pool.wake()
        return {"notified": True, "running": app.state.runner_pool.running}

    @app.post("/api/team/pool/start")
    async def team_pool_start():
        await app.state.runner_pool.start()
        return {"running": app.state.runner_pool.running}

    @app.post("/api/team/pool/stop")
    async def team_pool_stop():
        await app.state.runner_pool.stop()
        return {"running": app.state.runner_pool.running}

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
