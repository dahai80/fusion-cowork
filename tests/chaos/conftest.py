"""Chaos pack conftest (issue #386).

Every scenario documents its source issue — spec rule: a failure class
found live must convert to a replayable scenario, never pay the same
tuition twice.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.chaos]


@pytest.fixture()
def require_harness():
    pytest.importorskip("fusion_harness")
    return True
