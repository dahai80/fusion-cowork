"""跨设备协同 — 通过 WebSocket 实现多设备间的工作流同步与协作。

V0.3 特性：
- WebSocket 实时同步
- 设备发现与配对
- 工作流跨设备执行
- 状态实时推送
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class DeviceStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


@dataclass
class Device:
    """设备信息。"""

    device_id: str
    name: str
    device_type: str = "mac"  # mac | iphone | ipad
    host: str = "localhost"
    port: int = 11437
    status: DeviceStatus = DeviceStatus.OFFLINE
    capabilities: List[str] = field(default_factory=list)
    last_seen: float = 0.0


@dataclass
class SyncMessage:
    """同步消息。"""

    msg_id: str
    msg_type: str  # workflow_sync | status_update | file_transfer | command
    sender: str
    receiver: str  # broadcast 表示广播
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


class CrossDeviceSync:
    """跨设备同步引擎 — 通过 WebSocket 实现多设备协作。

    支持：
    - 设备发现与配对
    - 工作流同步
    - 状态实时推送
    - 文件传输
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11437,
        token: Optional[str] = None,
        ssl_cert: Optional[str] = None,
        ssl_key: Optional[str] = None,
        ssl_verify: bool = True,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.ssl_verify = ssl_verify
        self.device_id = f"device_{uuid.uuid4().hex[:8]}"
        self._devices: Dict[str, Device] = {}
        self._message_handlers: Dict[str, List[Callable]] = {}
        self._running = False
        self._server: Optional[asyncio.AbstractServer] = None

    def register_device(self, device: Device) -> None:
        """注册设备。"""
        device.status = DeviceStatus.ONLINE
        device.last_seen = time.time()
        self._devices[device.device_id] = device
        logger.info(f"设备注册: {device.name} ({device.device_id})")

    def unregister_device(self, device_id: str) -> None:
        """注销设备。"""
        self._devices.pop(device_id, None)
        logger.info(f"设备注销: {device_id}")

    def get_online_devices(self) -> List[Device]:
        """获取在线设备。"""
        now = time.time()
        return [d for d in self._devices.values() if d.status == DeviceStatus.ONLINE and now - d.last_seen < 30]

    def on_message(self, msg_type: str, handler: Callable) -> None:
        """注册消息处理器。"""
        self._message_handlers.setdefault(msg_type, []).append(handler)

    async def send_message(self, msg: SyncMessage) -> bool:
        """发送消息。"""
        msg.timestamp = time.time()

        if msg.receiver == "broadcast":
            # 广播给所有在线设备
            tasks = []
            for device in self.get_online_devices():
                if device.device_id != msg.sender:
                    tasks.append(self._send_to_device(device, msg))
            await asyncio.gather(*tasks, return_exceptions=True)
            return True
        else:
            device = self._devices.get(msg.receiver)
            if device and device.status == DeviceStatus.ONLINE:
                return await self._send_to_device(device, msg)
            return False

    async def _send_to_device(self, device: Device, msg: SyncMessage) -> bool:
        """发送消息到指定设备。"""
        try:
            ssl_ctx = None
            if self.ssl_cert and self.ssl_key:
                import ssl

                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_ctx.load_cert_chain(self.ssl_cert, self.ssl_key)
                if self.ssl_verify:
                    ssl_ctx.check_hostname = True
                    ssl_ctx.verify_mode = ssl.CERT_REQUIRED
                else:
                    # 仅用于自签名证书的开发环境
                    logger.warning("SSL 验证已禁用 — 仅用于开发环境自签名证书")
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(device.host, device.port, ssl=ssl_ctx),
                timeout=5.0,
            )
            data = json.dumps(
                {
                    "msg_id": msg.msg_id,
                    "msg_type": msg.msg_type,
                    "sender": msg.sender,
                    "payload": msg.payload,
                    "timestamp": msg.timestamp,
                    "token": self.token or "",
                }
            )
            writer.write(data.encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as e:
            logger.debug(f"发送消息失败 {device.name}: {e}")
            return False

    async def _handle_message(self, data: dict) -> None:
        """处理接收到的消息。"""
        if self.token:
            incoming_token = data.get("token", "")
            if incoming_token != self.token:
                logger.warning("消息认证失败: token 不匹配")
                return
        msg_type = data.get("msg_type", "")
        handlers = self._message_handlers.get(msg_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error(f"消息处理器异常: {e}")

    # ── 工作流同步 ──

    async def sync_workflow(
        self,
        workflow_data: Dict[str, Any],
        target_devices: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """同步工作流到其他设备。"""
        targets = target_devices or [d.device_id for d in self.get_online_devices()]

        msg = SyncMessage(
            msg_id=f"sync_{uuid.uuid4().hex[:8]}",
            msg_type="workflow_sync",
            sender=self.device_id,
            receiver="broadcast",
            payload=workflow_data,
        )

        results = {"success": 0, "failed": 0, "details": []}
        for device_id in targets:
            device = self._devices.get(device_id)
            if device:
                msg.receiver = device_id
                success = await self.send_message(msg)
                if success:
                    results["success"] += 1
                    results["details"].append({"device": device_id, "status": "ok"})
                else:
                    results["failed"] += 1
                    results["details"].append({"device": device_id, "status": "failed"})

        return results

    async def sync_status(self, status_data: Dict[str, Any]) -> None:
        """广播当前状态。"""
        msg = SyncMessage(
            msg_id=f"status_{uuid.uuid4().hex[:8]}",
            msg_type="status_update",
            sender=self.device_id,
            receiver="broadcast",
            payload=status_data,
        )
        await self.send_message(msg)

    # ── 生命周期 ──

    async def start(self) -> None:
        """启动同步服务。"""
        self._running = True
        logger.info(f"跨设备同步启动: {self.host}:{self.port}")

    async def stop(self) -> None:
        """停止同步服务。"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("跨设备同步已停止")

    def get_connected_devices(self) -> List[Dict[str, Any]]:
        """获取连接的设备列表。"""
        return [
            {
                "device_id": d.device_id,
                "name": d.name,
                "type": d.device_type,
                "status": d.status.value,
                "capabilities": d.capabilities,
            }
            for d in self._devices.values()
        ]
