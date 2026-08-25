"""Fusion-Cowork 日志工具 (v0.4.0 Stage 4 升级)。

支持:
- 控制台 (默认) / JSON 结构化 (json=True, structlog) — 生产可观测
- RotatingFileHandler (log_file 设了, maxBytes 100MB backupCount 5)
- trace_id processor — 从 contextvar 注入 (observability.trace)

向后兼容: 无 json=True 保旧格式; 现有调用零改动。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_FORMAT_VERBOSE = "[%(asctime)s] %(levelname)-8s %(name)s:%(lineno)d - %(message)s"
_LOG_FORMAT_PLAIN = "%(levelname)-8s %(message)s"
_FILE_MAX_BYTES = 100 * 1024 * 1024
_FILE_BACKUP = 5


class _TraceIdFilter(logging.Filter):
    """注入 trace_id 到 log record (从 contextvar, 无则空)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        tid = ""
        try:
            from fusion_cowork.observability.trace import _trace_var

            tid = _trace_var.get() or ""
        except Exception:
            pass
        record.trace_id = tid
        return True


def setup_logger(
    name: str = "fusion_cowork",
    level: int = logging.INFO,
    log_file: str = "",
    verbose: bool = False,
    json: bool = False,
) -> logging.Logger:
    """配置 Fusion-Cowork 日志系统。

    Args:
        name: 日志器名称
        level: 日志级别
        log_file: 日志文件路径（留空则只输出到控制台）
        verbose: 是否显示详细格式
        json: True → structlog JSON 结构化输出 (生产); False → 旧文本格式 (兼容)
    """
    if verbose:
        level = logging.DEBUG

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    trace_filter = _TraceIdFilter()

    if json:
        _setup_json_handler(logger, level, trace_filter)
    else:
        fmt = _LOG_FORMAT_VERBOSE if verbose else _LOG_FORMAT_PLAIN
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        console_handler.setLevel(level)
        console_handler.addFilter(trace_filter)
        logger.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=_FILE_MAX_BYTES, backupCount=_FILE_BACKUP, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT_VERBOSE))
        file_handler.setLevel(logging.DEBUG)
        file_handler.addFilter(trace_filter)
        logger.addHandler(file_handler)

    return logger


def _setup_json_handler(logger: logging.Logger, level: int, trace_filter: logging.Filter) -> None:
    try:
        import structlog
        from structlog.processors import JSONRenderer

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                _structlog_trace_processor,
                JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
            cache_logger_on_first_use=True,
        )
        # 标准 logging 也桥到 structlog JSON
        from structlog.stdlib import ProcessorFormatter

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            ProcessorFormatter(
                foreign_pre_chain=[
                    structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso"),
                    _structlog_trace_processor,
                ],
                processors=[ProcessorFormatter.remove_processors_meta, JSONRenderer()],
            )
        )
        handler.setLevel(level)
        handler.addFilter(trace_filter)
        logger.addHandler(handler)
    except ImportError:
        logger.warning("structlog 未装 — 回退文本日志; pip install 'fusion-cowork[cloud]'")
        fmt = _LOG_FORMAT_PLAIN
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        console_handler.setLevel(level)
        console_handler.addFilter(trace_filter)
        logger.addHandler(console_handler)


def _structlog_trace_processor(_logger, _method, event_dict: dict) -> dict:
    tid = ""
    try:
        from fusion_cowork.observability.trace import _trace_var

        tid = _trace_var.get() or ""
    except Exception:
        pass
    if tid:
        event_dict["trace_id"] = tid
    return event_dict


def get_logger(name: str = "fusion_cowork") -> logging.Logger:
    return logging.getLogger(name)
