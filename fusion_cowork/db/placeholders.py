"""SQL placeholder normalization (v0.4.0 Stage 3).

SQLite uses ? placeholders, Postgres (asyncpg) uses $1/$2/....
All store SQL stays ? (SQLite style); the postgres path converts via normalize_placeholders.
Single helper, not an architecture layer.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\?")


def normalize_placeholders(sql: str) -> str:
    """Replace ? placeholders in SQL with Postgres-style $1, $2, ....

    Numbered by order of appearance. Does not handle ? inside quotes (SQL literals rarely contain ?, legacy tables never use it).
    """
    counter = [0]

    def _sub(_m: re.Match) -> str:
        counter[0] += 1
        return f"${counter[0]}"

    return _PLACEHOLDER_RE.sub(_sub, sql)
