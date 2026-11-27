"""Observability module (v0.4.0 Stage 4).

- trace: unified trace_id (contextvar propagated across awaits)
- metrics: OpenTelemetry counters/histograms + prometheus exporter
- tracing: OpenTelemetry spans + OTLP exporter
- health: deep health checks (DB SELECT 1 / disk / upstream)

Replaces the scattered secrets.token_hex(8) / mcp_<hex> trace_ids in various servers (inconsistent formats, not propagated across awaits).
"""

from fusion_cowork.observability.health import HealthCheck, run_health
from fusion_cowork.observability.metrics import metrics_middleware, record_db_error
from fusion_cowork.observability.trace import (
    get_trace_id,
    new_trace_id,
    set_trace_id,
    trace_context,
)

__all__ = [
    "get_trace_id",
    "new_trace_id",
    "set_trace_id",
    "trace_context",
    "metrics_middleware",
    "record_db_error",
    "HealthCheck",
    "run_health",
]
