"""FusionMLX HTTP 客户端 — Fusion-Cowork 与 fusion-mlx 的唯一接口。

所有 AI 推理请求都通过此客户端发送到 fusion-mlx 的 OpenAI 兼容 API。
不直接导入任何 MLX 或 mlx-lm 代码，仅通过 HTTP 通信。

调用地址: http://localhost:8000/v1 (fusion-mlx 默认端口)
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """LLM 调用的结构化响应。"""
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })


@dataclass
class EmbeddingResponse:
    """Embedding 调用的响应。"""
    vector: list[float]
    model: str = ""
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0})


class FusionMLXClient:
    """fusion-mlx HTTP 客户端。

    通过 OpenAI 兼容 API 调用 fusion-mlx 的本地推理能力。
    支持：
    - 文本生成 (chat)
    - 流式输出 (stream_chat)
    - 模型列表
    - 健康检查
    - 服务统计
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "local",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
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

    async def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None

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
        """调用 fusion-mlx 的 /v1/chat/completions 端点。

        Args:
            model: 模型名称 (如 "qwen3.5-9b", "deepseek-v3-24b")
            messages: 对话消息列表 (OpenAI 格式)
            tools: 可选的工具定义 (function calling)
            temperature: 采样温度
            max_tokens: 最大生成 token 数
            stream: 是否启用流式输出
            **kwargs: 额外参数

        Returns:
            LLMResponse: 包含 content、tool_calls、usage
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        resp = await self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        message = choice.get("message", {})

        return LLMResponse(
            content=message.get("content", ""),
            tool_calls=message.get("tool_calls", []),
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
        )

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """流式调用 chat completions。"""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
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

    async def embed(
        self,
        text: str,
        model: str = "BGE-M3",
    ) -> EmbeddingResponse:
        """调用 fusion-mlx 的 /v1/embeddings 端点生成文本向量。

        Args:
            text: 输入文本
            model: 嵌入模型名称

        Returns:
            EmbeddingResponse: 包含向量和模型信息
        """
        payload = {
            "model": model,
            "input": text,
        }
        resp = await self.client.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()

        embedding_data = data["data"][0]
        return EmbeddingResponse(
            vector=embedding_data["embedding"],
            model=data.get("model", model),
            usage=data.get("usage", {}),
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """列出 fusion-mlx 中可用的模型。"""
        resp = await self.client.get("/models")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    async def health(self) -> bool:
        """检查 fusion-mlx 是否健康可访问。"""
        try:
            resp = await self.client.get("/models", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def get_server_stats(self) -> dict[str, Any]:
        """获取 fusion-mlx 服务统计信息。"""
        try:
            resp = await self.client.get("/stats", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}


class KBClient:
    """Fusion-KB HTTP 客户端 — 知识库语义检索。

    通过 HTTP 调用 fusion-kb 的 FastAPI 服务。
    调用地址: http://localhost:11434 (fusion-kb 默认端口)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
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

    async def list_bases(self) -> list[dict[str, Any]]:
        """列出所有知识库。"""
        resp = await self.client.get("/kb/bases")
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """在知识库中语义搜索。

        Args:
            kb_id: 知识库 ID
            query: 搜索查询
            top_k: 返回结果数量

        Returns:
            搜索结果列表，每项包含 content、score、metadata
        """
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
        """RAG 查询 — 基于知识库内容回答问题。

        Args:
            kb_id: 知识库 ID
            question: 用户问题
            top_k: 检索相关文档数

        Returns:
            str: 回答内容
        """
        payload = {"question": question, "top_k": top_k}
        resp = await self.client.post(f"/kb/bases/{kb_id}/query", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("answer", "")

    async def health(self) -> bool:
        """检查 fusion-kb 是否健康。"""
        try:
            resp = await self.client.get("/kb/bases", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False