"""可观测性模块 (v0.4.0 Stage 4).

- trace: 统一 trace_id (contextvar 跨 await 传播)
- metrics: OpenTelemetry 计数/直方图 + prometheus exporter
- tracing: OpenTelemetry span + OTLP exporter
- health: 深度健康检查 (DB SELECT 1 / 磁盘 / 上游)

替代散落各 server 的 secrets.token_hex(8) / mcp_<hex> trace_id (格式不一致, 不跨 await 传播)。
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
