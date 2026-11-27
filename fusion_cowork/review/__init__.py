"""Multi-agent code review package - P2-7.

UltraReview: multi-lens (security/correctness/style/tests) parallel LLM review
-> aggregate + rank -> synthesize report. Falls back to a file listing when
the LLM is unavailable.
"""

from .ultra_review import (
    LENS_DEFINITIONS,
    ReviewFinding,
    ReviewReport,
    run_ultra_review,
)

__all__ = [
    "LENS_DEFINITIONS",
    "ReviewFinding",
    "ReviewReport",
    "run_ultra_review",
]
