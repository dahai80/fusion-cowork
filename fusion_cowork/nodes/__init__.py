"""Fusion-Cowork 节点系统。

7 类节点 (macos/ai/io/logic/tools/browser/ecosystem), 全部通过 @register_node
注册到 NodeRegistry。节点模块在 import 时自注册, 故服务端启动需显式导入全部
节点模块, 否则 NodeRegistry 为空或残缺 (历史 bug: 仅 cli.py 副作用导入的
macos+browser 注册, 33/47 节点可见)。
"""

import logging

logger = logging.getLogger(__name__)

_NODE_MODULES = (
    "fusion_cowork.nodes.macos.system_nodes",
    "fusion_cowork.nodes.macos.input_nodes",
    "fusion_cowork.nodes.ai.classify",
    "fusion_cowork.nodes.io.file_io",
    "fusion_cowork.nodes.logic.logic_nodes",
    "fusion_cowork.nodes.tools.tool_nodes",
    "fusion_cowork.nodes.browser.browser_nodes",
    "fusion_cowork.nodes.browser.cdp_nodes",
    "fusion_cowork.nodes.ecosystem.trainer_node",
)


def import_all_nodes() -> int:
    """显式导入全部节点模块, 触发 @register_node 注册。

    幂等: 重复调用只导入一次 (Python import 缓存)。
    容错: 单个模块导入失败记录警告并继续, 不阻断服务启动。

    Returns:
        成功导入的模块数
    """
    import importlib

    ok = 0
    for mod_path in _NODE_MODULES:
        try:
            importlib.import_module(mod_path)
            ok += 1
        except Exception as e:
            logger.warning("节点模块导入失败 %s: %s", mod_path, e)
    logger.info("节点模块导入完成: %d/%d", ok, len(_NODE_MODULES))
    return ok
