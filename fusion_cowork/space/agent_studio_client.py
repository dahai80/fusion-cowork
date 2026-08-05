"""AgentStudioClient - HTTP client to consume published Agent definitions.

Connects to Agent Studio API (default http://localhost:8765) to load
published agent definitions and import them into collaboration spaces.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx

if TYPE_CHECKING:
    from .agent_runtime import SpaceAgentRuntime

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:8765"
_REQUEST_TIMEOUT = 30.0


class AgentStudioClient:
    """HTTP client for Agent Studio published agent definitions."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _REQUEST_TIMEOUT,
        max_retries: int = 2,
        retry_delay: float = 1.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as e:
                last_exc = e
                if attempt < self._max_retries:
                    logger.warning(f"AgentStudio {method} {path} attempt {attempt + 1} failed: {e}")
                    import asyncio

                    await asyncio.sleep(self._retry_delay)
                else:
                    logger.error(f"AgentStudio {method} {path} failed after {attempt + 1} attempts")
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 502, 503, 504) and attempt < self._max_retries:
                    last_exc = e
                    logger.warning(f"AgentStudio HTTP {e.response.status_code}, retrying")
                    import asyncio

                    await asyncio.sleep(self._retry_delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    async def list_published_agents(self) -> List[Dict[str, Any]]:
        resp = await self._request("GET", "/api/agents")
        data = resp.json()
        agents = data if isinstance(data, list) else data.get("agents", data.get("items", []))
        logger.info(f"AgentStudioClient.list_published_agents count={len(agents)}")
        return agents

    async def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = await self._request("GET", f"/api/agents/{agent_id}")
            agent = resp.json()
            logger.info(f"AgentStudioClient.get_agent id={agent_id}")
            return agent
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info(f"AgentStudioClient.get_agent id={agent_id} not_found")
                return None
            raise

    async def import_agent_to_space(
        self,
        agent_id: str,
        agent_runtime: SpaceAgentRuntime,
        space_id: str,
        operator_id: str,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        agent_def = await self.get_agent(agent_id)
        if not agent_def:
            raise ValueError(f"Agent {agent_id} not found in Agent Studio")
        name = agent_def.get("name", agent_id)
        system_prompt = agent_def.get("system_prompt", "")
        agent_type = agent_def.get("agent_type", "assistant")
        enable_rag = agent_def.get("enable_rag", False)
        config = agent_def.get("config", {})
        if overrides:
            name = overrides.get("name", name)
            system_prompt = overrides.get("system_prompt", system_prompt)
            agent_type = overrides.get("agent_type", agent_type)
            enable_rag = overrides.get("enable_rag", enable_rag)
            config.update(overrides.get("config", {}))
        result = await agent_runtime.add_agent(
            space_id=space_id,
            operator_id=operator_id,
            name=name,
            agent_type=agent_type,
            system_prompt=system_prompt,
            enable_rag=enable_rag,
            config=config,
        )
        logger.info(f"AgentStudioClient.import_agent_to_space agent={agent_id} space={space_id}")
        return result

    async def health_check(self) -> bool:
        try:
            resp = await self._request("GET", "/api/health")
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"AgentStudioClient.health_check failed: {e}")
            return False
