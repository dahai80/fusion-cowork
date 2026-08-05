"""协作空间对话服务 — 共享上下文 + Agent 回复 + 流式推理。

业务逻辑:
- send_message: 发送消息 + 事件广播 + 触发 Agent 回复
- stream_message: 流式推理 + 实时推送
- _agent_respond: Agent 上下文构建 + RAG 注入 + 推理回复
- get_context: 获取空间共享对话上下文
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, AsyncIterator, List, Optional

from ..ai.mlx_client import FusionMLXClient
from ..engine.events import EventEmitter, WorkflowEvent
from .models import SpaceMessage
from .permission import SpacePermission
from .store import SpaceStore

if TYPE_CHECKING:
    from .knowledge import SpaceKBService

logger = logging.getLogger(__name__)


class SpaceChatService:
    """空间对话服务 — 共享对话上下文 + Agent 流式推理。"""

    def __init__(
        self,
        store: SpaceStore,
        mlx_client: FusionMLXClient,
        permission: SpacePermission,
        event_emitter: Optional[EventEmitter] = None,
        kb_service: Optional[SpaceKBService] = None,
    ):
        self._store = store
        self._mlx = mlx_client
        self._perm = permission
        self._events = event_emitter or EventEmitter()
        self._kb_svc = kb_service

    def _get_config_model(self, agent_def: dict) -> str:
        config = agent_def.get("config", {})
        if isinstance(config, str):
            import json as _json

            try:
                config = _json.loads(config)
            except (_json.JSONDecodeError, TypeError):
                config = {}
        return config.get("model", "")

    async def send_message(
        self,
        space_id: str,
        user_id: str,
        content: str,
        agent_id: Optional[str] = None,
        attachments: Optional[list] = None,
        content_type: str = "text",
        parent_msg_id: Optional[str] = None,
    ) -> SpaceMessage:
        if not await self._perm.check(space_id, user_id, "send_message"):
            raise PermissionError(f"用户 {user_id} 无权在空间 {space_id} 发送消息")
        msg = SpaceMessage(
            space_id=space_id,
            user_id=user_id,
            content=content,
            content_type=content_type,
            attachments=attachments or [],
            parent_msg_id=parent_msg_id,
        )
        msg = await self._store.add_message(msg)
        await self._emit(space_id, "message", msg.to_dict())
        logger.info(f"SpaceChat.send_message space={space_id} user={user_id} msg={msg.id}")
        if agent_id:
            asyncio.create_task(self._agent_respond(space_id, agent_id, msg))
        return msg

    async def stream_message(
        self,
        space_id: str,
        user_id: str,
        content: str,
        agent_id: str,
        model: str = "",
        attachments: Optional[list] = None,
    ) -> AsyncIterator[str]:
        if not await self._perm.check(space_id, user_id, "send_message"):
            raise PermissionError(f"用户 {user_id} 无权在空间 {space_id} 发送消息")
        user_msg = SpaceMessage(
            space_id=space_id,
            user_id=user_id,
            content=content,
            attachments=attachments or [],
        )
        user_msg = await self._store.add_message(user_msg)
        await self._emit(space_id, "message", user_msg.to_dict())

        context = await self._store.get_messages(space_id, limit=100)
        messages = self._build_messages(context)

        agent_def = await self._store.get_agent_def(space_id, agent_id)
        if agent_def:
            system_prompt = agent_def.get("system_prompt", "")
            if system_prompt:
                messages = [{"role": "system", "content": system_prompt}] + messages
            if self._kb_svc and agent_def.get("enable_rag"):
                try:
                    rag_results = await self._kb_svc.search(space_id, content, top_k=5)
                    messages = self._inject_rag(messages, rag_results)
                except Exception as e:
                    logger.warning(f"RAG search failed: {e}")
            if not model:
                model = self._get_config_model(agent_def)

        if not model:
            models = await self._mlx.list_models()
            model = models[0]["id"] if models else "default"

        full_response = []
        async for chunk in self._mlx.stream_chat(
            model=model,
            messages=messages,
        ):
            full_response.append(chunk)
            await self._emit(space_id, "stream", {"chunk": chunk})
            yield chunk

        complete = "".join(full_response)
        assistant_msg = SpaceMessage(
            space_id=space_id,
            user_id="",
            agent_id=agent_id,
            content=complete,
            role="assistant",
        )
        assistant_msg = await self._store.add_message(assistant_msg)
        await self._emit(space_id, "message_complete", assistant_msg.to_dict())
        logger.info(f"SpaceChat.stream_message space={space_id} agent={agent_id} len={len(complete)}")

    async def _agent_respond(
        self,
        space_id: str,
        agent_id: str,
        trigger_msg: SpaceMessage,
    ) -> None:
        try:
            context = await self._store.get_messages(space_id, limit=100)
            agent_def = await self._store.get_agent_def(space_id, agent_id)
            if not agent_def:
                logger.warning(f"Agent {agent_id} not found in space {space_id}")
                return

            messages = self._build_agent_messages(agent_def, context)

            if self._kb_svc and agent_def.get("enable_rag"):
                try:
                    rag_results = await self._kb_svc.search(space_id, trigger_msg.content, top_k=5)
                    messages = self._inject_rag(messages, rag_results)
                except Exception as e:
                    logger.warning(f"RAG search failed: {e}")

            model = self._get_config_model(agent_def)
            if not model:
                models = await self._mlx.list_models()
                model = models[0]["id"] if models else "default"

            full_response = []
            async for chunk in self._mlx.stream_chat(model=model, messages=messages):
                full_response.append(chunk)
                await self._emit(space_id, "stream", {"chunk": chunk})

            complete = "".join(full_response)
            assistant_msg = SpaceMessage(
                space_id=space_id,
                user_id="",
                agent_id=agent_id,
                content=complete,
                role="assistant",
            )
            assistant_msg = await self._store.add_message(assistant_msg)
            await self._emit(space_id, "message_complete", assistant_msg.to_dict())
            logger.info(f"SpaceChat._agent_respond space={space_id} agent={agent_id}")
        except Exception as e:
            logger.error(f"Agent respond failed: {e}", exc_info=True)
            await self._emit(space_id, "error", {"agent_id": agent_id, "error": str(e)})

    def _build_messages(self, context: List[SpaceMessage]) -> List[dict]:
        messages = []
        for msg in context:
            role = msg.role if msg.role in ("user", "assistant", "system") else "user"
            messages.append({"role": role, "content": msg.content})
        return messages

    def _build_agent_messages(
        self,
        agent_def: dict,
        context: List[SpaceMessage],
    ) -> List[dict]:
        messages = []
        system_prompt = agent_def.get("system_prompt", "")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for msg in context:
            role = msg.role if msg.role in ("user", "assistant", "system") else "user"
            messages.append({"role": role, "content": msg.content})
        return messages

    def _inject_rag(
        self,
        messages: List[dict],
        rag_results: List[dict],
    ) -> List[dict]:
        if not rag_results:
            return messages
        rag_context = "\n".join(f"[{i + 1}] {r.get('content', '')}" for i, r in enumerate(rag_results))
        rag_msg = {
            "role": "system",
            "content": f"以下是相关参考资料，请基于这些内容回答用户问题:\n\n{rag_context}",
        }
        if messages and messages[0].get("role") == "system":
            return [messages[0], rag_msg] + messages[1:]
        return [rag_msg] + messages

    async def get_context(
        self,
        space_id: str,
        limit: int = 100,
    ) -> List[SpaceMessage]:
        return await self._store.get_messages(space_id, limit=limit)

    async def relay_agents(
        self,
        space_id: str,
        user_id: str,
        agent_ids: List[str],
        initial_message: str,
        model: str = "",
    ) -> List[dict]:
        if not await self._perm.check(space_id, user_id, "call_agent"):
            raise PermissionError(f"User {user_id} cannot call agents in space {space_id}")
        if len(agent_ids) < 2:
            raise ValueError("relay_agents requires at least 2 agents")
        user_msg = SpaceMessage(
            space_id=space_id,
            user_id=user_id,
            content=initial_message,
        )
        user_msg = await self._store.add_message(user_msg)
        await self._emit(space_id, "message", user_msg.to_dict())

        results = []
        current_message = initial_message
        for agent_id in agent_ids:
            agent_def = await self._store.get_agent_def(space_id, agent_id)
            if not agent_def:
                logger.warning(f"relay_agents: agent {agent_id} not found, skipping")
                results.append({"agent_id": agent_id, "error": "not found"})
                continue
            try:
                context = await self._store.get_messages(space_id, limit=100)
                messages = self._build_agent_messages(agent_def, context)
                if self._kb_svc and agent_def.get("enable_rag"):
                    try:
                        rag_results = await self._kb_svc.search(space_id, current_message, top_k=5)
                        messages = self._inject_rag(messages, rag_results)
                    except Exception as e:
                        logger.warning(f"RAG search failed for relay agent {agent_id}: {e}")
                agent_model = model or self._get_config_model(agent_def)
                if not agent_model:
                    models = await self._mlx.list_models()
                    agent_model = models[0]["id"] if models else "default"
                resp = await self._mlx.chat(model=agent_model, messages=messages)
                reply = resp.content
                assistant_msg = SpaceMessage(
                    space_id=space_id,
                    user_id="",
                    agent_id=agent_id,
                    content=reply,
                    role="assistant",
                )
                assistant_msg = await self._store.add_message(assistant_msg)
                await self._emit(space_id, "message", assistant_msg.to_dict())
                results.append({"agent_id": agent_id, "content": reply})
                current_message = reply
                logger.info(f"relay_agents step: agent={agent_id} len={len(reply)}")
            except Exception as e:
                logger.error(f"relay_agents: agent {agent_id} failed: {e}")
                results.append({"agent_id": agent_id, "error": str(e)})
                await self._emit(space_id, "error", {"agent_id": agent_id, "error": str(e)})
                break

        await self._emit(
            space_id,
            "relay_complete",
            {
                "agent_ids": agent_ids,
                "steps": len(results),
            },
        )
        logger.info(f"SpaceChat.relay_agents space={space_id} agents={agent_ids} steps={len(results)}")
        return results

    async def list_messages(
        self,
        space_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[SpaceMessage]:
        return await self._store.get_messages(space_id, limit=limit, offset=offset)

    async def _emit(self, space_id: str, event_name: str, data: dict) -> None:
        event = WorkflowEvent(
            event_type=f"space:{space_id}:{event_name}",
            data=data,
        )
        self._events.emit(event)
