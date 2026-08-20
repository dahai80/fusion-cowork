"""移动推送通知 — P2-5。

支持 Bark (iOS/macOS 自建) 与 ntfy (跨平台自建/公共)。未配置 server 时
降级到本地 macOS Notification Center (osascript), 结果标明降级。
配置来源: ConfigCenter push.bark_url / push.ntfy_url / push.ntfy_token。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

BARK_DEFAULT = "https://api.day.app"
NTFY_DEFAULT = "https://ntfy.sh"
LOCAL_FALLBACK = "local"
_TIMEOUT = 10.0


@dataclass
class PushConfig:
    provider: str = "auto"
    url: str = ""
    token: str = ""
    sound: str = ""
    priority: str = ""
    group: str = ""

    def resolved_provider(self) -> str:
        if self.provider != "auto":
            return self.provider
        if self.url:
            return "bark" if "bark" in self.url or "day.app" in self.url else "ntfy"
        return LOCAL_FALLBACK


@dataclass
class PushResult:
    success: bool = False
    provider: str = ""
    response: str = ""
    error: str = ""
    degraded: bool = False
    data: Dict[str, Any] = field(default_factory=dict)


def resolve_config(
    provider: str = "auto",
    url: str = "",
    token: str = "",
    sound: str = "",
    priority: str = "",
    group: str = "",
) -> PushConfig:
    """合并显式参数与 ConfigCenter 配置, 显式参数优先。"""
    from ..config_center import ConfigCenter

    cc = ConfigCenter.get_instance()
    bark_url = url or cc.get("push.bark_url", "")
    ntfy_url = cc.get("push.ntfy_url", "")
    ntfy_token = token or cc.get("push.ntfy_token", "")

    cfg_url = bark_url
    if not cfg_url and ntfy_url:
        cfg_url = ntfy_url
    cfg_token = ntfy_token
    cfg_sound = sound or cc.get("push.sound", "")
    cfg_priority = priority or cc.get("push.priority", "")
    cfg_group = group or cc.get("push.group", "")

    return PushConfig(
        provider=provider,
        url=cfg_url,
        token=cfg_token,
        sound=cfg_sound,
        priority=cfg_priority,
        group=cfg_group,
    )


async def push(
    title: str,
    message: str,
    *,
    provider: str = "auto",
    url: str = "",
    token: str = "",
    sound: str = "",
    priority: str = "",
    group: str = "",
) -> PushResult:
    """发送移动推送, 自动选择 provider, 失败/无配置时降级本地通知。"""
    if not message:
        return PushResult(success=False, error="推送内容不能为空")

    cfg = resolve_config(provider, url, token, sound, priority, group)
    prov = cfg.resolved_provider()
    logger.info(f"推送 provider={prov} title={title!r}")

    if prov == "bark":
        return await _push_bark(cfg, title, message)
    if prov == "ntfy":
        return await _push_ntfy(cfg, title, message)
    return await _push_local(title, message)


async def _push_bark(cfg: PushConfig, title: str, message: str) -> PushResult:
    base = cfg.url.rstrip("/")
    token = cfg.token
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if token:
                endpoint = f"{base}/{quote(token, safe='')}"
                payload: Dict[str, Any] = {"title": title, "body": message}
                if cfg.sound:
                    payload["sound"] = cfg.sound
                if cfg.group:
                    payload["group"] = cfg.group
                resp = await client.post(endpoint, json=payload)
            else:
                path = f"{base}/{quote(title, safe='')}/{quote(message, safe='')}"
                resp = await client.get(path)
            ok = resp.status_code == 200
            return PushResult(
                success=ok,
                provider="bark",
                response=resp.text[:500],
                error="" if ok else f"Bark HTTP {resp.status_code}",
                data={"status_code": resp.status_code, "url": str(resp.url)},
            )
    except Exception as e:
        logger.error(f"Bark 推送失败, 降级本地: {e}")
        return await _push_local(title, message, degraded_from="bark", err=str(e))


async def _push_ntfy(cfg: PushConfig, title: str, message: str) -> PushResult:
    base = cfg.url.rstrip("/")
    topic = cfg.token or "fusion-cowork"
    headers: Dict[str, str] = {"Title": title}
    if cfg.priority:
        headers["Priority"] = cfg.priority
    if cfg.group:
        headers["Tags"] = cfg.group
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{base}/{quote(topic, safe='')}", content=message, headers=headers)
            ok = resp.status_code in (200, 202)
            return PushResult(
                success=ok,
                provider="ntfy",
                response=resp.text[:500],
                error="" if ok else f"ntfy HTTP {resp.status_code}",
                data={"status_code": resp.status_code, "topic": topic},
            )
    except Exception as e:
        logger.error(f"ntfy 推送失败, 降级本地: {e}")
        return await _push_local(title, message, degraded_from="ntfy", err=str(e))


async def _push_local(title: str, message: str, degraded_from: str = "", err: str = "") -> PushResult:
    """降级: 发 macOS 本地通知。"""
    title_e = title.replace('"', '\\"')
    msg_e = message.replace('"', '\\"')
    script = f'display notification "{msg_e}" with title "{title_e}"'
    try:
        proc = await asyncio.create_subprocess_shell(
            f"osascript -e '{script}'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        ok = proc.returncode == 0
        return PushResult(
            success=ok,
            provider=LOCAL_FALLBACK,
            degraded=bool(degraded_from),
            response="local notification sent" if ok else stderr.decode()[:300],
            error=err if err else ("" if ok else "本地通知失败"),
            data={"degraded_from": degraded_from},
        )
    except Exception as e:
        return PushResult(
            success=False,
            provider=LOCAL_FALLBACK,
            error=f"本地通知失败: {e}",
            degraded=bool(degraded_from),
        )
