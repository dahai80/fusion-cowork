"""指标采集 (v0.4.0 Stage 4).

OpenTelemetry MeterProvider + 核心 Counter/Histogram; prometheus exporter 可选。
metrics_middleware(app): ASGI 中间件 — 计数请求 + 直方图时长 (labels method/status)。
record_db_error(op): DB 错误计数。

无 OTel SDK 安装 → 退化为 no-op meter (不阻断, 测试/本地兼容)。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_meter: Optional[Any] = None
_counters: dict = {}
_histograms: dict = {}


def get_meter() -> Any:
    global _meter
    if _meter is not None:
        return _meter
    try:
        from opentelemetry import metrics as otm
        from opentelemetry.sdk.metrics import MeterProvider

        provider = MeterProvider()
        otm.set_meter_provider(provider)
        _meter = otm.get_meter("fusion-cowork", "0.4.0")
        logger.info("OpenTelemetry MeterProvider 已启用")
    except ImportError:
        _meter = _NoopMeter()
        logger.debug("opentelemetry-sdk 未装 — metrics 退化为 no-op")
    return _meter


class _NoopMeter:
    def create_counter(self, name, **_):
        return _NoopInstrument()

    def create_histogram(self, name, **_):
        return _NoopInstrument()

    def create_up_down_counter(self, name, **_):
        return _NoopInstrument()


class _NoopInstrument:
    def add(self, *_, **__):
        pass

    def record(self, *_, **__):
        pass


def _counter(name: str, **kwargs):
    if name not in _counters:
        _counters[name] = get_meter().create_counter(name, **kwargs)
    return _counters[name]


def _histogram(name: str, **kwargs):
    if name not in _histograms:
        _histograms[name] = get_meter().create_histogram(name, **kwargs)
    return _histograms[name]


def record_db_error(operation: str) -> None:
    _counter("fusion_db_errors_total", description="DB 错误计数").add(1, {"operation": operation})


def metrics_middleware(app: Any) -> Any:
    """ASGI 中间件 — 包 app, 计 req count + duration 直方图。"""
    counter = _counter("fusion_requests_total", description="请求总数")
    histo = _histogram("fusion_request_duration_seconds", description="请求时长")

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrap(message):
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status", 500)
            await send(message)

        try:
            await app(scope, receive, send_wrap)
        finally:
            counter.add(1, {"method": scope.get("method", "UNKNOWN"), "status": str(status["code"])})
            histo.record(time.perf_counter() - start)

    return wrapped


def maybe_start_prometheus_endpoint(port: Optional[int] = None) -> None:
    p = port or os.environ.get("FUSION_METRICS_PORT")
    if not p:
        return
    try:
        from prometheus_client import start_http_server

        start_http_server(int(p))
        logger.info(f"prometheus /metrics 端点已启动 :{p}")
    except ImportError:
        logger.debug("prometheus-client 未装 — /metrics 端点跳过")
    except Exception as e:
        logger.warning(f"prometheus 端点启动失败: {e}")
