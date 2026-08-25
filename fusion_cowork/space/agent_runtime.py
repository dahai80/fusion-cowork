"""SpaceAgentRuntime - space-level Agent runtime with isolation and relay.

Wraps orchestrator Agent + runtime with space_id isolation.
Supports: add/remove/list agents, call agent, chain agents (relay).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fusion_cowork.tenant import resolve_tenant_id

from ..ai.mlx_client import FusionMLXClient
from .permission import SpacePermission
from .store import SpaceStore

logger = logging.getLogger(__name__)


class SpaceAgentRuntime:
    """Space-level Agent runtime with multi-agent relay support."""

    def __init__(
        self,
        store: SpaceStore,
        mlx_client: FusionMLXClient,
        permission: SpacePermission,
    ):
        self._store = store
        self._mlx = mlx_client
        self._perm = permission
        self._running_runtimes: Dict[str, Any] = {}

    async def add_agent(
        self,
        space_id: str,
        operator_id: str,
        name: str,
        agent_type: str = "assistant",
        system_prompt: str = "",
        enable_rag: bool = False,
        config: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not await self._perm.check(space_id, operator_id, "manage_agents"):
            raise PermissionError(f"User {operator_id} cannot manage agents in space {space_id}")
        agent_data = {
            "id": agent_id or f"agent_{uuid.uuid4().hex[:8]}",
            "space_id": space_id,
            "name": name,
            "agent_type": agent_type,
            "system_prompt": system_prompt,
            "enable_rag": enable_rag,
            "config": config or {},
            "created_by": operator_id,
        }
        aid = await self._store.add_agent(agent_data)
        agent_data["id"] = aid
        logger.info(f"SpaceAgentRuntime.add_agent space={space_id} agent={aid}")
        return agent_data

    async def remove_agent(
        self,
        space_id: str,
        agent_id: str,
        operator_id: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        if not await self._perm.check(space_id, operator_id, "manage_agents"):
            raise PermissionError(f"User {operator_id} cannot manage agents in space {space_id}")
        tid = resolve_tenant_id(tenant_id)
        # A-8: 经 store 串行写事务, 加 tenant_id 守卫防跨租户删 agent。
        async with self._store.write_tx(tid) as h:
            res = await h.exec(
                "DELETE FROM space_agents WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (agent_id, space_id, tid),
            )
            removed = res.rowcount > 0
        if removed:
            rt = self._running_runtimes.pop(agent_id, None)
            if rt and hasattr(rt, "stop"):
                await rt.stop()
        logger.info(f"SpaceAgentRuntime.remove_agent agent={agent_id} removed={removed} tenant={tid}")
        return removed

    async def list_agents(self, space_id: str) -> List[Dict[str, Any]]:
        return await self._store.list_agents(space_id)

    async def get_agent(self, space_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        return await self._store.get_agent_def(space_id, agent_id)

    async def call_agent(
        self,
        space_id: str,
        agent_id: str,
        user_id: str,
        message: str,
        model: str = "",
    ) -> str:
        if not await self._perm.check(space_id, user_id, "call_agent"):
            raise PermissionError(f"User {user_id} cannot call agents in space {space_id}")
        agent_def = await self._store.get_agent_def(space_id, agent_id)
        if not agent_def:
            raise ValueError(f"Agent {agent_id} not found in space {space_id}")
        messages = self._build_messages(agent_def, message)
        if not model:
            config = agent_def.get("config", {})
            if isinstance(config, str):
                import json

                try:
                    config = json.loads(config)
                except (json.JSONDecodeError, TypeError):
                    config = {}
            model = config.get("model", "")
        if not model:
            models = await self._mlx.list_models()
            model = models[0]["id"] if models else "default"
        resp = await self._mlx.chat(model=model, messages=messages)
        logger.info(f"SpaceAgentRuntime.call_agent agent={agent_id} len={len(resp.content)}")
        return resp.content

    async def chain_agents(
        self,
        space_id: str,
        agent_ids: List[str],
        user_id: str,
        initial_message: str,
        model: str = "",
    ) -> List[Dict[str, Any]]:
        if not await self._perm.check(space_id, user_id, "call_agent"):
            raise PermissionError(f"User {user_id} cannot call agents in space {space_id}")
        if len(agent_ids) < 2:
            raise ValueError("chain_agents requires at least 2 agents")
        results = []
        current_message = initial_message
        for agent_id in agent_ids:
            agent_def = await self._store.get_agent_def(space_id, agent_id)
            if not agent_def:
                logger.warning(f"chain_agents: agent {agent_id} not found, skipping")
                results.append({"agent_id": agent_id, "error": "not found"})
                continue
            try:
                reply = await self.call_agent(space_id, agent_id, user_id, current_message, model=model)
                results.append({"agent_id": agent_id, "content": reply})
                current_message = reply
            except Exception as e:
                logger.error(f"chain_agents: agent {agent_id} failed: {e}")
                results.append({"agent_id": agent_id, "error": str(e)})
                break
        logger.info(f"SpaceAgentRuntime.chain_agents space={space_id} agents={agent_ids} steps={len(results)}")
        return results

    def _build_messages(self, agent_def: Dict[str, Any], user_message: str) -> List[dict]:
        messages = []
        system_prompt = agent_def.get("system_prompt", "")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})
        return messages

    async def register_to_orchestrator(
        self,
        space_id: str,
        orchestrator: Any,
    ) -> int:
        from ..orchestrator.orchestrator import Agent, AgentRole

        agents = await self.list_agents(space_id)
        registered = 0
        for agent_def in agents:
            agent_id = agent_def.get("id", "")
            if agent_id in orchestrator._agents:
                continue
            role_map = {
                "assistant": AgentRole.EXECUTOR,
                "planner": AgentRole.PLANNER,
                "analyzer": AgentRole.ANALYZER,
                "validator": AgentRole.VALIDATOR,
                "coordinator": AgentRole.COORDINATOR,
            }
            agent_type = agent_def.get("agent_type", "assistant")
            role = role_map.get(agent_type, AgentRole.EXECUTOR)
            agent = Agent(
                agent_id=f"space:{space_id}:{agent_id}",
                name=agent_def.get("name", ""),
                role=role,
                description=agent_def.get("system_prompt", "")[:200],
                capabilities=[agent_type],
            )
            orchestrator.register_agent(agent)
            registered += 1
        logger.info(f"SpaceAgentRuntime.register_to_orchestrator space={space_id} registered={registered}")
        return registered
