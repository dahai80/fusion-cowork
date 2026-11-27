"""FusionMLX HTTP client - sole interface between Fusion-Cowork and fusion-mlx.

All AI inference requests go through this client to fusion-mlx OpenAI-compatible API.
No direct MLX or mlx-lm imports; HTTP only.

Default: http://localhost:11432/v1 (fusion-gateway netlayer port, per netlayer-compliance-plan Plan B)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ..auth.identity_propagation import friendly_quota_error, identity_auth_headers
from ..observability.trace import trace_propagation_headers

logger = logging.getLogger(__name__)

DEFAULT_MLX_PORT = 11432
# env override for local single-machine direct-mlx (bypass gateway netlayer), issue #83.
# Priority: FUSION_MLX_URL (full URL) > FUSION_MLX_HOST + FUSION_MLX_PORT > default localhost:11432.
# Default unchanged -> zero impact on multi-node/gateway deployments; local single machine can
# export FUSION_MLX_HOST=127.0.0.1 FUSION_MLX_PORT=11434 to reach mlx directly.
_env_mlx_url = os.environ.get("FUSION_MLX_URL", "").strip()
if _env_mlx_url:
    DEFAULT_MLX_BASE_URL = _env_mlx_url
else:
    _env_mlx_host = os.environ.get("FUSION_MLX_HOST", "localhost").strip() or "localhost"
    _env_mlx_port = os.environ.get("FUSION_MLX_PORT", str(DEFAULT_MLX_PORT)).strip() or str(DEFAULT_MLX_PORT)
    DEFAULT_MLX_BASE_URL = f"http://{_env_mlx_host}:{_env_mlx_port}/v1"
MAX_RETRIES = 2
RETRY_DELAY = 1.0

# fusion-core infrastructure adoption (path B, issue #58): borrow with_retry+connection pool,
# keep the in-house client business layer. Use the in-tree package when locally installed
# (unified retry + metrics reporting + LRU pool); fall back to the original hand-written retry
# when CI lacks fusion-core (zero functional degradation).
_HAS_FUSION_CORE = False
_with_retry = None
_get_async_client = None
try:
    from fusion_core.http_client import get_async_client as _get_async_client
    from fusion_core.http_client import with_retry as _with_retry

    _HAS_FUSION_CORE = True
    logger.info("fusion_core.http_client available, FusionMLXClient uses with_retry+connection pool (path B)")
except ImportError:
    logger.info("fusion_core unavailable, FusionMLXClient falls back to hand-written retry (path B fallback)")


def _safe_json(resp: httpx.Response) -> Any:
    # issue #288: best-effort body decode for quota-shape detection — 429 bodies
    # may be JSON or plain text; decode failure must never mask the status.
    try:
        return resp.json()
    except Exception:
        return resp.text


def _client_api_key() -> str:
    # issue #204: single-point config resolution via fusion_core.config.client_key (canonical
    # name FUSION_CLIENT_KEY, legacy alias chain + prod missing RuntimeError all converge inside
    # fusion-core). Bare install without fusion-core falls back to the canonical name only —
    # alias compatibility is fusion-core's job, not duplicated in this repo.
    try:
        from fusion_core.config import client_key

        return client_key()
    except ImportError:
        key = os.environ.get("FUSION_CLIENT_KEY", "")
        if not key:
            logger.warning(
                "FUSION_CLIENT_KEY not set: MLX calls will be unauthenticated and may be rejected by upstream 401 (export FUSION_CLIENT_KEY=<key>)"
            )
        return key


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
            api_key = _client_api_key()
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client: httpx.AsyncClient | None = None
        # Stage 6: upstream circuit breaker (enabled when env FUSION_CB_MLX_THRESHOLD is set, otherwise direct call).
        self._breaker = None
        cb_threshold = os.environ.get("FUSION_CB_MLX_THRESHOLD", "").strip()
        if cb_threshold:
            try:
                from fusion_cowork.security.circuit_breaker import CircuitBreaker

                rec = os.environ.get("FUSION_CB_MLX_RECOVERY", "30").strip()
                self._breaker = CircuitBreaker(
                    name="mlx", failure_threshold=int(cb_threshold), recovery_timeout=float(rec)
                )
                logger.info(f"MLX circuit breaker enabled threshold={cb_threshold} recovery={rec}s")
            except (ValueError, ImportError):
                logger.warning("FUSION_CB_MLX_THRESHOLD malformed or security missing, MLX circuit breaker not enabled")

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
        # Path B: fusion-core connection pool (per-loop+base_url LRU, cap 8, auto eviction).
        # fallback: in-house single-instance client (back compatible; used when CI lacks fusion-core).
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

        # Stage 6: breaker open -> reject call (caller catches CircuitOpenError and degrades, no retry, no downstream hang).
        if self._breaker is not None and not self._breaker.is_call_allowed():
            from fusion_cowork.security.circuit_breaker import CircuitOpenError

            raise CircuitOpenError("circuit[mlx] open")

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                # issue #288: per-request identity header — gateway meter keys the
                # quota by the caller's identity JWT `sub` when present.
                resp = await self.client.post(
                    "/chat/completions",
                    json=payload,
                    headers={**identity_auth_headers(), **trace_propagation_headers()} or None,
                )
                resp.raise_for_status()
                data = resp.json()
                # E-12: missing/empty choices -> defensive; old code data["choices"][0] raised bare IndexError
                choices = data.get("choices") or []
                if not choices:
                    logger.error(f"chat() response has no choices (model={model}, keys={list(data.keys())})")
                    raise RuntimeError(f"模型响应无 choices: {data.get('error', data)}")  # i18n: user-facing
                choice = choices[0]
                message = choice.get("message", {})
                if self._breaker is not None:
                    self._breaker.on_success()
                return self._build_llm_response(message, choice, data, model)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as e:
                last_exc = e
                if attempt < self.max_retries:
                    logger.warning(f"chat() attempt {attempt + 1} failed: {e}, retrying in {self.retry_delay}s")
                    await asyncio.sleep(self.retry_delay)
                else:
                    logger.error(f"chat() failed after {attempt + 1} attempts: {e}")
            except httpx.HTTPStatusError as e:
                # issue #288: quota 429 never retries (daily budget won't recover
                # in-session) — surface the friendly bilingual message instead.
                quota_msg = friendly_quota_error(e.response.status_code, _safe_json(e.response))
                if quota_msg:
                    raise RuntimeError(quota_msg) from e
                if e.response.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                    last_exc = e
                    logger.warning(f"chat() HTTP {e.response.status_code}, retrying in {self.retry_delay}s")
                    await asyncio.sleep(self.retry_delay)
                else:
                    if e.response.status_code in (401, 403):
                        logger.error(
                            f"chat() auth failed HTTP {e.response.status_code}: "
                            "FUSION_CLIENT_KEY should be the fusion-gateway client api key "
                            "(auth.api_keys[].key in config.yaml), not the fusion-mlx "
                            "auth.api_key in settings.json; the two are independent auth layers"
                        )
                    raise
        # Stage 6: retries exhausted -> record breaker failure (5xx/timeout class; 401/403 auth
        # errors are not recorded — upstream is not broken, the key is wrong).
        if self._breaker is not None and isinstance(
            last_exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout)
        ):
            self._breaker.on_failure()
        # E-12: last_exc may be None (max_retries<0 zero iterations, or a future branch missed
        # setting it) -> raising None would throw TypeError
        raise last_exc if last_exc is not None else RuntimeError("请求无有效响应且无异常记录")  # i18n: user-facing

    async def _chat_core(self, payload: dict, model: str) -> LLMResponse:
        # Path B: fusion-core with_retry unified retry (429/500/502/503/504 + transient
        # exceptions, jittered backoff) + metrics reporting (aggregatable at gateway /metrics).
        # The 401/403 auth hint is preserved.
        client = self._core_client()

        async def _do_post() -> httpx.Response:
            # issue #288: per-request identity header — gateway meter keys the
            # quota by the caller's identity JWT `sub` when present.
            return await client.post(
                "/chat/completions",
                json=payload,
                headers={**identity_auth_headers(), **trace_propagation_headers()} or None,
            )

        try:
            resp = await _with_retry(_do_post, retries=self.max_retries, initial_backoff=self.retry_delay)
        except httpx.HTTPStatusError as e:
            # issue #288: quota 429 → friendly bilingual message (fusion-core
            # with_retry already exhausted its own 429 retries at this point).
            quota_msg = friendly_quota_error(e.response.status_code, _safe_json(e.response))
            if quota_msg:
                raise RuntimeError(quota_msg) from e
            if e.response.status_code in (401, 403):
                logger.error(
                    f"chat() auth failed HTTP {e.response.status_code}: "
                    "FUSION_CLIENT_KEY should be the fusion-gateway client api key "
                    "(auth.api_keys[].key in config.yaml), not the fusion-mlx "
                    "auth.api_key in settings.json; the two are independent auth layers"
                )
            # Stage 6: 5xx records breaker failure (upstream broken), 4xx auth errors do not (wrong key, not broken upstream).
            if self._breaker is not None and e.response.status_code >= 500:
                self._breaker.on_failure()
            raise
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout):
            if self._breaker is not None:
                self._breaker.on_failure()
            raise
        data = resp.json()
        # E-12: missing/empty choices -> defensive
        choices = data.get("choices") or []
        if not choices:
            logger.error(f"chat() response has no choices (model={model}, keys={list(data.keys())})")
            raise RuntimeError(f"模型响应无 choices: {data.get('error', data)}")  # i18n: user-facing
        choice = choices[0]
        message = choice.get("message", {})
        if self._breaker is not None:
            self._breaker.on_success()
        return self._build_llm_response(message, choice, data, model)

    def _build_llm_response(self, message: dict, choice: dict, data: dict, model: str) -> LLMResponse:
        # D-H3 silent-failure guard: empty content on the success path -> explicit marker
        # (no raise, preserves caller field-check semantics, zero changes for 18 callers).
        content = message.get("content", "")
        finish_reason = choice.get("finish_reason", "stop")
        if not content.strip() and not message.get("tool_calls"):
            logger.warning(f"chat() model returned empty content (model={model}, finish_reason={finish_reason})")
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
                async with self.client.stream(
                    "POST",
                    "/chat/completions",
                    json=payload,
                    headers={**identity_auth_headers(), **trace_propagation_headers()} or None,
                ) as resp:
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
                # issue #288: quota 429 → friendly message, no retry (same as chat()).
                quota_msg = friendly_quota_error(e.response.status_code, _safe_json(e.response))
                if quota_msg:
                    raise RuntimeError(quota_msg) from e
                if e.response.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                    last_exc = e
                    logger.warning(f"stream_chat() HTTP {e.response.status_code}, retrying")
                    await asyncio.sleep(self.retry_delay)
                else:
                    raise
        # E-12: last_exc may be None (max_retries<0 zero iterations, or a future branch missed
        # setting it) -> raising None would throw TypeError
        raise last_exc if last_exc is not None else RuntimeError("请求无有效响应且无异常记录")  # i18n: user-facing

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
        # Stage 6: KB upstream circuit breaker (enabled when env FUSION_CB_KB_THRESHOLD is set).
        self._breaker = None
        cb_threshold = os.environ.get("FUSION_CB_KB_THRESHOLD", "").strip()
        if cb_threshold:
            try:
                from fusion_cowork.security.circuit_breaker import CircuitBreaker

                rec = os.environ.get("FUSION_CB_KB_RECOVERY", "30").strip()
                self._breaker = CircuitBreaker(
                    name="kb", failure_threshold=int(cb_threshold), recovery_timeout=float(rec)
                )
                logger.info(f"KB circuit breaker enabled threshold={cb_threshold} recovery={rec}s")
            except (ValueError, ImportError):
                logger.warning("FUSION_CB_KB_THRESHOLD malformed or security missing, KB circuit breaker not enabled")

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
        # Stage 6: breaker open -> reject call (caller degrades, no downstream hang).
        if self._breaker is not None and not self._breaker.is_call_allowed():
            from fusion_cowork.security.circuit_breaker import CircuitOpenError

            raise CircuitOpenError("circuit[kb] open")
        payload = {"question": question, "top_k": top_k}
        try:
            resp = await self.client.post(f"/kb/bases/{kb_id}/ask", json=payload)
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout):
            if self._breaker is not None:
                self._breaker.on_failure()
            raise
        except httpx.HTTPStatusError as e:
            if self._breaker is not None and e.response.status_code >= 500:
                self._breaker.on_failure()
            raise
        if self._breaker is not None:
            self._breaker.on_success()
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
