# #196: the fusion-harness kernel ships in the [plugins] extra (git tag
# only, not on PyPI) — when the package is missing after a bare
# `pip install -e ".[test]"`, engine shim modules use this helper to turn
# the raw ImportError into an error with an actionable install hint
# (fail-visible).

from __future__ import annotations

HARNESS_INSTALL_HINT = (
    "fusion-harness kernel package is missing (required by fusion_cowork.engine). "
    "Install it with: pip install 'fusion-cowork[plugins]' "
    "or pip install 'git+https://github.com/dahai80/fusion-harness.git@v0.2.0'"
)


def harness_import_error(exc: BaseException) -> ImportError:
    # __cause__ is set by the caller's `raise ... from exc`
    return ImportError(HARNESS_INSTALL_HINT)
