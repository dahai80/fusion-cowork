"""Chaos S7: deploy drift / version-string consistency (F5, spec verification-infra-1108 §3.2 row 7 / §3.5 DoD #3).

Guards against deploy drift where the web API health endpoint reports a
version string that no longer matches the installed fusion_cowork package.
"""

from __future__ import annotations

import inspect

import pytest

import fusion_cowork
import fusion_cowork.server.web_api as web_api

pytestmark = [pytest.mark.chaos]


def test_health_version_matches_package_version():
    version = web_api._version()
    assert isinstance(version, str)
    assert version.strip() != ""
    assert version == fusion_cowork.__version__


def test_health_endpoint_source_delegates_to_version_helper():
    src = inspect.getsource(web_api)
    idx = src.find("async def health")
    assert idx != -1, "health endpoint must exist in fusion_cowork.server.web_api"
    body = src[idx : idx + 400]
    assert '"version": _version()' in body, (
        "health endpoint must report its version via _version(), not a hardcoded literal"
    )
