"""深度研究包 — P2-6。

多 Agent 研究: 规划 (分解问题) → 搜索 (并行 web 搜索) → 合成 (LLM 汇总带引用报告)。
LLM 不可用时降级返回结构化原始发现。
"""

from .deep_research import (
    ResearchFinding,
    ResearchReport,
    ResearchSubQuestion,
    run_deep_research,
)

__all__ = [
    "ResearchFinding",
    "ResearchReport",
    "ResearchSubQuestion",
    "run_deep_research",
]
