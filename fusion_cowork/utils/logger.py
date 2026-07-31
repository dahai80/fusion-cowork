"""Fusion-Cowork 日志工具。"""

import logging
import sys
from pathlib import Path


def setup_logger(
    name: str = "fusion_cowork",
    level: int = logging.INFO,
    log_file: str = "",
    verbose: bool = False,
) -> logging.Logger:
    """配置 Fusion-Cowork 日志系统。

    Args:
        name: 日志器名称
        level: 日志级别
        log_file: 日志文件路径（留空则只输出到控制台）
        verbose: 是否显示详细格式

    Returns:
        logging.Logger: 配置好的日志器
    """
    if verbose:
        level = logging.DEBUG

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    if verbose:
        fmt = "[%(asctime)s] %(levelname)-8s %(name)s:%(lineno)d - %(message)s"
    else:
        fmt = "%(levelname)-8s %(message)s"
    console_handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # 文件输出
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s:%(lineno)d - %(message)s")
        )
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "fusion_cowork") -> logging.Logger:
    """获取 Fusion-Cowork 日志器。"""
    return logging.getLogger(name)