"""SQL 占位符归一化 (v0.4.0 Stage 3).

SQLite 用 ? 占位符, Postgres (asyncpg) 用 $1/$2/...。
所有 store SQL 保持 ? (SQLite 风格), postgres 路径经 normalize_placeholders 转换。
单 helper, 非架构层。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\?")


def normalize_placeholders(sql: str) -> str:
    """把 SQL 里的 ? 占位符替换为 Postgres 风格 $1, $2, ...。

    按 ? 出现顺序编号。不处理引号内的 ? (SQL 字面量极少含 ?, 旧库无此用法)。
    """
    counter = [0]

    def _sub(_m: re.Match) -> str:
        counter[0] += 1
        return f"${counter[0]}"

    return _PLACEHOLDER_RE.sub(_sub, sql)
