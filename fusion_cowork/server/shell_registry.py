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

# issue #168 (K4): 网关签发 fm- 模块密钥落盘路径 (0600)。
# 静态 env 密钥继续作为覆盖路径向后兼容; 网关签发后文件覆盖 env 值。
MODULE_KEY_PATH = Path.home() / ".fusion-cowork" / "web_module_key"


def load_module_key() -> str:
    try:
        return MODULE_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_module_key(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    MODULE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODULE_KEY_PATH.write_text(key + "\n", encoding="utf-8")
    MODULE_KEY_PATH.chmod(0o600)
    logger.info(f"网关签发模块密钥已持久化: {MODULE_KEY_PATH} ({_mask_key(key)})")
    return True


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _extract_issued_key(payload: Any) -> str:
    # issue #173/#174: fusion-gateway K1 签发响应 (writeShellKeyResponse) 明文在 raw_key,
    # 其余字段名保留兼容旧/自定义网关
    if not isinstance(payload, dict):
        return ""
    for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("data")
    if isinstance(nested, dict):
        for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
            value = nested.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


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
    # issue #168 (K4): env 未配置时不随注册体发送 api_key — 网关 (K1) 签发
    # fm- 密钥并在响应中返回一次, 由 _registry_loop_body 持久化; env 已配置
    # 则保留静态覆盖路径 (旧网关向后兼容)。
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
    admin_headers = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}
    registered = False
    while True:
        try:
            if not registered:
                resp = await client.post(f"{gateway}/api/v1/shell/manifests", json=manifest, headers=admin_headers)
                if resp.status_code == 200:
                    registered = True
                    logger.info(f"shell manifest 已注册: {module_id}")
                    # issue #168 (K4): 注册响应携带网关签发密钥时持久化 (仅此一次返回明文)
                    issued = _extract_issued_key(_safe_json(resp))
                    if issued and issued != load_module_key():
                        save_module_key(issued)
                    elif issued:
                        logger.info("网关签发密钥与已存值一致, 不重复落盘")
                else:
                    logger.warning(f"shell manifest 注册失败: {resp.status_code} {resp.text[:200]}")
            else:
                # issue #168 (K4): 心跳改用签发 fm- 密钥鉴权; 未签发时回退 admin key
                module_key = load_module_key()
                beat_key = module_key or admin_key
                headers = {"Authorization": f"Bearer {beat_key}", "Content-Type": "application/json"}
                # issue #177: 心跳携带 manifest (网关 #284 契约) — 每拍重读磁盘,
                # 磁盘编辑 (增删 pages 等) 一个心跳周期内收敛到网关注册表, 无需重启;
                # 重读失败沿用注册时版本, 不中断心跳保活
                try:
                    beat_manifest = load_manifest()
                except Exception as e:
                    logger.warning(f"shell 心跳重读 manifest 失败, 沿用注册时版本: {e}")
                    beat_manifest = manifest
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests/{module_id}/heartbeat", json=beat_manifest, headers=headers
                )
                if resp.status_code == 404:
                    logger.info(f"shell 心跳 404, 重注册 {module_id}")
                    registered = False
                elif resp.status_code == 401 and module_key:
                    # 签发密钥被轮换/吊销 — 重注册走 admin key 重新获取
                    logger.warning(f"shell 心跳 401 (签发密钥失效), 重注册 {module_id}")
                    registered = False
                elif resp.status_code != 200:
                    raise RuntimeError(f"shell 心跳失败: {resp.status_code} {resp.text[:200]}")
        except asyncio.CancelledError:
            logger.info(f"shell 注册循环已取消 (module={module_id})")
            raise
        except Exception as e:
            logger.warning(f"shell 注册循环错误 (将重试): {e}")
        await asyncio.sleep(interval)


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


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
