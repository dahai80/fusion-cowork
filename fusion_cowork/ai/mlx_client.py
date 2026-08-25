"""FusionMLX HTTP client - sole interface between Fusion-Cowork and fusion-mlx.

All AI inference requests go through this client to fusion-mlx OpenAI-compatible API.
No direct MLX or mlx-lm imports; HTTP only.

Default: http://localhost:11432/v1 (fusion-gateway netlayer port, per netlayer-compliance-plan §方案B)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MLX_PORT = 11432
DEFAULT_MLX_BASE_URL = f"http://localhost:{DEFAULT_MLX_PORT}/v1"
MAX_RETRIES = 2
RETRY_DELAY = 1.0

# fusion-core 基础设施采纳 (路径B, issue #58): 借 with_retry+连接池, 保自建客户端业务层。
# in-tree 包本地装了就用 (重试统一+指标上报+LRU池), CI 未装则 fallback 原手写重试 (零功能退化)。
_HAS_FUSION_CORE = False
_with_retry = None
_get_async_client = None
try:
    from fusion_core.http_client import get_async_client as _get_async_client
    from fusion_core.http_client import with_retry as _with_retry

    _HAS_FUSION_CORE = True
    logger.info("fusion_core.http_client available, FusionMLXClient 借 with_retry+连接池 (路径B)")
except ImportError:
    logger.info("fusion_core 不可用, FusionMLXClient 回退手写重试 (path B fallback)")


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    )


@dataclass
class EmbeddingResponse:
    vector: list[float]
    model: str = ""
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0})


class FusionMLXClient:
    """fusion-mlx HTTP client with retry and stream robustness."""

    def __init__(
        self,
        base_url: str = DEFAULT_MLX_BASE_URL,
        api_key: str = "",
        timeout: float = 120.0,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY,
    ):
        self.base_url = base_url.rstrip("/")
        if not api_key:
            api_key = os.environ.get("FUSION_MLX_API_KEY", "")
        self.api_key = api_key
        if not self.api_key:
            logger.warning(
                "FUSION_MLX_API_KEY 未设置: MLX 调用将无鉴权, 可能被上游 401 拒绝 (请 export FUSION_MLX_API_KEY=<key>)"
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    def _core_client(self) -> httpx.AsyncClient:
        # 路径B: fusion-core 连接池 (per-loop+base_url LRU, 上限8, 自动驱逐).
        # fallback: 自建单实例 client (保向后兼容, CI 无 fusion-core 时走此).
        if _HAS_FUSION_CORE:
            return _get_async_client(
                self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self.client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> FusionMLXClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        """Call /v1/chat/completions with retry on transient errors."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        if _HAS_FUSION_CORE:
            return await self._chat_core(payload, model)

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                # E-12: choices 缺失/空 → 防御, 旧版 data["choices"][0] 直接 IndexError
                choices = data.get("choices") or []
                if not choices:
                    logger.error(f"chat() 响应无 choices (model={model}, keys={list(data.keys())})")
                    raise RuntimeError(f"模型响应无 choices: {data.get('error', data)}")
                choice = choices[0]
                message = choice.get("message", {})
                return self._build_llm_response(message, choice, data, model)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as e:
                last_exc = e
                if attempt < self.max_retries:
                    logger.warning(f"chat() attempt {attempt + 1} failed: {e}, retrying in {self.retry_delay}s")
                    await asyncio.sleep(self.retry_delay)
                else:
                    logger.error(f"chat() failed after {attempt + 1} attempts: {e}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                    last_exc = e
                    logger.warning(f"chat() HTTP {e.response.status_code}, retrying in {self.retry_delay}s")
                    await asyncio.sleep(self.retry_delay)
                else:
                    if e.response.status_code in (401, 403):
                        logger.error(
                            f"chat() 鉴权失败 HTTP {e.response.status_code}: "
                            "FUSION_MLX_API_KEY 应为 fusion-gateway 的 client api key "
                            "(config.yaml 中 auth.api_keys[].key), 而非 fusion-mlx "
                            "settings.json 的 auth.api_key; 两者是独立鉴权"
                        )
                    raise
        raise last_exc

    async def _chat_core(self, payload: dict, model: str) -> LLMResponse:
        # 路径B: fusion-core with_retry 统一重试 (429/500/502/503/504 + 瞬态异常, jitter退避)
        # + 指标上报 (gateway /metrics 可聚合). 鉴权401/403 hint 保留.
        client = self._core_client()

        async def _do_post() -> httpx.Response:
            return await client.post("/chat/completions", json=payload)

        try:
            resp = await _with_retry(_do_post, retries=self.max_retries, initial_backoff=self.retry_delay)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.error(
                    f"chat() 鉴权失败 HTTP {e.response.status_code}: "
                    "FUSION_MLX_API_KEY 应为 fusion-gateway 的 client api key "
                    "(config.yaml 中 auth.api_keys[].key), 而非 fusion-mlx "
                    "settings.json 的 auth.api_key; 两者是独立鉴权"
                )
            raise
        data = resp.json()
        # E-12: choices 缺失/空 → 防御
        choices = data.get("choices") or []
        if not choices:
            logger.error(f"chat() 响应无 choices (model={model}, keys={list(data.keys())})")
            raise RuntimeError(f"模型响应无 choices: {data.get('error', data)}")
        choice = choices[0]
        message = choice.get("message", {})
        return self._build_llm_response(message, choice, data, model)

    def _build_llm_response(self, message: dict, choice: dict, data: dict, model: str) -> LLMResponse:
        # D-H3 静默失败守卫: 成功路径 content 空 → 显式标记 (非抛错, 保调用方字段判定语义, 18调用方零改).
        content = message.get("content", "")
        finish_reason = choice.get("finish_reason", "stop")
        if not content.strip() and not message.get("tool_calls"):
            logger.warning(f"chat() 模型返回空 content (model={model}, finish_reason={finish_reason})")
            finish_reason = "empty_content"
        return LLMResponse(
            content=content,
            tool_calls=message.get("tool_calls", []),
            finish_reason=finish_reason,
            usage=data.get("usage", {}),
        )

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Stream chat completions with connection retry and error recovery."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            chunk = line[6:]
                            if chunk.strip() == "[DONE]":
                                break
                            try:
                                data = json.loads(chunk)
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as e:
                last_exc = e
                if attempt < self.max_retries:
                    logger.warning(f"stream_chat() attempt {attempt + 1} failed: {e}, retrying")
                    await asyncio.sleep(self.retry_delay)
                else:
                    logger.error(f"stream_chat() failed after {attempt + 1} attempts: {e}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                    last_exc = e
                    logger.warning(f"stream_chat() HTTP {e.response.status_code}, retrying")
                    await asyncio.sleep(self.retry_delay)
                else:
                    raise
        raise last_exc

    async def embed(
        self,
        text: str,
        model: str = "BGE-M3",
    ) -> EmbeddingResponse:
        """Call /v1/embeddings."""
        payload = {
            "model": model,
            "input": text,
        }
        resp = await self._core_client().post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()

        embedding_data = data["data"][0]
        return EmbeddingResponse(
            vector=embedding_data["embedding"],
            model=data.get("model", model),
            usage=data.get("usage", {}),
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models from fusion-mlx."""
        resp = await self._core_client().get("/models")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    async def health(self) -> bool:
        """Check if fusion-mlx is reachable."""
        try:
            resp = await self.client.get("/models", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def get_server_stats(self) -> dict[str, Any]:
        """Get fusion-mlx server stats."""
        try:
            resp = await self.client.get("/stats", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}


class KBClient:
    """Fusion-RAG HTTP client for knowledge base operations.

    Default: http://localhost:11436 (fusion-rag default port)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11436",
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> KBClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def list_bases(self) -> list[dict[str, Any]]:
        resp = await self.client.get("/kb/bases")
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        payload = {"query": query, "top_k": top_k}
        resp = await self.client.post(f"/kb/bases/{kb_id}/search", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def query(
        self,
        kb_id: str,
        question: str,
        top_k: int = 5,
    ) -> str:
        payload = {"question": question, "top_k": top_k}
        resp = await self.client.post(f"/kb/bases/{kb_id}/ask", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("answer", "")

    async def create_kb(self, name: str, description: str = "") -> str:
        resp = await self.client.post("/kb/bases", json={"name": name, "description": description})
        resp.raise_for_status()
        data = resp.json()
        kb_id = data.get("id") or data.get("kb_id") or data.get("name", name)
        return kb_id

    async def delete_kb(self, kb_id: str) -> bool:
        resp = await self.client.delete(f"/kb/bases/{kb_id}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def upload_file(
        self,
        kb_id: str,
        file_path: str,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        import os as _os

        name = file_name or _os.path.basename(file_path)
        with open(file_path, "rb") as f:
            resp = await self.client.post(
                f"/kb/bases/{kb_id}/documents",
                files={"file": (name, f)},
                data={"name": name},
            )
        resp.raise_for_status()
        return resp.json()

    async def list_documents(self, kb_id: str) -> list[dict[str, Any]]:
        resp = await self.client.get(f"/kb/bases/{kb_id}/documents")
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> bool:
        try:
            resp = await self.client.get("/kb/bases", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False
