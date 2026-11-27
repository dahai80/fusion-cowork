# #196: the fusion-harness kernel ships in the [plugins] extra (git tag only, not on PyPI) —
# on a bare `pip install -e ".[test]"` the package is missing, and engine shim modules use
# this helper to replace the bare ImportError with an error carrying actionable install guidance (fail-visible).

from __future__ import annotations

HARNESS_INSTALL_HINT = (
    "fusion-harness kernel package is missing (required by fusion_cowork.engine). "
    "Install it with: pip install 'fusion-cowork[plugins]' "
    "or pip install 'git+https://github.com/dahai80/fusion-harness.git@v0.2.0'"
)


def harness_import_error(exc: BaseException) -> ImportError:
    # __cause__ is set by the caller's `raise ... from exc`
    return ImportError(HARNESS_INSTALL_HINT)
