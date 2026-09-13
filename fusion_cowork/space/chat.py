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
        # v4 方案②: incremental watermark for task_step reads — relay context
        # assembly previously re-scanned the whole trajectory pool on every
        # group, a latency that grew linearly with history. Same pattern as
        # the v2 retrospective after_ts optimization.
        self._step_watermark: float = 0.0

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
        try:
            async for chunk in self._mlx.stream_chat(
                model=model,
                messages=messages,
            ):
                full_response.append(chunk)
                await self._emit(space_id, "stream", {"chunk": chunk})
                yield chunk
        except Exception as e:
            # v2 方案三 (P1-4): a mid-stream model failure previously dropped the
            # partial response silently — no persistence, no error event, the
            # SSE feed just died and the agent's "answer" was untraceable.
            logger.error(f"stream_message 模型流中断: {e}", exc_info=True)
            await self._emit(space_id, "error", {"agent_id": agent_id, "error": str(e), "partial": len(full_response)})
            partial = "".join(full_response)
            if partial:
                interrupted = SpaceMessage(
                    space_id=space_id,
                    user_id="",
                    agent_id=agent_id,
                    content=partial + "\n[响应中断]",
                    role="assistant",
                )
                await self._store.add_message(interrupted)
                await self._emit(space_id, "message_complete", interrupted.to_dict())
            raise

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

    @staticmethod
    def _trim_context(messages: List[dict], max_chars: int = 24000) -> List[dict]:
        """P1-4 (audit 0912): keep the most recent messages within a character
        budget — long sessions previously stuffed 100 full messages into the
        prompt and blew the model context window. System prompts are always
        preserved; a marker notes elided history."""
        if sum(len(m.get("content", "")) for m in messages) <= max_chars:
            return messages
        system_head: List[dict] = []
        body = messages
        if messages and messages[0].get("role") == "system":
            system_head = [messages[0]]
            body = messages[1:]
        kept: List[dict] = []
        used = 0
        for m in reversed(body):
            c = len(m.get("content", ""))
            if used + c > max_chars and kept:
                break
            kept.append(m)
            used += c
        kept.reverse()
        elided = len(body) - len(kept)
        if elided > 0:
            kept.insert(0, {"role": "system", "content": f"[{elided} earlier messages omitted to fit context budget]"})
        return system_head + kept

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
        # P1-4 (audit 0912): apply the context budget before sending to the model
        return self._trim_context(messages)

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
        # agent_ids entries may be plain ids or nested lists (parallel groups,
        # P1-5) — count real agents, not top-level slots, so a single parallel
        # group like [["a", "b"]] is valid fan-out topology.
        total_agents = sum(len(g) if isinstance(g, list) else 1 for g in agent_ids)
        if total_agents < 2:
            raise ValueError("relay_agents requires at least 2 agents")
        user_msg = SpaceMessage(
            space_id=space_id,
            user_id=user_id,
            content=initial_message,
        )
        user_msg = await self._store.add_message(user_msg)
        await self._emit(space_id, "message", user_msg.to_dict())

        # P1-3 (audit 0912): relay used to break on the first agent failure,
        # discarding completed steps. Now optional agents (agent_def.config
        # optional=true) are skipped on failure and the chain continues; the
        # summary honestly reports partial completion.
        # P1-5 (audit 0912): agent_ids may contain nested lists — agents in
        # one group run in parallel (asyncio.gather), groups run sequentially,
        # and the merged group output feeds the next stage.
        # P1-4 (audit 0912): the conversation context is built once per stage
        # and trimmed to a token budget instead of re-fetching 100 full
        # messages for every agent.
        results: List[dict] = []
        current_message = initial_message
        had_failure = False
        for group in agent_ids:
            group_ids = group if isinstance(group, list) else [group]
            if not group_ids:
                continue

            async def _run_one(aid: str, stage_message: str, context: List[SpaceMessage]) -> dict:
                try:
                    agent_def = await self._store.get_agent_def(space_id, aid)
                    if not agent_def:
                        return {"agent_id": aid, "error": "not found"}
                    # v2 P2: context is fetched ONCE per group and passed in —
                    # each agent previously re-read 100 messages from the store
                    # (duplicated IO per relay member, contradicting the
                    # once-per-stage note below).
                    messages = self._build_agent_messages(agent_def, context)
                    if self._kb_svc and agent_def.get("enable_rag"):
                        try:
                            rag_results = await self._kb_svc.search(space_id, stage_message, top_k=5)
                            messages = self._inject_rag(messages, rag_results)
                        except Exception as e:
                            logger.warning(f"RAG search failed for relay agent {aid}: {e}")
                    agent_model = model or self._get_config_model(agent_def)
                    if not agent_model:
                        models = await self._mlx.list_models()
                        agent_model = models[0]["id"] if models else "default"
                    resp = await self._mlx.chat(model=agent_model, messages=messages)
                    reply = resp.content
                    # 方案二 (audit v3): persist the intermediate output so the
                    # collaboration's working history survives restarts.
                    try:
                        from ..orchestrator.trajectory_writer import write_task_step

                        write_task_step(space_id, aid, "reply", reply)
                    except Exception:
                        pass
                    assistant_msg = SpaceMessage(
                        space_id=space_id,
                        user_id="",
                        agent_id=aid,
                        content=reply,
                        role="assistant",
                    )
                    assistant_msg = await self._store.add_message(assistant_msg)
                    await self._emit(space_id, "message", assistant_msg.to_dict())
                    return {"agent_id": aid, "content": reply}
                except Exception as e:
                    # P1-3: per-agent failure is recorded, not raised — a single
                    # agent crash previously propagated and killed the stage.
                    logger.error(f"relay_agents: agent {aid} failed: {e}")
                    # 方案二 (audit v3): failures are context too — the next
                    # stage must see what broke, not just an absent reply.
                    try:
                        from ..orchestrator.trajectory_writer import write_task_step

                        write_task_step(space_id, aid, "error", str(e))
                    except Exception:
                        pass
                    return {"agent_id": aid, "error": str(e)}

            # v2 P2: fetch the group's context once, share across members
            group_context = await self._store.get_messages(space_id, limit=100)
            # 方案二 (audit v3): pull recent task_step events (tool outputs,
            # per-agent failures from earlier stages) into the shared context —
            # they never go through the chat table, so a restart wiped the
            # collaboration's working history before this.
            try:
                from ..orchestrator.trajectory_writer import read_task_steps

                # v4 方案②: after_ts watermark — only events newer than the
                # last read are scanned; cold files are skipped by mtime.
                steps = read_task_steps(space_id, limit=10, after_ts=self._step_watermark)
                if steps:
                    self._step_watermark = max(self._step_watermark, max(s["ts"] for s in steps))
                if steps:
                    step_msg = SpaceMessage(
                        space_id=space_id,
                        user_id="",
                        agent_id="",
                        content="[近期任务中间产出]\n"
                        + "\n".join(f"- ({s['agent_id']}/{s['step']}) {s['content'][:300]}" for s in steps),
                        role="assistant",
                    )
                    group_context = group_context + [step_msg]
            except Exception as e:
                logger.debug(f"read_task_steps for relay context failed (best-effort): {e}")

            if len(group_ids) == 1:
                group_results = [await _run_one(group_ids[0], current_message, group_context)]
            else:
                group_results = list(
                    await asyncio.gather(
                        *[_run_one(aid, current_message, group_context) for aid in group_ids], return_exceptions=True
                    )
                )
                group_results = [r if isinstance(r, dict) else {"error": str(r)} for r in group_results]

            ok_outputs = []
            for aid, r in zip(group_ids, group_results):
                if isinstance(r, dict) and r.get("error"):
                    results.append(r)
                    had_failure = True
                    await self._emit(space_id, "error", {"agent_id": aid, "error": r["error"]})
                    logger.error(f"relay_agents: agent {aid} failed: {r['error']}")
                else:
                    results.append(r)
                    ok_outputs.append(r.get("content", ""))
            if not ok_outputs:
                # whole group failed: keep already-completed steps, stop chain
                break
            current_message = "\n\n".join(ok_outputs) if len(ok_outputs) > 1 else ok_outputs[0]

        await self._emit(
            space_id,
            "relay_complete",
            {
                "agent_ids": agent_ids,
                "steps": len(results),
                "partial": had_failure,
            },
        )
        logger.info(
            f"SpaceChat.relay_agents space={space_id} agents={agent_ids} steps={len(results)} partial={had_failure}"
        )
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
