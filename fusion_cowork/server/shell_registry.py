"""Union Web shell 注册 — issue #97 (PRD §5.2, 对齐 agent-studio shell_registry)。

web serve 启动时向 fusion-gateway shell 注册表注册 web/manifest.json
(POST /api/v1/shell/manifests, fg-key auth) 并周期心跳保活
(heartbeat_timeout 后网关置灰)。env 门控 — 需同时配置:
- FUSION_SHELL_GATEWAY_URL + FUSION_GATEWAY_ADMIN_KEY
可选 FUSION_SHELL_MANIFEST 覆盖 manifest 路径。未配置则静默跳过 (默认 OFF)。
/shell/fusion-cowork/ 静态代理本身是网关配置 — 动态 manifest 只驱动菜单入口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

GATEWAY_URL_ENV = "FUSION_SHELL_GATEWAY_URL"
ADMIN_KEY_ENV = "FUSION_GATEWAY_ADMIN_KEY"
MANIFEST_PATH_ENV = "FUSION_SHELL_MANIFEST"
UPSTREAM_OVERRIDE_ENV = "FUSION_COWORK_WEB_UPSTREAM"
REGISTER_INTERVAL_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 5.0


def _default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "web" / "manifest.json"


def shell_registration_enabled() -> bool:
    return bool(os.environ.get(GATEWAY_URL_ENV, "").strip() and os.environ.get(ADMIN_KEY_ENV, "").strip())


def load_manifest() -> Dict[str, Any]:
    path = Path(os.environ.get(MANIFEST_PATH_ENV, "").strip() or _default_manifest_path())
    if not path.exists():
        raise FileNotFoundError(f"shell manifest 不存在: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not manifest.get("id"):
        raise ValueError(f"shell manifest 缺少 id: {path}")
    # issue #125: gateway 动态代理 (#232 契约) 解析 upstream/api_key —
    # manifest 声明默认值; FUSION_COWORK_WEB_UPSTREAM 覆盖部署特定端口;
    # api_key 经 api_key_env 指向的运行时变量注入 (空值不注入, 不落盘)。
    override = os.environ.get(UPSTREAM_OVERRIDE_ENV, "").strip()
    if override:
        manifest["upstream"] = override
    if not manifest.get("upstream"):
        manifest["upstream"] = "http://127.0.0.1:11461"
    key_env = str(manifest.get("api_key_env", "")).strip()
    api_key = os.environ.get(key_env, "").strip() if key_env else ""
    if api_key:
        manifest["api_key"] = api_key
    else:
        manifest.pop("api_key", None)
    logger.info(
        f"shell manifest 已加载: {path} (id={manifest['id']} upstream={manifest['upstream']} api_key={'set' if api_key else 'unset'})"
    )
    return manifest


async def _registry_loop_body(client: Any, manifest: Dict[str, Any], interval: float) -> None:
    module_id = str(manifest["id"])
    gateway = os.environ.get(GATEWAY_URL_ENV, "").strip().rstrip("/")
    admin_key = os.environ.get(ADMIN_KEY_ENV, "").strip()
    headers = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}
    registered = False
    while True:
        try:
            if not registered:
                resp = await client.post(f"{gateway}/api/v1/shell/manifests", json=manifest, headers=headers)
                if resp.status_code == 200:
                    registered = True
                    logger.info(f"shell manifest 已注册: {module_id}")
                else:
                    logger.warning(f"shell manifest 注册失败: {resp.status_code} {resp.text[:200]}")
            else:
                resp = await client.post(f"{gateway}/api/v1/shell/manifests/{module_id}/heartbeat", headers=headers)
                if resp.status_code == 404:
                    logger.info(f"shell 心跳 404, 重注册 {module_id}")
                    registered = False
                elif resp.status_code != 200:
                    raise RuntimeError(f"shell 心跳失败: {resp.status_code} {resp.text[:200]}")
        except asyncio.CancelledError:
            logger.info(f"shell 注册循环已取消 (module={module_id})")
            raise
        except Exception as e:
            logger.warning(f"shell 注册循环错误 (将重试): {e}")
        await asyncio.sleep(interval)


async def shell_registry_loop(client: Any = None, interval: float = REGISTER_INTERVAL_SECONDS) -> None:
    """注册一次, 心跳保活; 网关重启 (404) 后重注册。失败仅告警重试, 不影响服务。"""
    try:
        manifest = load_manifest()
    except Exception as e:
        logger.error(f"shell 注册禁用 (manifest 不可用): {e}")
        return
    if client is None:
        import httpx

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as own:
            await _registry_loop_body(own, manifest, interval)
    else:
        await _registry_loop_body(client, manifest, interval)
