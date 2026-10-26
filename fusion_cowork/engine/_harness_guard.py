# #196: fusion-harness 内核装在 [plugins] extra (仅 git tag, 不在 PyPI) —
# 裸装 `pip install -e ".[test]"` 时缺包, 引擎 shim 模块用本 helper 把
# 裸 ImportError 换成带可执行安装指引的报错 (fail-visible)。

from __future__ import annotations

HARNESS_INSTALL_HINT = (
    "fusion-harness kernel package is missing (required by fusion_cowork.engine). "
    "Install it with: pip install 'fusion-cowork[plugins]' "
    "or pip install 'git+https://github.com/dahai80/fusion-harness.git@v0.2.0'"
)


def harness_import_error(exc: BaseException) -> ImportError:
    # __cause__ 由调用点的 `raise ... from exc` 设置
    return ImportError(HARNESS_INSTALL_HINT)
