"""Desk RPC 服务端 — JSON-RPC 2.0 over Unix Domain Socket。

Fusion-Studio (Swift GUI) 通过 UDS 与 Fusion-Cowork 通信。
提供 desk.* 命名空间方法，对标 Studio 端 IPCClient。

协议:
  - 传输: Unix Domain Socket (/tmp/fusion-cowork.sock)
  - 编码: JSON-RPC 2.0 (每行一个 JSON 对象)
  - 命名空间: desk.health, desk.nodes.*, desk.workflow.*, desk.agent.*, desk.mlx.*
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_SOCK_PATH = "/tmp/fusion-cowork.sock"

_DEFAULT_PRINCIPAL = "local_user"
_AUTH_TOKEN_KEY = "desk.auth_token"
_IDENTITY_FIELDS = frozenset({"operator_id", "user_id", "inviter_id", "author_id", "owner_id", "from_user_id"})

_MAX_B64_BYTES = 50 * 1024 * 1024
_MAX_DECODED_BYTES = 25 * 1024 * 1024
_MAX_SYNC_FILES = 50
# MD-1: UDS 单行上限 1MiB + 读超时 5s, 防无 \n 长行 OOM; batch (数组请求) 显式拒 -32600
_MAX_RPC_LINE = 1024 * 1024
_RPC_READ_TIMEOUT = 5.0
# HI-13: collab 消息内容上限 (16KiB), 防存储型 prompt 注入 + 撑爆 session 记录
_MAX_COLLAB_CONTENT = 16 * 1024
# HI-15: 文本知识库扩展名白名单 (KB 主要消费文本; 二进制需 allow_binary 单独路径)
_TEXT_EXT_WHITELIST = frozenset(
    {".txt", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml", ".csv", ".html", ".htm", ".log", ".py", ".js", ".ts"}
)


def _secure_filename(name: str) -> str:
    """HI-15: 净化文件名 — 取 basename, 剥路径穿越 (../), 拒空/纯点。
    返回安全基名或空串 (调用方拒空)。"""
    if not isinstance(name, str) or not name.strip():
        return ""
    base = os.path.basename(name.replace("\\", "/"))
    base = base.strip().lstrip(".")
    if not base or base in {".", ".."}:
        return ""
    # 拒残留分隔符 (basename 后仍含 / 说明异常)
    if "/" in base or "\\" in base:
        return ""
    return base[:200]


def _strip_control_chars(text: str) -> str:
    """HI-13: 剥除 C0 控制字符 (保留 \\t \\n \\r), 防终端转义/隐藏指令注入。"""
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")


class DeskRPCServer:
    """Desk RPC 服务端 — 监听 UDS，处理 Studio 发来的 JSON-RPC 请求。"""

    def __init__(
        self,
        sock_path: str = DEFAULT_SOCK_PATH,
        event_emitter=None,
        session_store=None,
        permission_manager=None,
        hook_manager=None,
        space_store=None,
    ):
        self._sock_path = sock_path
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False
        self._handlers: Dict[str, Callable] = {}
        self._event_emitter = event_emitter
        self._session_store = session_store
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._space_store = space_store
        self._orchestrator = None
        self._presence_manager = None
        self._collab_hub = None
        self._collab_sessions: Dict[str, Dict[str, Any]] = {}
        self._collab_queues: Dict[str, asyncio.Queue] = {}
        self._auth_token: Optional[str] = None
        # A-10: 共享 MLX/KB 客户端实例 (旧版 11 处 handler 各 new FusionMLXClient() 不 close,
        # httpx 连接池泄漏)。handler 取共享实例, stop() 统一关闭。
        self._mlx_client = None
        self._kb_client = None
        self._engine = None  # E-7: 复用单例 engine, 避免每请求新建
        self._register_handlers()

    def _get_mlx_client(self):
        """A-10: 懒构造共享 FusionMLXClient (httpx 连接池复用, stop 统一 close)。"""
        if self._mlx_client is None:
            from ..ai import FusionMLXClient

            self._mlx_client = FusionMLXClient()
            logger.debug("DeskRPC 共享 FusionMLXClient 已构造")
        return self._mlx_client

    def _get_kb_client(self):
        """A-10: 懒构造共享 KBClient。"""
        if self._kb_client is None:
            from fusion_cowork.ai.mlx_client import KBClient

            self._kb_client = KBClient()
            logger.debug("DeskRPC 共享 KBClient 已构造")
        return self._kb_client

    def _register_handlers(self) -> None:
        """注册所有 desk.* JSON-RPC 方法。"""
        self._handlers = {
            # 健康检查
            "desk.health": self._handle_health,
            # 节点管理
            "desk.nodes.list": self._handle_nodes_list,
            "desk.nodes.info": self._handle_nodes_info,
            "desk.nodes.execute": self._handle_nodes_execute,
            "desk.nodes.categories": self._handle_nodes_categories,
            # 工作流
            "desk.workflow.list": self._handle_workflow_list,
            "desk.workflow.create": self._handle_workflow_create,
            "desk.workflow.run": self._handle_workflow_run,
            "desk.workflow.status": self._handle_workflow_status,
            "desk.workflow.cancel": self._handle_workflow_cancel,
            # 模板
            "desk.template.list": self._handle_template_list,
            "desk.template.get": self._handle_template_get,
            "desk.template.run": self._handle_template_run,
            # 智能体
            "desk.agent.list": self._handle_agent_list,
            "desk.agent.submit": self._handle_agent_submit,
            "desk.agent.status": self._handle_agent_status,
            "desk.agent.cancel": self._handle_agent_cancel,
            # MLX
            "desk.mlx.status": self._handle_mlx_status,
            "desk.mlx.models": self._handle_mlx_models,
            # 系统
            "desk.system.info": self._handle_system_info,
            "desk.system.set_scoped_folder": self._handle_set_scoped_folder,
            "desk.system.scoped_folder": self._handle_get_scoped_folder,
            # 事件订阅
            "desk.events.subscribe": self._handle_events_subscribe,
            "desk.events.recent": self._handle_events_recent,
            "desk.events.poll": self._handle_events_poll,
            # 会话
            "desk.session.list": self._handle_session_list,
            "desk.session.get": self._handle_session_get,
            "desk.session.fork": self._handle_session_fork,
            "desk.session.create": self._handle_session_create,
            "desk.session.update": self._handle_session_update,
            "desk.session.delete": self._handle_session_delete,
            # 权限
            "desk.permission.check": self._handle_permission_check,
            "desk.permission.approve": self._handle_permission_approve,
            "desk.permission.deny": self._handle_permission_deny,
            "desk.permission.list": self._handle_permission_list,
            "desk.permission.reset": self._handle_permission_reset,
            # 协作空间
            "desk.space.create": self._handle_space_create,
            "desk.space.list": self._handle_space_list,
            "desk.space.get": self._handle_space_get,
            "desk.space.update": self._handle_space_update,
            "desk.space.archive": self._handle_space_archive,
            "desk.space.delete": self._handle_space_delete,
            "desk.space.member.invite": self._handle_space_member_invite,
            "desk.space.member.join": self._handle_space_member_join,
            "desk.space.member.list": self._handle_space_member_list,
            "desk.space.member.remove": self._handle_space_member_remove,
            "desk.space.member.update_role": self._handle_space_member_update_role,
            # 协作空间 - Presence + 光标
            "desk.space.presence.heartbeat": self._handle_space_presence_heartbeat,
            "desk.space.presence.cursor": self._handle_space_presence_cursor,
            "desk.space.presence.list": self._handle_space_presence_list,
            "desk.space.presence.remove": self._handle_space_presence_remove,
            # 协作空间 - WS 双向 (轮询桥接, 供非 WS 主机)
            "desk.space.collab.join": self._handle_space_collab_join,
            "desk.space.collab.send": self._handle_space_collab_send,
            "desk.space.collab.cursor": self._handle_space_collab_cursor,
            "desk.space.collab.poll": self._handle_space_collab_poll,
            "desk.space.collab.leave": self._handle_space_collab_leave,
            # 协作空间 - 对话
            "desk.space.chat.send": self._handle_space_chat_send,
            "desk.space.chat.list": self._handle_space_chat_list,
            "desk.space.chat.history": self._handle_space_chat_list,
            "desk.space.chat.context": self._handle_space_chat_context,
            # 协作空间 - 知识库
            "desk.space.knowledge.bind": self._handle_space_kb_bind,
            "desk.space.knowledge.status": self._handle_space_kb_status,
            "desk.space.knowledge.upload": self._handle_space_kb_upload,
            "desk.space.knowledge.search": self._handle_space_kb_search,
            "desk.space.knowledge.query": self._handle_space_kb_query,
            "desk.space.knowledge.unbind": self._handle_space_kb_unbind,
            # 协作空间 - Agent
            "desk.space.agent.list": self._handle_space_agent_list,
            "desk.space.agent.add": self._handle_space_agent_add,
            "desk.space.agent.remove": self._handle_space_agent_remove,
            "desk.space.agent.call": self._handle_space_agent_call,
            "desk.space.agent.relay": self._handle_space_agent_relay,
            "desk.space.agent.update": self._handle_space_agent_update,
            # 协作空间 - 快照
            "desk.space.snapshot.list": self._handle_space_snapshot_list,
            "desk.space.snapshot.create": self._handle_space_snapshot_create,
            "desk.space.snapshot.clone": self._handle_space_snapshot_clone,
            "desk.space.snapshot.restore": self._handle_space_snapshot_restore,
            "desk.space.snapshot.delete": self._handle_space_snapshot_delete,
            # 协作空间 - 流式对话
            "desk.space.chat.stream": self._handle_space_chat_stream,
            # 协作空间 - 评论
            "desk.space.comment.create": self._handle_space_comment_create,
            "desk.space.comment.list": self._handle_space_comment_list,
            # 协作空间 - 工作流
            "desk.space.workflow.list": self._handle_space_workflow_list,
            "desk.space.workflow.create": self._handle_space_workflow_create,
            "desk.space.workflow.run": self._handle_space_workflow_run,
            # 协作空间 - 发现
            "desk.space.discovery.scan": self._handle_space_discovery_scan,
            # 协作空间 - 桌面共享
            "desk.space.desktop.share": self._handle_space_desktop_share,
            "desk.space.desktop.control": self._handle_space_desktop_control,
            # 会话快照
            "desk.session.snapshot_list": self._handle_session_snapshot_list,
            "desk.session.snapshot_create": self._handle_session_snapshot_create,
            "desk.session.snapshot_restore": self._handle_session_snapshot_restore,
            "desk.session.snapshot_fork": self._handle_session_snapshot_fork,
            "desk.session.snapshot_delete": self._handle_session_snapshot_delete,
            # 跨产品集成 — fusion-projects
            "desk.project.syncKnowledge": self._handle_project_sync_knowledge,
            "desk.project.importSnapshot": self._handle_project_import_snapshot,
            "desk.project.exportToProject": self._handle_project_export_to_project,
            # 跨产品集成 — fusion-projects 中转 (ST→project-svc→cowork)
            "cowork.trigger": self._handle_cowork_trigger,
            "cowork.status": self._handle_cowork_status,
            # 协作空间 - Artifact 权限
            "desk.space.artifact.create": self._handle_space_artifact_create,
            "desk.space.artifact.get": self._handle_space_artifact_get,
            "desk.space.artifact.update": self._handle_space_artifact_update,
            "desk.space.artifact.share": self._handle_space_artifact_share,
            "desk.space.artifact.transfer": self._handle_space_artifact_transfer,
            "desk.space.artifact.list": self._handle_space_artifact_list,
            "desk.space.artifact.delete": self._handle_space_artifact_delete,
            # 侧边栏模块
            "desk.module.register": self._handle_module_register,
            "desk.module.list": self._handle_module_list,
            "desk.module.enable": self._handle_module_enable,
            "desk.module.disable": self._handle_module_disable,
            # 通知推送
            "desk.notification.push": self._handle_notification_push,
            "desk.notification.list": self._handle_notification_list,
            "desk.notification.markRead": self._handle_notification_mark_read,
            # 通知推送 — 兼容 Studio space.notification 命名空间
            "desk.space.notification.list": self._handle_notification_list,
            "desk.space.notification.mark_read": self._handle_notification_mark_read,
        }
        logger.info(f"Desk RPC 注册 {len(self._handlers)} 个方法")

    async def start(self) -> None:
        """启动 RPC 服务端。"""
        from ..config_center import ConfigCenter
        from ..nodes import import_all_nodes

        import_all_nodes()

        if self._space_store is not None:
            try:
                await self._space_store.initialize()
            except Exception as e:
                logger.warning(f"SpaceStore 初始化失败 (desk.space.* 不可用): {e}")
        self._auth_token = ConfigCenter.get_instance().get(_AUTH_TOKEN_KEY) or None
        if self._auth_token:
            logger.info("Desk RPC 认证已启用: desk.auth_token 握手校验")
        if os.path.exists(self._sock_path):
            if not self._is_owned_socket(self._sock_path):
                logger.error(f"UDS {self._sock_path} 已存在但非当前用户所有, 拒绝覆盖 (可能被恶意占用)")
                raise RuntimeError(f"UDS {self._sock_path} 被非当前用户占用")
            os.unlink(self._sock_path)

        self._running = True
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._sock_path,
        )
        try:
            os.chmod(self._sock_path, 0o600)
        except OSError as e:
            logger.warning(f"UDS 权限设置 0o600 失败 (仅 owner 可连接): {e}")
        logger.info(f"Desk RPC 服务启动: {self._sock_path} (perms 0o600, owner-only)")

    @staticmethod
    def _is_owned_socket(path: str) -> bool:
        """校验 UDS 文件属当前用户所有, 防恶意占用后窃听 (darwin 无 SO_PEERCRED)。"""
        try:
            st = os.stat(path)
            return st.st_uid == os.getuid()
        except OSError:
            return True

    async def stop(self) -> None:
        """停止 RPC 服务端。"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._space_store is not None:
            try:
                await self._space_store.close()
            except Exception as e:
                logger.debug(f"SpaceStore 关闭失败: {e}")
        # A-10: 关闭共享 MLX/KB 客户端 (旧版各 handler new 后不 close, 连接池泄漏)
        if self._mlx_client is not None:
            try:
                await self._mlx_client.close()
            except Exception as e:
                logger.debug(f"FusionMLXClient 关闭失败: {e}")
            self._mlx_client = None
        if self._kb_client is not None:
            try:
                await self._kb_client.close()
            except Exception as e:
                logger.debug(f"KBClient 关闭失败: {e}")
            self._kb_client = None
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)
        logger.info("Desk RPC 服务停止")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理单个客户端连接。"""
        addr = writer.get_extra_info("peername")
        logger.info(f"Desk RPC 客户端连接: {addr}")

        try:
            while self._running:
                # MD-1: 有界读 — readuntil(\n) 限 _MAX_RPC_LINE, 超长拒 -32700, 超 5s 断
                try:
                    raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_RPC_READ_TIMEOUT)
                except asyncio.IncompleteReadError:
                    break
                except asyncio.LimitOverrunError:
                    logger.warning("Desk RPC 行超 %d 字节上限, 拒绝并断开", _MAX_RPC_LINE)
                    await self._write_response(
                        writer,
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Request too large"}},
                    )
                    break
                except TimeoutError:
                    logger.warning("Desk RPC 读超时 %ss, 断开", _RPC_READ_TIMEOUT)
                    break
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"无效 JSON: {e}")
                    await self._write_response(
                        writer,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        },
                    )
                    continue

                # MD-1: JSON-RPC batch (数组) 显式拒 -32600, 不当 dict 分发
                if isinstance(request, list):
                    logger.warning("Desk RPC 拒绝 batch 请求 (数组, %d 元素)", len(request))
                    await self._write_response(
                        writer,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32600, "message": "Batch requests not supported"},
                        },
                    )
                    continue

                response = await self._dispatch(request)
                # MD-1: 通知 (无 id) 不回响应 — JSON-RPC 2.0 规范
                if response.get("id") is None and "id" not in request:
                    continue
                await self._write_response(writer, response)
        except Exception as e:
            logger.error(f"客户端处理异常: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info(f"Desk RPC 客户端断开: {addr}")

    async def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """分发 JSON-RPC 请求。"""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        handler = self._handlers.get(method)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        authed = self._authenticate(params)
        if isinstance(authed, dict) and "__auth_error__" in authed:
            err = authed["__auth_error__"]
            return {"jsonrpc": "2.0", "id": req_id, "error": err}

        space_err = await self._check_space_access(method, authed)
        if space_err is not None:
            if req_id is not None:
                return {"jsonrpc": "2.0", "id": req_id, "result": space_err}
            return {"jsonrpc": "2.0", "result": space_err}

        try:
            result = await handler(authed)
            if req_id is not None:
                return {"jsonrpc": "2.0", "id": req_id, "result": result}
            return {"jsonrpc": "2.0", "result": result}
        except Exception as e:
            # HI-5: 对外仅 trace_id + 通用消息, 不泄内部栈/绝对路径/SQL 错误;
            # 服务端详记 trace_id ↔ (method, params 摘要, 完整 traceback) 供排查
            trace_id = secrets.token_hex(8)
            param_keys = list(authed.keys()) if isinstance(authed, dict) else []
            logger.error(
                "Desk RPC 处理异常 trace_id=%s method=%s param_keys=%s err=%s\n%s",
                trace_id,
                method,
                param_keys,
                e,
                traceback.format_exc(),
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {"trace_id": trace_id},
                },
            }

    def _authenticate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """认证调用方, 返回带可信身份的 params (CR-5 反 IDOR)。

        - desk.auth_token 配了则校验 params._auth_token, 缺/错拒。
        - 身份字段 (operator_id/user_id/inviter_id/author_id/owner_id/from_user_id) 不信 params,
          一律改用连接级 principal (本地单用户 = local_user, UDS 0o600 已保证连接即本机 owner)。
        - 返回新 dict: 剥离调用方提供的身份字段, 注入可信 principal 到全部身份字段。
        """
        if self._auth_token:
            token = params.get("_auth_token", "")
            if not isinstance(token, str) or token != self._auth_token:
                logger.warning("Desk RPC 认证失败: _auth_token 缺失或不匹配")
                return {"__auth_error__": {"code": -32001, "message": "认证失败: token 无效"}}
        sanitized = {k: v for k, v in params.items() if k not in _IDENTITY_FIELDS and k != "_auth_token"}
        sanitized["__principal__"] = _DEFAULT_PRINCIPAL
        sanitized["operator_id"] = _DEFAULT_PRINCIPAL
        sanitized["user_id"] = _DEFAULT_PRINCIPAL
        sanitized["inviter_id"] = _DEFAULT_PRINCIPAL
        sanitized["author_id"] = _DEFAULT_PRINCIPAL
        return sanitized

    async def _require_space_access(self, space_id: str, principal: str, action: str) -> Optional[Dict[str, Any]]:
        """校验 principal 对 space 的访问权 (CR-5 IDOR 守卫)。返回 error_dict 或 None。"""
        if not self._space_store or not space_id:
            return None
        space = await self._space_store.get_space(space_id)
        if not space:
            return {"error": f"空间不存在: {space_id}"}
        if principal == space.owner_id:
            return None
        from ..space.permission import SpacePermission

        perm = SpacePermission(self._space_store)
        member = await self._space_store.get_member(space_id, principal)
        if member is None:
            logger.warning(f"IDOR 拒绝: principal={principal} 非空间 {space_id} 成员")
            return {"error": f"无权访问空间 {space_id}"}
        if action and not await perm.check(space_id, principal, action):
            return {"error": f"权限不足: 缺 {action}"}
        return None

    _SPACE_ACCESS_ACTIONS: Dict[str, str] = {
        "desk.space.get": "",
        "desk.space.update": "manage_space",
        "desk.space.archive": "manage_space",
        "desk.space.delete": "manage_space",
        "desk.space.member.invite": "manage_members",
        "desk.space.member.list": "",
        "desk.space.member.remove": "manage_members",
        "desk.space.member.update_role": "manage_members",
        "desk.space.presence.heartbeat": "",
        "desk.space.presence.cursor": "",
        "desk.space.presence.list": "",
        "desk.space.presence.remove": "",
        "desk.space.collab.join": "",
        "desk.space.collab.send": "send_message",
        "desk.space.collab.cursor": "",
        "desk.space.collab.poll": "",
        "desk.space.collab.leave": "",
        "desk.space.chat.send": "send_message",
        "desk.space.chat.list": "",
        "desk.space.chat.history": "",
        "desk.space.chat.context": "",
        "desk.space.chat.stream": "send_message",
        "desk.space.knowledge.bind": "manage_kb",
        "desk.space.knowledge.status": "",
        "desk.space.knowledge.upload": "upload_document",
        "desk.space.knowledge.search": "",
        "desk.space.knowledge.query": "",
        "desk.space.knowledge.unbind": "manage_kb",
        "desk.space.agent.list": "",
        "desk.space.agent.add": "manage_agents",
        "desk.space.agent.remove": "manage_agents",
        "desk.space.agent.call": "call_agent",
        "desk.space.agent.relay": "call_agent",
        "desk.space.agent.update": "manage_agents",
        "desk.space.snapshot.list": "",
        "desk.space.snapshot.create": "manage_snapshots",
        "desk.space.snapshot.clone": "manage_snapshots",
        "desk.space.snapshot.restore": "manage_snapshots",
        "desk.space.snapshot.delete": "manage_snapshots",
        "desk.space.comment.create": "send_message",
        "desk.space.comment.list": "",
        "desk.space.workflow.list": "",
        "desk.space.workflow.create": "run_workflow",
        "desk.space.workflow.run": "run_workflow",
        "desk.space.discovery.scan": "",
        "desk.space.desktop.share": "",
        "desk.space.desktop.control": "",
        "desk.space.artifact.create": "upload_file",
        "desk.space.artifact.get": "view_artifact",
        "desk.space.artifact.update": "edit_artifact",
        "desk.space.artifact.share": "share_artifact",
        "desk.space.artifact.transfer": "transfer_artifact",
        "desk.space.artifact.list": "view_artifact",
        "desk.space.artifact.delete": "delete_data",
        "desk.space.notification.list": "",
        "desk.space.notification.mark_read": "",
        "desk.project.syncKnowledge": "upload_document",
        "desk.project.importSnapshot": "send_message",
        "desk.project.exportToProject": "",
    }

    async def _check_space_access(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """dispatch 层 IDOR 守卫: desk.space.* + desk.project.* 按 method→action 校验。

        desk.space.member.join (invite_code 自助加入) / desk.space.create / desk.space.list 不校验。
        """
        action = self._SPACE_ACCESS_ACTIONS.get(method)
        if action is None:
            return None
        space_id = params.get("space_id", "")
        if not space_id:
            return None
        principal = params.get("__principal__", _DEFAULT_PRINCIPAL)
        return await self._require_space_access(space_id, principal, action)

    def _internal_error(self, e: Exception, method: str = "") -> Dict[str, Any]:
        """HI-5: handler 业务异常统一对外返回通用消息 + trace_id, 不泄 str(e) 内部细节
        (绝对路径/SQL 列名/httpx URL); 服务端详记 trace_id↔(method, err, traceback)。"""
        trace_id = secrets.token_hex(8)
        logger.error(
            "Desk RPC handler 异常 trace_id=%s method=%s err=%s\n%s",
            trace_id,
            method,
            e,
            traceback.format_exc(),
        )
        return {"error": "内部错误, 请联系管理员并提供 trace_id", "trace_id": trace_id}

    async def _write_response(self, writer: asyncio.StreamWriter, response: Dict[str, Any]) -> None:
        """写入 JSON-RPC 响应 — 序列化失败时降级为错误帧，避免静默断连 (0 bytes)。"""
        try:
            data = json.dumps(response, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            # HI-5: 序列化失败只记顶层键名, 不记 response!r (含节点 data/文件内容/shell 输出)
            resp_keys = list(response.keys()) if isinstance(response, dict) else type(response).__name__
            logger.error("Desk RPC 响应序列化失败, 降级错误帧: %s | resp_keys=%s", e, resp_keys)
            req_id = response.get("id") if isinstance(response, dict) else None
            fallback = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Response serialization failed"},
            }
            data = json.dumps(fallback, ensure_ascii=False) + "\n"
        writer.write(data.encode("utf-8"))
        await writer.drain()

    # ── 方法实现 ──

    async def _handle_health(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from .. import __version__

        return {"status": "ok", "service": "fusion-cowork", "version": __version__}

    async def _handle_nodes_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry

        category = params.get("category")
        cat_filter = None
        if category:
            from ..engine.node import NodeCategory

            cat_filter = NodeCategory(category) if category in {c.value for c in NodeCategory} else None
        nodes = NodeRegistry.list(category=cat_filter)
        return {"nodes": nodes, "count": len(nodes)}

    async def _handle_nodes_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry

        name = params.get("name", "")
        cls = NodeRegistry._registry.get(name)
        if not cls:
            return {"error": f"节点未注册: {name}"}
        doc = getattr(cls, "__doc__", "") or ""
        cat = getattr(cls, "category", None)
        cat_value = cat.value if hasattr(cat, "value") else (cat or "unknown")
        return {
            "name": name,
            "category": cat_value,
            "description": doc.strip(),
            "params_schema": getattr(cls, "params_schema", {}),
        }

    async def _handle_nodes_execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeConfig, NodeRegistry

        name = params.get("name", "")
        node_params = params.get("params", {})
        if self._permission_manager:
            allowed = await self._permission_manager.check(name, "execute", node_params)
            if not allowed:
                logger.warning(f"节点执行被权限拒绝: {name}")
                return {"error": f"权限拒绝: {name}"}
        node = NodeRegistry.create(name, config=NodeConfig(params=node_params))
        if not node:
            return {"error": f"节点创建失败: {name}"}
        result = await node.execute(node_params)
        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
            "error": result.error,
        }

    async def _handle_nodes_categories(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry

        categories: Dict[str, int] = {}
        for name, cls in NodeRegistry._registry.items():
            cat = getattr(cls, "category", None)
            cat_value = cat.value if hasattr(cat, "value") else (cat or "unknown")
            categories[cat_value] = categories.get(cat_value, 0) + 1
        return {"categories": categories, "count": len(categories)}

    async def _handle_workflow_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..templates import TemplateManager

        mgr = TemplateManager()
        templates = mgr.list_templates()
        return {"templates": templates, "count": len(templates)}

    async def _handle_workflow_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..ai import NLWorkflowGenerator

        prompt = params.get("prompt", "")
        if not prompt:
            return {"error": "prompt 不能为空"}
        gen = NLWorkflowGenerator()
        workflow = await gen.generate(prompt)
        return {"workflow": workflow.to_dict() if workflow else None}

    async def _handle_workflow_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine import Workflow

        template_id = params.get("template_id", "")
        workflow_def = params.get("workflow", {})
        if not workflow_def and template_id:
            from ..templates import TemplateManager

            tmpl = TemplateManager().get_template(template_id)
            if tmpl:
                workflow_def = tmpl
            else:
                return {"error": f"模板不存在: {template_id}"}
        if not workflow_def:
            return {"error": "workflow 或 template_id 必填"}
        wf = Workflow.from_dict(workflow_def)
        engine = self._get_engine()
        result = await engine.execute(wf)
        return {
            "id": result.id,
            "status": result.status.value if hasattr(result.status, "value") else str(result.status),
            "summary": result.result_summary,
            "steps": [s.to_dict() if hasattr(s, "to_dict") else str(s) for s in result.steps],
            "error": result.error,
        }

    async def _handle_workflow_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "idle", "running_workflows": 0}

    async def _handle_workflow_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:

        execution_id = params.get("execution_id", "")
        if not execution_id:
            return {"error": "execution_id 不能为空"}
        engine = self._get_engine()
        engine.cancel(execution_id)
        return {"status": "cancelled", "execution_id": execution_id}

    async def _handle_template_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..templates import TemplateManager

        mgr = TemplateManager()
        category = params.get("category", "")
        templates = mgr.list_templates()
        if category:
            templates = [t for t in templates if t.get("category") == category]
        return {"templates": templates, "count": len(templates)}

    async def _handle_template_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..templates import TemplateManager

        template_id = params.get("template_id", "") or params.get("id", "")
        mgr = TemplateManager()
        template = mgr.get_template(template_id)
        if not template:
            return {"error": f"模板不存在: {template_id}"}
        return template

    async def _handle_template_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine import Workflow
        from ..templates import TemplateManager

        template_id = params.get("template_id", "")
        _variables = params.get("variables", {})
        mgr = TemplateManager()
        template = mgr.get_template(template_id)
        if not template:
            return {"error": f"模板不存在: {template_id}"}
        wf_data = template.get("workflow", template)
        wf = Workflow.from_dict(wf_data)
        engine = self._get_engine()
        result = await engine.execute(wf)
        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
        }

    def _get_orchestrator(self):
        from ..orchestrator import AgentOrchestrator

        if self._orchestrator is None:
            # HI-9: 注入父引擎运行时, 子工作流执行器受同一权限/ Hook 约束
            self._orchestrator = AgentOrchestrator(
                permission_manager=self._permission_manager,
                hook_manager=self._hook_manager,
                session_store=self._session_store,
            )
            self._orchestrator.register_default_agents()
            logger.info("DeskRPC 共享 AgentOrchestrator 已构建并注册默认 Agent")
        return self._orchestrator

    def _get_engine(self):
        # E-7: 旧版每请求新建 WorkflowEngine → 重复构造开销。缓存单例 (其依赖均共享级)。
        if self._engine is not None:
            return self._engine
        from ..engine import WorkflowEngine

        self._engine = WorkflowEngine(
            permission_manager=self._permission_manager,
            hook_manager=self._hook_manager,
            session_store=self._session_store,
            event_emitter=self._event_emitter,
        )
        logger.debug("DeskRPC 复用单例 WorkflowEngine")
        return self._engine

    async def _handle_agent_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        orch = self._get_orchestrator()
        agents = [{"id": a.agent_id, "name": a.name, "role": a.role.value} for a in orch._agents.values()]
        return {"agents": agents, "count": len(agents)}

    async def _handle_agent_submit(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task = params.get("task", "")
        if not task:
            return {"error": "task 不能为空"}
        orch = self._get_orchestrator()
        task_id = await orch.submit_task(task)
        return {"task_id": task_id}

    async def _handle_agent_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        orch = self._get_orchestrator()
        task_id = params.get("task_id", "")
        t = orch.get_task(task_id)
        if t:
            return {
                "task_id": task_id,
                "status": t.status,
                "result": t.output_data,
                "error": t.error,
            }
        return {"status": "idle"}

    async def _handle_agent_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id = params.get("task_id", "")
        if not task_id:
            return {"error": "task_id 不能为空"}
        orch = self._get_orchestrator()
        ok = orch.cancel_task(task_id)
        return {"status": "cancelled" if ok else "noop", "task_id": task_id}

    async def _handle_mlx_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client = self._get_mlx_client()
        try:
            status = await client.health_check()
            return {"status": "running", "info": status}
        except Exception:
            return {"status": "stopped"}

    async def _handle_mlx_models(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client = self._get_mlx_client()
        try:
            models = await client.list_models()
            return {"models": models}
        except Exception as e:
            return {"error": str(e), "models": []}

    async def _handle_system_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import platform

        import psutil

        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "memory_used_pct": psutil.virtual_memory().percent,
            "disk_free_gb": round(psutil.disk_usage("/").free / (1024**3), 1),
        }

    async def _handle_set_scoped_folder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GUI 选定授权工作文件夹后下发 — P1-1 配套 (fusion-studio issue #202)。

        params: {"folders": ["/abs/path", ...], "enforce": true}
        设置 ScopedFolderManager 单例, enforce=True 时文件节点/ShellExec 强制校验越界。
        """
        from ..security import ScopedFolderManager, get_scoped_folder_manager, reset_scoped_folder_manager

        folders = params.get("folders", [])
        enforce = bool(params.get("enforce", True))
        if not isinstance(folders, list) or not folders:
            return {"error": "folders 必须为非空绝对路径列表"}
        reset_scoped_folder_manager()
        # 用指定 folders 重建单例 (from_config 走配置默认, 这里覆盖)
        mgr = ScopedFolderManager(scoped_folders=[str(f) for f in folders], enforce=enforce)
        import fusion_cowork.security.scoped_folder as _sf

        _sf._default_manager = mgr
        cur = get_scoped_folder_manager()
        logger.info(f"set_scoped_folder: {len(cur.scopes)} 文件夹, enforce={cur.enforce}")
        return {"set": True, "folders": [str(s) for s in cur.scopes], "enforce": cur.enforce}

    async def _handle_get_scoped_folder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..security import get_scoped_folder_manager

        mgr = get_scoped_folder_manager()
        return {"folders": [str(s) for s in mgr.scopes], "enforce": mgr.enforce}

    async def _handle_events_subscribe(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        sub_id, queue = self._event_emitter.subscribe()
        return {"sub_id": sub_id, "message": "已订阅事件流，通过 desk.events.poll 获取"}

    async def _handle_events_recent(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        since = params.get("since", 0.0)
        events = self._event_emitter.get_buffered(since=since)
        return {
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }

    async def _handle_events_poll(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        sub_id = params.get("sub_id", "")
        events = []
        if sub_id and sub_id in self._event_emitter._subscribers:
            queue = self._event_emitter._subscribers[sub_id]
            while not queue.empty():
                events.append(queue.get_nowait().to_dict())
        return {"count": len(events), "events": events}

    async def _handle_session_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        status = params.get("status")
        limit = params.get("limit", 20)
        sessions = self._session_store.list_sessions(status=status, limit=limit)
        return {
            "count": len(sessions),
            "sessions": [self._session_store.to_dict(s) for s in sessions],
        }

    async def _handle_session_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        s = self._session_store.get(session_id)
        if not s:
            return {"error": f"会话不存在: {session_id}"}
        return self._session_store.to_dict(s)

    async def _handle_session_fork(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        from_step = params.get("from_step", 0)
        forked = self._session_store.fork(session_id, from_step=from_step)
        if not forked:
            return {"error": f"分叉失败: {session_id}"}
        return self._session_store.to_dict(forked)

    async def _handle_session_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        from fusion_cowork.engine.session import Session

        name = params.get("name", "")
        workflow_id = params.get("workflow_id", "")
        workflow_name = name or params.get("workflow_name", "")
        initial_input = params.get("initial_input", {})
        metadata = params.get("metadata", {})
        if description := params.get("description", ""):
            metadata["description"] = description
        if space_id := params.get("space_id", ""):
            metadata["space_id"] = space_id
        if user_id := params.get("user_id", ""):
            metadata["user_id"] = user_id
        session = Session(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            initial_input=initial_input,
            metadata=metadata,
        )
        self._session_store.save(session)
        logger.info(f"Session created: {session.id} workflow={workflow_name}")
        return self._session_store.to_dict(session)

    async def _handle_session_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        session = self._session_store.get(session_id)
        if not session:
            return {"error": f"会话不存在: {session_id}"}
        updates = params.get("updates", {})
        if "name" in updates or "workflow_name" in updates:
            session.workflow_name = updates.get("workflow_name", updates.get("name", session.workflow_name))
        if "status" in updates:
            self._session_store.update_status(session_id, updates["status"])
        if "steps" in updates:
            self._session_store.update_steps(session_id, updates["steps"])
        if "metadata" in updates:
            new_meta = updates["metadata"]
            if not isinstance(new_meta, dict):
                return {"error": "metadata 必须为对象"}
            # HI-14: bound metadata 大小, 防客户端灌超大 dict 撑爆 session 记录
            _MAX_META_BYTES = 64 * 1024
            merged = dict(session.metadata)
            merged.update(new_meta)
            if len(json.dumps(merged, ensure_ascii=False)) > _MAX_META_BYTES:
                return {"error": f"metadata 超 {_MAX_META_BYTES} 字节上限"}
            session.metadata = merged
            self._session_store.save(session)
        session = self._session_store.get(session_id)
        return self._session_store.to_dict(session)

    async def _handle_session_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        deleted = self._session_store.delete(session_id)
        return {"deleted": deleted, "session_id": session_id}

    async def _handle_permission_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        tool_params = params.get("params", {})
        allowed = await self._permission_manager.check(tool_name, "execute", tool_params)
        return {"tool_name": tool_name, "allowed": allowed}

    async def _handle_permission_approve(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        scope = params.get("scope", "*")
        self._permission_manager.approve(tool_name, scope=scope)
        return {"tool_name": tool_name, "approved": True, "scope": scope}

    async def _handle_permission_deny(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        scope = params.get("scope", "*")
        self._permission_manager.deny(tool_name, scope=scope)
        return {"tool_name": tool_name, "denied": True, "scope": scope}

    async def _handle_permission_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        return self._permission_manager.to_dict()

    async def _handle_permission_reset(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        self._permission_manager.reset()
        return {"status": "reset"}

    # ── 协作空间 ──

    async def _handle_space_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService

        svc = SpaceService(self._space_store)
        name = params.get("name", "")
        owner_id = params.get("owner_id", "local_user")
        description = params.get("description", "")
        collab_mode = params.get("collab_mode", "local")
        try:
            sp = await svc.create(name=name, owner_id=owner_id, description=description, collab_mode=collab_mode)
            return sp.to_dict()
        except Exception as e:
            logger.error(f"space.create 失败: {e}")
            return self._internal_error(e)

    async def _handle_space_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService

        svc = SpaceService(self._space_store)
        status = params.get("status")
        owner_id = params.get("owner_id")
        limit = params.get("limit", 20)
        spaces = await svc.list(status=status, owner_id=owner_id, limit=limit)
        return {"spaces": [s.to_dict() for s in spaces], "count": len(spaces)}

    async def _handle_space_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService

        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        sp = await svc.get(space_id)
        if not sp:
            return {"error": f"空间不存在: {space_id}"}
        return sp.to_dict()

    async def _handle_space_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService

        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        updates = params.get("updates", {})
        # HI-14: handler 层显式字段白名单, 拒 owner_id/status/kb_*/collab_mode/config
        # (客户端经 **updates 可夺权或反归档)。store 层 CR-4 白名单含这些高危列,
        # 此处再收紧到用户可改的业务字段。
        _USER_EDITABLE = {"name", "description"}
        cleaned = {}
        for k, v in updates.items():
            if k not in _USER_EDITABLE:
                logger.warning("desk.space.update 拒非用户可编辑字段: %s", k)
                continue
            cleaned[k] = v
        if not cleaned:
            return {"error": "无可更新字段 (仅允许 name/description)"}
        sp = await svc.update(space_id, **cleaned)
        if not sp:
            return {"error": f"空间不存在: {space_id}"}
        return sp.to_dict()

    async def _handle_space_archive(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService

        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        try:
            result = await svc.archive(space_id)
            return {"space_id": space_id, "archived": bool(result)}
        except Exception as e:
            logger.error(f"space.archive 失败: {e}")
            return self._internal_error(e)

    async def _handle_space_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService

        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        await svc.delete(space_id)
        return {"space_id": space_id, "deleted": True}

    async def _handle_space_member_invite(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission

        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        space_id = params.get("space_id", "")
        inviter_id = params.get("inviter_id", "")
        if not inviter_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            inviter_id = sp.owner_id if sp else "local_user"
        role = params.get("role", "member")
        max_uses = params.get("max_uses", 0)
        expires_hours = params.get("expires_hours", 0)
        try:
            code = await svc.invite(space_id, inviter_id, role=role, max_uses=max_uses, expires_hours=expires_hours)
            return {"invite_code": code, "space_id": space_id}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_member_join(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission

        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        invite_code = params.get("invite_code", "")
        user_id = params.get("user_id", "")
        display_name = params.get("display_name", "")
        try:
            member = await svc.join(invite_code, user_id=user_id, display_name=display_name)
            return member.to_dict()
        except ValueError as e:
            return {"error": str(e)}

    async def _handle_space_member_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        members = await self._space_store.list_members(space_id)
        return {"members": [m.to_dict() for m in members], "count": len(members)}

    async def _handle_space_member_remove(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission

        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            removed = await svc.remove(space_id, user_id, operator_id=operator_id)
            return {"space_id": space_id, "user_id": user_id, "removed": removed}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_member_update_role(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission

        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        new_role = params.get("new_role", "member")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            member = await svc.update_role(space_id, user_id, new_role, operator_id=operator_id)
            return member.to_dict()
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    def _get_presence_manager(self):
        if self._presence_manager is None:
            from fusion_cowork.space import PresenceManager

            self._presence_manager = PresenceManager(event_emitter=self._event_emitter)
        return self._presence_manager

    async def _handle_space_presence_heartbeat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pm = self._get_presence_manager()
        st = pm.heartbeat(
            params.get("space_id", ""),
            params.get("user_id", ""),
            display_name=params.get("display_name", ""),
            extras=params.get("extras"),
        )
        return st.to_dict()

    async def _handle_space_presence_cursor(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pm = self._get_presence_manager()
        st = pm.set_cursor(
            params.get("space_id", ""),
            params.get("user_id", ""),
            float(params.get("x", 0)),
            float(params.get("y", 0)),
            target=params.get("target", ""),
        )
        return st.to_dict()

    async def _handle_space_presence_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pm = self._get_presence_manager()
        states = pm.list_present(params.get("space_id", ""))
        return {"members": [s.to_dict() for s in states], "count": len(states)}

    async def _handle_space_presence_remove(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pm = self._get_presence_manager()
        removed = pm.remove(params.get("space_id", ""), params.get("user_id", ""))
        return {"removed": removed}

    def _get_collab_hub(self):
        if self._collab_hub is None:
            from fusion_cowork.server.collab_ws import CollabHub

            self._collab_hub = CollabHub(presence_manager=self._get_presence_manager())
        return self._collab_hub

    def _mock_ws_for(self, session_id: str):
        queue = self._collab_queues.setdefault(session_id, asyncio.Queue(maxsize=500))

        class _MockWS:
            async def send(self, payload):
                try:
                    await queue.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.warning(f"collab 会话 {session_id} 队列已满, 丢弃")

        return _MockWS(), queue

    async def _handle_space_collab_join(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import secrets as _secrets

        hub = self._get_collab_hub()
        # HI-13: principal 取认证身份, 不信 params.user_id (CR-5 同源); 128-bit 会话 id
        authed = self._authenticate(params)
        if "__auth_error__" in authed:
            return authed["__auth_error__"]
        principal = authed["__principal__"]
        space_id = authed.get("space_id", "")
        user_id = principal
        display_name = authed.get("display_name", "")
        session_id = authed.get("session_id") or f"sess_{_secrets.token_hex(16)}"
        ws, _ = self._mock_ws_for(session_id)
        result = await hub.join(ws, space_id, user_id, display_name=display_name)
        self._collab_sessions[session_id] = {"space_id": space_id, "user_id": user_id, "ws": ws, "principal": principal}
        result["session_id"] = session_id
        return result

    async def _handle_space_collab_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        hub = self._get_collab_hub()
        session_id = params.get("session_id", "")
        sess = self._collab_sessions.get(session_id)
        if not sess:
            return {"error": "会话不存在, 请先 collab.join"}
        # HI-13: content 限长 + 剥控制字符, 防 LLM 存储型 prompt 注入 / 撑爆记录
        content = params.get("content", "")
        if not isinstance(content, str):
            return {"error": "content 必须为字符串"}
        if len(content) > _MAX_COLLAB_CONTENT:
            return {"error": f"消息超 {_MAX_COLLAB_CONTENT} 字符上限"}
        content = _strip_control_chars(content)
        raw = json.dumps(
            {"type": "chat_send", "content": content, "msg_id": params.get("msg_id", "")},
            ensure_ascii=False,
        )
        return await hub.handle_message(sess["ws"], raw)

    async def _handle_space_collab_cursor(self, params: Dict[str, Any]) -> Dict[str, Any]:
        hub = self._get_collab_hub()
        session_id = params.get("session_id", "")
        sess = self._collab_sessions.get(session_id)
        if not sess:
            return {"error": "会话不存在, 请先 collab.join"}
        raw = json.dumps(
            {
                "type": "cursor_move",
                "x": params.get("x", 0),
                "y": params.get("y", 0),
                "target": params.get("target", ""),
            },
            ensure_ascii=False,
        )
        return await hub.handle_message(sess["ws"], raw)

    async def _handle_space_collab_poll(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session_id = params.get("session_id", "")
        if session_id not in self._collab_queues:
            return {"events": []}
        queue = self._collab_queues[session_id]
        events = []
        while not queue.empty():
            try:
                events.append(json.loads(queue.get_nowait()))
            except (asyncio.QueueEmpty, json.JSONDecodeError):
                break
        return {"events": events}

    async def _handle_space_collab_leave(self, params: Dict[str, Any]) -> Dict[str, Any]:
        hub = self._get_collab_hub()
        session_id = params.get("session_id", "")
        sess = self._collab_sessions.pop(session_id, None)
        self._collab_queues.pop(session_id, None)
        if not sess:
            return {"removed": False}
        await hub.leave(sess["ws"])
        return {"removed": True}

    async def _handle_space_chat_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceChatService, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        content = params.get("content", "")
        agent_id = params.get("agent_id")
        try:
            msg = await chat_svc.send_message(space_id, user_id, content, agent_id=agent_id)
            return msg.to_dict()
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_chat_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceChatService, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        limit = params.get("limit", 50)
        offset = params.get("offset", 0)
        try:
            msgs = await chat_svc.list_messages(space_id, limit=limit, offset=offset)
            return {"messages": [m.to_dict() for m in msgs]}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_chat_context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceChatService, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        limit = params.get("limit", 100)
        try:
            msgs = await chat_svc.get_context(space_id, limit=limit)
            return {"messages": [m.to_dict() for m in msgs]}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_kb_bind(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        perm = SpacePermission(self._space_store)
        kb_svc = SpaceKBService(self._space_store, None, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        kb_id = params.get("kb_id")
        try:
            result = await kb_svc.bind_kb(space_id, operator_id, kb_id=kb_id)
            return {"kb_id": result}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_kb_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        perm = SpacePermission(self._space_store)
        kb_svc = SpaceKBService(self._space_store, None, perm)
        space_id = params.get("space_id", "")
        try:
            return await kb_svc.get_kb_status(space_id)
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_kb_upload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        perm = SpacePermission(self._space_store)
        kb_client = self._get_kb_client()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        file_path = params.get("file_path", "")
        try:
            result = await kb_svc.upload_document(space_id, operator_id, file_path)
            return {"result": result}
        except (PermissionError, FileNotFoundError) as e:
            return {"error": str(e)}

    async def _handle_space_kb_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        perm = SpacePermission(self._space_store)
        kb_client = self._get_kb_client()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        space_id = params.get("space_id", "")
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        try:
            results = await kb_svc.search(space_id, query, top_k=top_k)
            return {"results": results}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_kb_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        perm = SpacePermission(self._space_store)
        kb_client = self._get_kb_client()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        space_id = params.get("space_id", "")
        question = params.get("question", "")
        top_k = params.get("top_k", 5)
        try:
            answer = await kb_svc.query(space_id, question, top_k=top_k)
            return {"answer": answer}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_kb_unbind(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        perm = SpacePermission(self._space_store)
        kb_svc = SpaceKBService(self._space_store, None, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            await kb_svc.unbind_kb(space_id, operator_id)
            return {"status": "unbound"}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_agent_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        try:
            agents = await rt.list_agents(space_id)
            return {"agents": agents, "count": len(agents)}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_agent_add(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        name = params.get("name", "") or params.get("agent_name", "")
        agent_type = params.get("agent_type", "assistant")
        system_prompt = params.get("system_prompt", "")
        enable_rag = params.get("enable_rag", False)
        config = params.get("config", {})
        try:
            result = await rt.add_agent(
                space_id=space_id,
                operator_id=operator_id,
                name=name,
                agent_type=agent_type,
                system_prompt=system_prompt,
                enable_rag=enable_rag,
                config=config,
            )
            return result
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_agent_remove(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            removed = await rt.remove_agent(space_id, agent_id, operator_id)
            return {"agent_id": agent_id, "removed": removed}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_agent_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        message = params.get("message", "")
        model = params.get("model", "")
        try:
            reply = await rt.call_agent(space_id, agent_id, user_id, message, model=model)
            return {"content": reply}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_agent_relay(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceChatService, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        agent_ids = params.get("agent_ids", [])
        message = params.get("message", "")
        model = params.get("model", "")
        try:
            results = await chat_svc.relay_agents(
                space_id,
                user_id,
                agent_ids,
                message,
                model=model,
            )
            return {"results": results}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    # ── 跨产品集成：fusion-projects ──

    async def _handle_project_sync_knowledge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收外部项目知识库文件同步到协同空间。

        params:
            space_id: 目标空间 ID
            files: [{"name": "x.pdf", "content": "<base64>", "folder": "需求文档"}, ...]
            operator_id: 操作者 (default: "project_sync")
        """
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission

        space_id = params.get("space_id", "")
        files = params.get("files", [])
        operator_id = params.get("operator_id", "project_sync")
        if not space_id:
            return {"error": "space_id 必填"}
        if not files:
            return {"error": "files 不能为空"}
        if not isinstance(files, list) or len(files) > _MAX_SYNC_FILES:
            return {"error": f"files 须为列表且不超 {_MAX_SYNC_FILES} 个"}
        perm = SpacePermission(self._space_store)
        kb_client = self._get_kb_client()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        synced = []
        errors = []
        for f in files:
            name = f.get("name", "")
            content_b64 = f.get("content", "")
            folder = f.get("folder", "")
            # HI-15: 文件名净化 (拒路径穿越 ../../etc/cron.d/x)
            safe_name = _secure_filename(name)
            if not safe_name:
                errors.append({"name": name, "error": "文件名非法或缺失"})
                continue
            if not content_b64 or not isinstance(content_b64, str):
                errors.append({"name": safe_name, "error": "content 缺失"})
                continue
            # HI-15: base64 体积上限 (防 10GB base64 解 7.5GB)
            if len(content_b64) > _MAX_B64_BYTES:
                errors.append({"name": safe_name, "error": f"base64 超 {_MAX_B64_BYTES} 字节上限"})
                continue
            # HI-15: 扩展名白名单 (KB 消费文本; 非文本扩展名拒)
            ext = os.path.splitext(safe_name)[1].lower()
            if ext and ext not in _TEXT_EXT_WHITELIST:
                errors.append({"name": safe_name, "error": f"非文本扩展名 {ext} 不受支持"})
                continue
            try:
                import base64

                content_bytes = base64.b64decode(content_b64, validate=True)
                # HI-15: 解码后体积上限
                if len(content_bytes) > _MAX_DECODED_BYTES:
                    errors.append({"name": safe_name, "error": f"解码后超 {_MAX_DECODED_BYTES} 字节上限"})
                    continue
                result = await kb_svc.upload_document(
                    space_id,
                    operator_id,
                    safe_name,
                    content_bytes,
                    folder=folder,
                )
                synced.append({"name": safe_name, "result": result})
            except Exception as e:
                logger.error("syncKnowledge: %s failed: %s", safe_name, e)
                errors.append({"name": safe_name, "error": "上传失败", "trace_id": secrets.token_hex(8)})
        logger.info("desk.project.syncKnowledge space=%s synced=%d errors=%d", space_id, len(synced), len(errors))
        return {"synced": synced, "errors": errors}

    async def _handle_project_import_snapshot(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收会话快照导入到协同空间。

        params:
            space_id: 目标空间 ID
            snapshot: {"title": "...", "messages": [...], "instructionSnapshot": "...", "agentId": "..."}
            operator_id: 操作者 (default: "project_import")
        """
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        import uuid

        from fusion_cowork.space.models import SpaceMessage

        space_id = params.get("space_id", "")
        snapshot = params.get("snapshot", {})
        operator_id = params.get("operator_id", "project_import")
        if not space_id:
            return {"error": "space_id 必填"}
        if not snapshot:
            return {"error": "snapshot 不能为空"}
        space = await self._space_store.get_space(space_id)
        if not space:
            return {"error": f"空间 {space_id} 不存在"}
        messages_data = snapshot.get("messages", [])
        imported = 0
        errors = []
        for msg in messages_data:
            try:
                m = SpaceMessage(
                    id=msg.get("id", f"msg_{uuid.uuid4().hex[:8]}"),
                    space_id=space_id,
                    user_id=msg.get("user_id", operator_id),
                    content=msg.get("content", ""),
                    content_type=msg.get("content_type", "text"),
                    agent_id=msg.get("agent_id", snapshot.get("agentId", "")),
                    parent_msg_id=msg.get("parent_msg_id"),
                    created_at=msg.get("created_at", ""),
                )
                await self._space_store.add_message(m)
                imported += 1
            except Exception as e:
                logger.error(f"importSnapshot: message failed: {e}")
                errors.append({"id": msg.get("id", "?"), "error": str(e)})
        logger.info(f"desk.project.importSnapshot space={space_id} imported={imported} errors={len(errors)}")
        return {"imported": imported, "errors": errors}

    async def _handle_project_export_to_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """导出协同空间内容到 fusion-projects。

        params:
            space_id: 源空间 ID
            items: {"files": true, "chatHistory": true, "artifacts": false}
            target_project_id: 目标项目 ID (记录用)
        """
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        items = params.get("items", {})
        target_project_id = params.get("target_project_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        space = await self._space_store.get_space(space_id)
        if not space:
            return {"error": f"空间 {space_id} 不存在"}
        export_data: Dict[str, Any] = {"space_id": space_id, "space_name": space.name}
        if items.get("chatHistory", True):
            messages = await self._space_store.get_messages(space_id, limit=1000)
            export_data["messages"] = [
                {
                    "id": m.id,
                    "user_id": m.user_id,
                    "content": m.content,
                    "content_type": m.content_type,
                    "agent_id": m.agent_id or "",
                    "created_at": m.created_at,
                }
                for m in messages
            ]
        if items.get("files", True):
            agents = await self._space_store.list_agents(space_id)
            export_data["agents"] = agents
        export_data["target_project_id"] = target_project_id
        logger.info(f"desk.project.exportToProject space={space_id} target={target_project_id}")
        return {"export_data": export_data}

    async def _handle_cowork_trigger(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """ST→project-svc→cowork 中转: 触发协同任务。

        params:
            project_id: 关联项目 ID
            action: 触发动作 (sync_knowledge / import_snapshot / export_to_project)
            payload: 动作参数 (可选)
        """
        project_id = params.get("project_id", "")
        action = params.get("action", "")
        payload = params.get("payload") or params.get("params") or {}
        if not project_id:
            return {"error": "project_id 必填"}
        if not action:
            return {"error": "action 必填"}
        import uuid

        task_id = f"cowork_{uuid.uuid4().hex[:8]}"
        action_map = {
            "sync_knowledge": "desk.project.syncKnowledge",
            "import_snapshot": "desk.project.importSnapshot",
            "export_to_project": "desk.project.exportToProject",
        }
        target_method = action_map.get(action)
        result: Dict[str, Any] = {"task_id": task_id, "project_id": project_id, "action": action, "status": "triggered"}
        if target_method and self._space_store:
            try:
                handler = self._handlers.get(target_method)
                if handler:
                    # A-5: 经 _dispatch 的认证+IDOR 守卫 — 原 _handle_cowork_trigger 直调
                    # handler 绕过 _authenticate (无 __principal__) + _check_space_access,
                    # 且 call_params 无 __principal__ → 目标 handler 取 params 身份字段被冒充。
                    call_params = {**payload, "project_id": project_id}
                    authed = self._authenticate(call_params)
                    if isinstance(authed, dict) and "__auth_error__" in authed:
                        result["status"] = "auth_failed"
                        result["error"] = authed["__auth_error__"].get("message", "认证失败")
                        logger.warning(f"cowork.trigger auth failed project={project_id}")
                        return result
                    space_err = await self._check_space_access(target_method, authed)
                    if space_err is not None:
                        result["status"] = "forbidden"
                        result["error"] = space_err.get("error", "空间访问被拒")
                        logger.warning(f"cowork.trigger IDOR 拒 project={project_id} method={target_method}")
                        return result
                    action_result = await handler(authed)
                    result["action_result"] = action_result
                    result["status"] = "completed"
                else:
                    result["status"] = "no_handler"
                    result["error"] = f"handler {target_method} not found"
            except Exception as e:
                logger.error(f"cowork.trigger action={action} project={project_id} failed: {e}")
                result["status"] = "failed"
                result["error"] = str(e)
        else:
            if not target_method:
                result["status"] = "unknown_action"
                result["error"] = f"action {action} not in {list(action_map.keys())}"
            elif not self._space_store:
                result["status"] = "no_space_store"
                result["error"] = "SpaceStore 未配置"
        logger.info(f"cowork.trigger project={project_id} action={action} task={task_id} status={result['status']}")
        return result

    async def _handle_cowork_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """ST→project-svc→cowork 中转: 查询协同任务状态。

        params:
            task_id: 任务 ID
        """
        task_id = params.get("task_id", "")
        if not task_id:
            return {"error": "task_id 必填"}
        logger.info(f"cowork.status task={task_id}")
        return {"task_id": task_id, "status": "completed"}

    # ── Artifact 权限 Handlers ──

    def _get_artifact_svc(self):
        from ..space.artifact import SpaceArtifactService
        from ..space.permission import SpacePermission

        if not self._space_store:
            return None, "space_store 未初始化"
        perm = SpacePermission(self._space_store)
        return SpaceArtifactService(self._space_store, perm), None

    async def _handle_space_artifact_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "") or params.get("owner_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.create_artifact(
                space_id=space_id,
                owner_user_id=user_id,
                name=params.get("name", ""),
                artifact_type=params.get("artifact_type", "document"),
                content=params.get("content", ""),
                metadata=params.get("metadata"),
            )
            return {"artifact": result}
        except PermissionError as e:
            return self._internal_error(e)

    async def _handle_space_artifact_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            art = await svc.get_artifact(space_id, artifact_id, user_id)
            if not art:
                return {"error": f"Artifact {artifact_id} 不存在"}
            return {"artifact": art}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_artifact_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.update_artifact(
                space_id=space_id,
                artifact_id=artifact_id,
                user_id=user_id,
                content=params.get("content", ""),
                name=params.get("name", ""),
                metadata=params.get("metadata"),
            )
            return {"artifact": result}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_artifact_share(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.share_artifact(space_id, artifact_id, user_id)
            return result
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_artifact_transfer(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        from_user = params.get("__principal__", "local_user")
        to_user = params.get("to_user_id", "")
        if not all([space_id, artifact_id, from_user, to_user]):
            return {"error": "space_id, artifact_id, to_user_id 必填"}
        try:
            result = await svc.transfer_ownership(space_id, artifact_id, from_user, to_user)
            return result
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_artifact_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            artifacts = await svc.list_artifacts(
                space_id,
                user_id,
                params.get("artifact_type", ""),
            )
            return {"artifacts": artifacts}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_artifact_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        removed = await svc.delete_artifact(space_id, artifact_id, user_id)
        return {"deleted": removed}

    # ── 侧边栏模块 Handlers ──

    def _get_module_registry(self):
        from ..space.fsb import ModuleRegistry

        if not self._space_store:
            return None, "space_store 未初始化"
        return ModuleRegistry(self._space_store), None

    async def _handle_module_register(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        module_id = params.get("id", "")
        name = params.get("name", "")
        if not module_id or not name:
            return {"error": "id 和 name 必填"}
        result = await reg.register_module(
            module_id=module_id,
            name=name,
            icon=params.get("icon", ""),
            route_path=params.get("route_path", ""),
            enabled=params.get("enabled", True),
            metadata=params.get("metadata"),
        )
        return {"module": result}

    async def _handle_module_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        modules = await reg.list_modules(enabled_only=params.get("enabled_only", False))
        return {"modules": modules}

    async def _handle_module_enable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        module_id = params.get("id", "")
        if not module_id:
            return {"error": "id 必填"}
        await reg.enable_module(module_id)
        return {"enabled": True}

    async def _handle_module_disable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        module_id = params.get("id", "")
        if not module_id:
            return {"error": "id 必填"}
        await reg.disable_module(module_id)
        return {"disabled": True}

    # ── 通知推送 Handlers ──

    def _get_notification_svc(self):
        from ..space.fsb import NotificationService

        if not self._space_store:
            return None, "space_store 未初始化"
        return NotificationService(self._space_store), None

    async def _handle_notification_push(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_notification_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "") or ""
        user_id = params.get("user_id", "") or "local_user"
        title = params.get("title", "")
        if not title:
            return {"error": "title 必填"}
        result = await svc.push_notification(
            space_id=space_id,
            user_id=user_id,
            notification_type=params.get("type", "approval"),
            title=title,
            content=params.get("content", ""),
            metadata=params.get("metadata"),
        )
        return {"notification": result}

    async def _handle_notification_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_notification_svc()
        if err:
            return {"error": err}
        user_id = params.get("user_id", "") or "local_user"
        notifications = await svc.list_notifications(
            user_id,
            unread_only=params.get("unread_only", False),
        )
        return {"notifications": notifications}

    async def _handle_notification_mark_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_notification_svc()
        if err:
            return {"error": err}
        notif_id = params.get("id", "") or params.get("notification_id", "")
        if not notif_id:
            return {"error": "id 或 notification_id 必填"}
        await svc.mark_read(notif_id)
        return {"read": True}

    # ── Agent Update ──

    async def _handle_space_agent_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        if not space_id or not agent_id:
            return {"error": "space_id 和 agent_id 必填"}
        update_fields = {}
        for key in ("name", "system_prompt", "enable_rag", "config", "agent_type"):
            if key in params:
                update_fields[key] = params[key]
        model = params.get("model")
        if model:
            config = update_fields.get("config", {})
            if isinstance(config, str):
                import json as _json

                try:
                    config = _json.loads(config)
                except Exception:
                    config = {}
            config["model"] = model
            update_fields["config"] = config
        permission = params.get("permission")
        if permission:
            config = update_fields.get("config", {})
            if isinstance(config, str):
                import json as _json

                try:
                    config = _json.loads(config)
                except Exception:
                    config = {}
            config["permission"] = permission
            update_fields["config"] = config
        capabilities = params.get("capabilities")
        if capabilities:
            config = update_fields.get("config", {})
            if isinstance(config, str):
                import json as _json

                try:
                    config = _json.loads(config)
                except Exception:
                    config = {}
            config["capabilities"] = capabilities
            update_fields["config"] = config
        if not update_fields:
            return {"error": "无更新字段"}
        try:
            ok = await self._space_store.update_agent(space_id, agent_id, **update_fields)
            return {"agent_id": agent_id, "updated": ok}
        except Exception as e:
            logger.error(f"agent.update failed: {e}")
            return self._internal_error(e)

    # ── Snapshot ──

    async def _handle_space_snapshot_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        try:
            snapshots = await self._space_store.list_snapshots(space_id)
            return {"snapshots": [s.to_dict() for s in snapshots]}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_snapshot_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space.models import SpaceSnapshot

        space_id = params.get("space_id", "")
        name = params.get("name", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            msgs = await self._space_store.get_messages(space_id, limit=1000)
            agents = await self._space_store.list_agents(space_id)
            snapshot = SpaceSnapshot(
                space_id=space_id,
                name=name or f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                messages_count=len(msgs),
                agents_count=len(agents),
                snapshot_data={
                    "messages": [m.to_dict() for m in msgs],
                    "agents": agents,
                },
                created_by=operator_id,
            )
            result = await self._space_store.create_snapshot(snapshot)
            return result.to_dict()
        except Exception as e:
            logger.error(f"snapshot.create failed: {e}")
            return self._internal_error(e)

    async def _handle_space_snapshot_clone(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        snapshot_id = params.get("snapshot_id", "")
        new_name = params.get("name", "")
        operator_id = params.get("operator_id", "")
        try:
            src = await self._space_store.get_snapshot(space_id, snapshot_id)
            if not src:
                return {"error": f"快照 {snapshot_id} 不存在"}
            if not operator_id:
                sp = await self._space_store.get_space(space_id)
                operator_id = sp.owner_id if sp else "local_user"
            from fusion_cowork.space.models import Space, SpaceMessage

            clone_space = Space(
                name=new_name or f"{src.name} - 克隆空间",
                description=f"由快照 {snapshot_id} 克隆",
                owner_id=operator_id,
            )
            clone_space = await self._space_store.create_space(clone_space)
            new_space_id = clone_space.id
            logger.info(f"snapshot.clone 新建空间: {new_space_id} (源快照={snapshot_id})")

            restored_msgs = 0
            for msg_data in src.snapshot_data.get("messages", []):
                m = SpaceMessage.from_dict(msg_data)
                m.id = ""
                m.space_id = new_space_id
                await self._space_store.add_message(m)
                restored_msgs += 1

            restored_agents = 0
            for agent_data in src.snapshot_data.get("agents", []):
                agent_data = dict(agent_data)
                agent_data.pop("id", None)
                agent_data.pop("created_at", None)
                agent_data["space_id"] = new_space_id
                agent_data["created_by"] = operator_id
                await self._space_store.add_agent(agent_data)
                restored_agents += 1

            return {
                "cloned": True,
                "new_space_id": new_space_id,
                "source_snapshot_id": snapshot_id,
                "messages_restored": restored_msgs,
                "agents_restored": restored_agents,
            }
        except Exception as e:
            logger.error(f"snapshot.clone failed: {e}")
            return self._internal_error(e)

    async def _handle_space_snapshot_restore(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        snapshot_id = params.get("snapshot_id", "")
        try:
            snap = await self._space_store.get_snapshot(space_id, snapshot_id)
            if not snap:
                return {"error": f"快照 {snapshot_id} 不存在"}
            restored = 0
            for msg_data in snap.snapshot_data.get("messages", []):
                from fusion_cowork.space.models import SpaceMessage

                m = SpaceMessage.from_dict(msg_data)
                m.id = ""  # let store generate new id
                m.space_id = space_id
                await self._space_store.add_message(m)
                restored += 1
            logger.info(f"snapshot.restore space={space_id} msgs={restored}")
            return {"restored": restored, "snapshot_id": snapshot_id}
        except Exception as e:
            logger.error(f"snapshot.restore failed: {e}")
            return self._internal_error(e)

    async def _handle_space_snapshot_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        snapshot_id = params.get("snapshot_id", "")
        try:
            ok = await self._space_store.delete_snapshot(space_id, snapshot_id)
            return {"deleted": ok, "snapshot_id": snapshot_id}
        except Exception as e:
            return self._internal_error(e)

    # ── Chat Stream ──

    async def _handle_space_chat_stream(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceChatService, SpacePermission

        perm = SpacePermission(self._space_store)
        mlx = self._get_mlx_client()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        content = params.get("content", "")
        agent_id = params.get("agent_id", "")
        model = params.get("model", "")
        try:
            chunks = []
            async for chunk in chat_svc.stream_message(
                space_id,
                user_id,
                content,
                agent_id,
                model=model,
            ):
                chunks.append(chunk)
            full = "".join(chunks)
            return {"content": full, "chunk_count": len(chunks)}
        except PermissionError as e:
            return {"error": str(e)}

    # ── Comments ──

    async def _handle_space_comment_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        message_id = params.get("message_id", "")
        author_id = params.get("author_id", "") or "local_user"
        author_name = params.get("author_name", "") or author_id
        content = params.get("content", "")
        space_id = params.get("space_id", "")
        thread_id = params.get("thread_id", "")
        if not message_id or not content:
            return {"error": "message_id 和 content 必填"}
        try:
            comment_id = await self._space_store.add_comment(
                message_id,
                author_id,
                author_name,
                content,
                space_id=space_id,
                thread_id=thread_id,
            )
            return {"comment_id": comment_id}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_space_comment_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        message_id = params.get("message_id", "")
        try:
            comments = await self._space_store.list_comments(message_id)
            return {"comments": comments}
        except Exception as e:
            return self._internal_error(e)

    # ── Space Workflow ──

    async def _handle_space_workflow_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        user_id = params.get("operator_id", "") or params.get("user_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            artifacts = await svc.list_artifacts(space_id, user_id, "workflow")
            return {"workflows": artifacts}
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"workflow.list failed: {e}")
            return self._internal_error(e)

    async def _handle_space_workflow_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        name = params.get("name", "")
        nodes = params.get("nodes", [])
        edges = params.get("edges", [])
        operator_id = params.get("operator_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.create_artifact(
                space_id=space_id,
                owner_user_id=operator_id,
                name=name,
                artifact_type="workflow",
                content=json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False),
            )
            return {"artifact_id": result.get("id", ""), "name": name}
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"workflow.create failed: {e}")
            return self._internal_error(e)

    async def _handle_space_workflow_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "") or params.get("workflow_id", "")
        operator_id = params.get("operator_id", "") or params.get("user_id", "")
        inputs = params.get("inputs", {})
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            artifact = await svc.get_artifact(space_id, artifact_id, operator_id)
            if not artifact:
                return {"error": f"工作流 {artifact_id} 不存在"}
            wf_data = json.loads(artifact.get("content", "{}"))
            wf_data.setdefault("name", artifact.get("name", "workflow"))
            from ..engine.workflow import Workflow

            wf = Workflow.from_dict(wf_data)
            engine = self._get_engine()
            result = await engine.execute(wf, initial_input=inputs)
            status_val = getattr(result, "status", "completed")
            status_str = status_val.value if hasattr(status_val, "value") else str(status_val)
            return {
                "execution_id": getattr(result, "id", ""),
                "status": status_str,
                "error": getattr(result, "error", None),
                "result_summary": getattr(result, "result_summary", ""),
                "steps": [s.to_dict() for s in getattr(result, "steps", []) if hasattr(s, "to_dict")],
            }
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"workflow.run failed: {e}")
            return self._internal_error(e)

    # ── Discovery ──

    async def _handle_space_discovery_scan(self, params: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("discovery.scan: Bonjour scan not yet implemented")
        return {"peers": [], "message": "Bonjour scan not yet available"}

    # ── Desktop Sharing ──

    async def _handle_space_desktop_share(self, params: Dict[str, Any]) -> Dict[str, Any]:
        space_id = params.get("space_id", "")
        enable = params.get("enable", True)
        role = params.get("role", "presenter")
        logger.info(f"desktop.share space={space_id} enable={enable} role={role}")
        return {"status": "not_implemented", "message": "Desktop sharing not yet available"}

    async def _handle_space_desktop_control(self, params: Dict[str, Any]) -> Dict[str, Any]:
        space_id = params.get("space_id", "")
        action = params.get("action", "")
        logger.info(f"desktop.control space={space_id} action={action}")
        return {"status": "not_implemented", "message": "Desktop control not yet available"}

    # ── Session Snapshots ──

    async def _handle_session_snapshot_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        session_id = params.get("session_id", "")
        try:
            snapshots = await self._space_store.list_snapshots(space_id)
            session_snaps = [s for s in snapshots if s.snapshot_data.get("session_id") == session_id]
            return {"snapshots": [s.to_dict() for s in session_snaps]}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_session_snapshot_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space.models import SpaceSnapshot

        space_id = params.get("space_id", "")
        session_id = params.get("session_id", "")
        label = params.get("label", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            snapshot = SpaceSnapshot(
                space_id=space_id,
                name=label or f"session_snap_{session_id[:8]}",
                snapshot_data={"session_id": session_id, "steps": []},
                created_by=operator_id,
            )
            result = await self._space_store.create_snapshot(snapshot)
            return result.to_dict()
        except Exception as e:
            logger.error(f"session.snapshot_create failed: {e}")
            return self._internal_error(e)

    async def _handle_session_snapshot_restore(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        session_id = params.get("session_id", "")
        snapshot_id = params.get("snapshot_id", "")
        try:
            snap = await self._space_store.get_snapshot(space_id, snapshot_id)
            if not snap:
                return {"error": f"快照 {snapshot_id} 不存在"}
            return {"restored": True, "snapshot_id": snapshot_id, "session_id": session_id}
        except Exception as e:
            return self._internal_error(e)

    async def _handle_session_snapshot_fork(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        snapshot_id = params.get("snapshot_id", "")
        try:
            src = await self._space_store.get_snapshot(space_id, snapshot_id)
            if not src:
                return {"error": f"快照 {snapshot_id} 不存在"}
            from fusion_cowork.space.models import SpaceSnapshot

            fork = SpaceSnapshot(
                space_id=space_id,
                name=f"{src.name} (fork)",
                snapshot_data=src.snapshot_data,
                created_by=params.get("operator_id", "local_user"),
            )
            result = await self._space_store.create_snapshot(fork)
            return result.to_dict()
        except Exception as e:
            logger.error(f"session.snapshot_fork failed: {e}")
            return self._internal_error(e)

    async def _handle_session_snapshot_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        snapshot_id = params.get("snapshot_id", "")
        try:
            ok = await self._space_store.delete_snapshot(space_id, snapshot_id)
            return {"deleted": ok, "snapshot_id": snapshot_id}
        except Exception as e:
            return self._internal_error(e)
