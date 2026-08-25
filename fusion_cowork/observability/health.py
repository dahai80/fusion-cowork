"""深度健康检查 (v0.4.0 Stage 4).

替静态 {"status":"ok"} — 实查 DB SELECT 1 / 磁盘 / 上游 MLX/KB 可达 (短超时)。
desk_rpc + mcp_http /health 复用。

run_health(store=None, upstreams=None) -> {status, checks:{db,disk,mlx,kb}}
status: ok (全通) | degraded (部分挂) | down (核心挂)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HealthCheck:
    """单检查项 — 返 {ok:bool, detail:str}。"""

    @staticmethod
    async def check_db(store: Any) -> Dict[str, Any]:
        if store is None:
            return {"ok": True, "detail": "no store (本地模式)"}
        try:
            backend = getattr(store, "backend", "sqlite")
            if backend == "postgres":
                # asyncpg pool: 试 SELECT 1
                async def _sel1():
                    if hasattr(store, "_pool") and store._pool:
                        async with store._pool.acquire() as conn:
                            return await conn.fetchval("SELECT 1")

                await asyncio.wait_for(_sel1(), timeout=3)
            else:
                # sqlite: 试读 schema_migrations 或任意表存在性
                handle = getattr(store, "_ensure_db", None)
                if handle:
                    await handle()
            return {"ok": True, "detail": "db reachable"}
        except Exception as e:
            logger.warning(f"health check_db 失败: {e}")
            return {"ok": False, "detail": f"db error: {type(e).__name__}"}

    @staticmethod
    def check_disk(threshold_pct: int = 90) -> Dict[str, Any]:
        try:
            usage = shutil.disk_usage("/")
            pct = int(usage.used / usage.total * 100)
            return {"ok": pct < threshold_pct, "detail": f"disk {pct}% used", "pct": pct}
        except Exception as e:
            return {"ok": False, "detail": f"disk check error: {type(e).__name__}"}

    @staticmethod
    async def check_upstream(url: str, timeout: float = 2.0) -> Dict[str, Any]:
        if not url:
            return {"ok": True, "detail": "not configured"}
        try:
            # 简单 TCP 探活 (不依赖 httpx, 免引入)
            from urllib.parse import urlparse

            p = urlparse(url)
            host = p.hostname or "localhost"
            port = p.port or (443 if p.scheme == "https" else 80)
            try:
                _, reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
                writer.close()
                await writer.wait_closed()
            except (socket.gaierror, ConnectionRefusedError, TimeoutError) as e:
                return {"ok": False, "detail": f"unreachable: {type(e).__name__}"}
            return {"ok": True, "detail": f"{host}:{port} reachable"}
        except Exception as e:
            return {"ok": False, "detail": f"upstream error: {type(e).__name__}"}

    @staticmethod
    async def run_all(
        store: Any = None,
        upstreams: Optional[Dict[str, str]] = None,
        disk_threshold: int = 90,
    ) -> Dict[str, Any]:
        checks: Dict[str, Any] = {}
        checks["db"] = await HealthCheck.check_db(store)
        checks["disk"] = HealthCheck.check_disk(disk_threshold)
        upstreams = upstreams or {}
        if "mlx" not in upstreams:
            upstreams["mlx"] = "http://localhost:11434"
        for name, url in upstreams.items():
            checks[name] = await HealthCheck.check_upstream(url)

        oks = [c.get("ok", False) for c in checks.values()]
        if all(oks):
            status = "ok"
        elif any(oks):
            status = "degraded"
        else:
            status = "down"
        logger.debug(f"health run_all: status={status} checks={checks}")
        return {"status": status, "checks": checks}


async def run_health(store: Any = None, upstreams: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return await HealthCheck.run_all(store=store, upstreams=upstreams)
