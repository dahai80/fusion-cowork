"""分布式追踪 (v0.4.0 Stage 4).

OpenTelemetry TracerProvider + span ctx mgr + OTLP exporter (env OTEL_EXPORTER_OTLP_ENDPOINT)。
无 OTel SDK → no-op span (不阻断)。

用法:
    from fusion_cowork.observability.tracing import span
    with span("desk_rpc.dispatch", method="desk.nodes.list"):
        ...
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_tracer: Optional[Any] = None


def get_tracer() -> Any:
    global _tracer
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace as ott
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(resource=Resource.create({"service.name": "fusion-cowork"}))
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(
                    __import__(
                        "opentelemetry.sdk.trace.export",
                        fromlist=["BatchSpanProcessor"],
                    ).BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
                logger.info(f"OTLP span exporter → {endpoint}")
            except ImportError:
                logger.debug("otlp exporter 包未装 — span 仅本地")
        ott.set_tracer_provider(provider)
        _tracer = ott.get_tracer("fusion-cowork", "0.4.0")
        logger.info("OpenTelemetry TracerProvider 已启用")
    except ImportError:
        _tracer = _NoopTracer()
        logger.debug("opentelemetry-sdk 未装 — tracing 退化为 no-op")
    return _tracer


class _NoopTracer:
    def start_as_current_span(self, name, **_):
        return _NoopSpan()


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, *_, **__):
        pass

    def record_exception(self, *_, **__):
        pass


class span:
    """ctx mgr — 起当前 tracer span, 可设属性。同步/异步兼容 (不 await)。"""

    def __init__(self, name: str, **attrs: Any):
        self._name = name
        self._attrs = attrs
        self._ctx = None

    def __enter__(self):
        self._ctx = get_tracer().start_as_current_span(self._name)
        s = self._ctx.__enter__()
        for k, v in self._attrs.items():
            s.set_attribute(k, v)
        return s

    def __exit__(self, *exc):
        if self._ctx is not None:
            return self._ctx.__exit__(*exc)
        return False
