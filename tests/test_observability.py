"""可观测性测试 (v0.4.0 Stage 4)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fusion_cowork.observability.health import HealthCheck
from fusion_cowork.observability.metrics import metrics_middleware, record_db_error
from fusion_cowork.observability.trace import (
    get_trace_id,
    new_trace_id,
    set_trace_id,
    trace_context,
)
from fusion_cowork.utils.logger import setup_logger

# ---- trace ----


class TestTrace:
    def test_new_trace_id_format(self):
        tid = new_trace_id()
        assert tid.startswith("fc_")
        assert len(tid) == 3 + 16

    def test_get_auto_generates_and_persists(self):
        set_trace_id(None)
        tid1 = get_trace_id()
        tid2 = get_trace_id()
        assert tid1 == tid2

    @pytest.mark.asyncio
    async def test_trace_context_cross_await(self):
        async def read_tid():
            await asyncio.sleep(0)
            return get_trace_id()

        fixed = "fc_fixed0000000001"
        async with trace_context(fixed):
            assert get_trace_id() == fixed
            assert await read_tid() == fixed
        assert get_trace_id() != fixed

    @pytest.mark.asyncio
    async def test_trace_context_nested(self):
        outer = "fc_outer0000000001"
        inner = "fc_inner0000000002"
        async with trace_context(outer):
            assert get_trace_id() == outer
            async with trace_context(inner):
                assert get_trace_id() == inner
            assert get_trace_id() == outer


# ---- metrics ----


class TestMetrics:
    def test_record_db_error_noop(self):
        record_db_error("insert")
        record_db_error("insert")

    @pytest.mark.asyncio
    async def test_metrics_middleware_counts(self):
        called = {"n": 0}

        async def app(scope, receive, send):
            called["n"] += 1
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        wrapped = metrics_middleware(app)
        recv_q = asyncio.Queue()
        sent = []

        async def send(msg):
            sent.append(msg)

        async def recv():
            return await recv_q.get()

        await wrapped({"type": "http", "method": "GET", "path": "/x"}, recv, send)
        assert called["n"] == 1
        assert sent[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_metrics_middleware_non_http_passthrough(self):
        called = {"n": 0}

        async def app(scope, receive, send):
            called["n"] += 1

        wrapped = metrics_middleware(app)
        await wrapped({"type": "lifespan"}, lambda: None, lambda m: None)
        assert called["n"] == 1


# ---- health ----


class TestHealth:
    @pytest.mark.asyncio
    async def test_check_db_none_store_ok(self):
        r = await HealthCheck.check_db(None)
        assert r["ok"] is True

    @pytest.mark.asyncio
    async def test_check_db_closed_store_down(self):
        store = MagicMock()
        store.backend = "sqlite"

        async def boom():
            raise RuntimeError("closed")

        store._ensure_db = boom
        r = await HealthCheck.check_db(store)
        assert r["ok"] is False

    @pytest.mark.asyncio
    async def test_check_db_sqlite_ok(self):
        store = MagicMock()
        store.backend = "sqlite"
        called = {"n": 0}

        async def ok():
            called["n"] += 1

        store._ensure_db = ok
        r = await HealthCheck.check_db(store)
        assert r["ok"] is True
        assert called["n"] == 1

    def test_check_disk(self):
        # 阈值须高于当前实际占用, 否则满盘机器本地 fail (CI runner 低占用不触发)
        import shutil as _sh

        cur = int(_sh.disk_usage("/").used / _sh.disk_usage("/").total * 100)
        r = HealthCheck.check_disk(cur + 10)
        assert r["ok"] is True
        assert "pct" in r
        # 阈值低于当前占用 → ok=False, 验比较逻辑
        r_down = HealthCheck.check_disk(max(1, cur - 1))
        assert r_down["ok"] is False

    @pytest.mark.asyncio
    async def test_check_upstream_unreachable(self):
        r = await HealthCheck.check_upstream("http://127.0.0.1:1", timeout=0.5)
        assert r["ok"] is False

    @pytest.mark.asyncio
    async def test_check_upstream_empty_ok(self):
        r = await HealthCheck.check_upstream("")
        assert r["ok"] is True

    @pytest.mark.asyncio
    async def test_run_all_degraded(self):
        r = await HealthCheck.run_all(store=None, upstreams={"mlx": "http://127.0.0.1:1"}, disk_threshold=1)
        assert r["status"] in ("degraded", "down")
        assert "db" in r["checks"]
        assert "disk" in r["checks"]
        assert "mlx" in r["checks"]


# ---- logger ----


class TestLogger:
    def test_setup_logger_plain(self):
        logger = setup_logger("test_plain", level=logging.DEBUG)
        assert logger.level == logging.DEBUG
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)

    def test_setup_logger_json(self):
        logger = setup_logger("test_json", json=True)
        assert len(logger.handlers) >= 1

    def test_setup_logger_file(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        logger = setup_logger("test_file", log_file=log_file, level=logging.DEBUG)
        logger.info("hello world")
        for h in logger.handlers:
            h.flush()
        assert Path(log_file).exists()
        for h in logger.handlers:
            h.close()
            logger.removeHandler(h)


# ---- health probe (desk_rpc _handle_health wired) ----


class TestHealthIntegration:
    @pytest.mark.asyncio
    async def test_run_health_default_mlx(self):
        r = await HealthCheck.run_all(store=None)
        assert "status" in r
        assert "mlx" in r["checks"]
