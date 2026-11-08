"""Chaos S3: injected upstream 503 / outage window (issue #386, spec §3.2 row 3).

Source defects: as#649 (a single transient 503 killed pipeline nodes),
as#615 (stacked/hardcoded retry windows amplified a 4-minute outage).
Assertion: the KERNEL-owned retry policy (fusion-harness #13,
`RetryPolicy` + `run_with_retry`) exhausts with every attempt logged,
fails honestly past the deadline (never hangs), and 4xx-style
non-retryable errors fail fast without burning attempts.
"""

from __future__ import annotations

import logging

import pytest
from fusion_harness import Deadline, DeadlineExceeded, RetryPolicy, TransientError, run_with_retry

pytestmark = [pytest.mark.chaos]


class TestUpstream503:
    def test_transient_503_retries_then_recovers(self, caplog):
        attempts = {"n": 0}

        def upstream():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientError("503 Service Unavailable")
            return {"ok": True}

        with caplog.at_level(logging.WARNING, logger="fusion_harness.policies"):
            result = run_with_retry(upstream, RetryPolicy(attempts=5, base_backoff=0.0))
        assert result == {"ok": True}
        retry_lines = [r for r in caplog.records if "retry attempt=" in r.getMessage()]
        assert len(retry_lines) == 2, "every retry must be logged with attempt count"

    def test_outage_exhausts_policy_and_fails_honestly(self, caplog):
        def always_503():
            raise TransientError("503 Service Unavailable")

        with caplog.at_level(logging.ERROR, logger="fusion_harness.policies"), pytest.raises(TransientError):
            run_with_retry(always_503, RetryPolicy(attempts=3, base_backoff=0.0))
        assert any("retry stopped" in r.getMessage() for r in caplog.records)

    def test_outage_past_deadline_never_hangs(self):
        clock = {"t": 0.0}

        def always_503():
            raise TransientError("503 down for 4 minutes")

        deadline = Deadline(2.0, clock=lambda: clock["t"])

        def fake_sleep(s):
            clock["t"] += s

        with pytest.raises(DeadlineExceeded):
            run_with_retry(
                always_503,
                RetryPolicy(attempts=50, base_backoff=0.5, max_backoff=0.5),
                deadline=deadline,
                sleep=fake_sleep,
            )
        assert clock["t"] <= 2.0 + 1e-9, "client must not wait past the caller deadline"

    def test_4xx_fails_fast_no_attempts_burned(self):
        attempts = {"n": 0}

        def auth_error():
            attempts["n"] += 1
            raise ValueError("401 unauthorized")  # non-retryable classifier default

        with pytest.raises(ValueError):
            run_with_retry(auth_error, RetryPolicy(attempts=5, base_backoff=0.0))
        assert attempts["n"] == 1, "non-retryable must not consume retry budget"
