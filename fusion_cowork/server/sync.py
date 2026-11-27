"""Cross-device sync — workflow synchronization and collaboration across devices over WebSocket.

V0.3 features:
- Real-time WebSocket sync
- Device discovery and pairing
- Cross-device workflow execution
- Real-time status push
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def cross_sync_from_env() -> Optional[CrossDeviceSync]:
    """#123: web_serve injection seam — build the sync engine when FUSION_COWORK_CROSS_SYNC=1.

    Config: FUSION_COWORK_SYNC_HOST/PORT/TOKEN/CERT/KEY (default 127.0.0.1:11437).
    Returns None when disabled (/api/deliver keeps its honest 503).
    """
    if os.environ.get("FUSION_COWORK_CROSS_SYNC") != "1":
        logger.info("Cross-device sync disabled (FUSION_COWORK_CROSS_SYNC!=1), /api/deliver returns 503")
        return None
    instance = CrossDeviceSync(
        host=os.environ.get("FUSION_COWORK_SYNC_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUSION_COWORK_SYNC_PORT", "11437")),
        token=os.environ.get("FUSION_COWORK_SYNC_TOKEN") or None,
        ssl_cert=os.environ.get("FUSION_COWORK_SYNC_CERT") or None,
        ssl_key=os.environ.get("FUSION_COWORK_SYNC_KEY") or None,
    )
    logger.info(f"Cross-device sync enabled (FUSION_COWORK_CROSS_SYNC=1): {instance.host}:{instance.port}")
    return instance


class DeviceStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


@dataclass
class Device:
    """Device info."""

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
    """Sync message."""

    msg_id: str
    msg_type: str  # workflow_sync | status_update | file_transfer | command
    sender: str
    receiver: str  # broadcast means send to all devices
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


class CrossDeviceSync:
    """Cross-device sync engine — multi-device collaboration over WebSocket.

    Supports:
    - Device discovery and pairing
    - Workflow sync
    - Real-time status push
    - File transfer
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
        self._ws_server = None
        self._ws_clients: set = set()
        # R-7: track each workflow's latest synced updated_at; receivers reject stale versions based on it (conflict resolution).
        self._last_workflow_sync: Dict[str, float] = {}

    def register_device(self, device: Device) -> None:
        """Register a device."""
        device.status = DeviceStatus.ONLINE
        device.last_seen = time.time()
        self._devices[device.device_id] = device
        logger.info(f"Device registered: {device.name} ({device.device_id})")

    def unregister_device(self, device_id: str) -> None:
        """Unregister a device."""
        self._devices.pop(device_id, None)
        logger.info(f"Device unregistered: {device_id}")

    def get_online_devices(self) -> List[Device]:
        """Get online devices."""
        now = time.time()
        return [d for d in self._devices.values() if d.status == DeviceStatus.ONLINE and now - d.last_seen < 30]

    def _touch_device(self, sender_id: str) -> None:
        # R-7: refresh last_seen + set ONLINE when a message arrives — the old code only set last_seen in register_device,
        # so an active device that skipped re-registration for 30s was judged offline by get_online_devices → broadcasts lost their target.
        if not sender_id:
            return
        dev = self._devices.get(sender_id)
        if dev:
            dev.last_seen = time.time()
            dev.status = DeviceStatus.ONLINE

    def on_message(self, msg_type: str, handler: Callable) -> None:
        """Register a message handler."""
        self._message_handlers.setdefault(msg_type, []).append(handler)

    async def send_message(self, msg: SyncMessage) -> bool:
        """Send a message."""
        msg.timestamp = time.time()

        if msg.receiver == "broadcast":
            # Broadcast to all online devices
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
        """Send a message to a specific device."""
        # R-7: the old code opened a fresh TCP connection per message and dropped it on the first failure (a 5s peer blip lost everything).
        # Add retries: connection-level transient failures retry with backoff, covering peer restarts/network jitter windows;
        # a durable queue is out of scope for this patch (messages still lost when offline > retry window, logged as WARN).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
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
                        # Dev environments with self-signed certificates only
                        logger.warning(
                            "SSL verification disabled — for dev environments with self-signed certificates only"
                        )
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
            except (TimeoutError, OSError, asyncio.CancelledError) as e:
                if attempt < max_attempts:
                    backoff = 0.5 * attempt
                    logger.warning(
                        f"Send message failed {device.name} (attempt {attempt}): {e}, retrying in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.warning(
                        f"Send message failed {device.name} (still failing after {max_attempts} retries): {e}, message {msg.msg_id} dropped"
                    )
                    return False
            except Exception as e:
                logger.debug(f"Send message failed {device.name}: {e}")
                return False
        return False

    async def _handle_message(self, data: dict) -> None:
        """Handle a received message."""
        if self.token:
            incoming_token = data.get("token", "")
            # Stage 2: JWT first, static token fallback
            from fusion_cowork.auth import verify_any_token

            if verify_any_token(incoming_token, self.token) is None:
                logger.warning("Message authentication failed: invalid token")
                return
        msg_type = data.get("msg_type", "")
        # R-7: refresh the sender's last_seen on incoming messages (active devices not misjudged as offline)
        self._touch_device(data.get("sender", ""))
        # R-7: workflow_sync conflict resolution — the old last-writer-wins silently lost data (concurrent writes to the same workflow).
        # payload.updated_at is authoritative; stale versions are rejected outright (never overwriting a locally newer version).
        if msg_type == "workflow_sync":
            payload = data.get("payload", {}) or {}
            wf_id = str(payload.get("id") or payload.get("workflow_id") or "")
            incoming_ts = float(payload.get("updated_at") or 0.0)
            if wf_id and incoming_ts:
                local_ts = self._last_workflow_sync.get(wf_id, 0.0)
                if incoming_ts < local_ts:
                    logger.warning(
                        f"workflow_sync conflict: {wf_id} local updated_at={local_ts} is newer than inbound {incoming_ts}, rejecting stale version"
                    )
                    return
                self._last_workflow_sync[wf_id] = incoming_ts
        handlers = self._message_handlers.get(msg_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error(f"Message handler error: {e}")

    # ── Workflow sync ──

    async def sync_workflow(
        self,
        workflow_data: Dict[str, Any],
        target_devices: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Sync a workflow to other devices."""
        targets = target_devices or [d.device_id for d in self.get_online_devices()]

        # R-7: make sure the payload carries updated_at (the conflict-resolution basis). Fill in the send time if missing.
        if isinstance(workflow_data, dict) and not workflow_data.get("updated_at"):
            workflow_data = {**workflow_data, "updated_at": time.time()}
        wf_id = (
            str(workflow_data.get("id") or workflow_data.get("workflow_id") or "")
            if isinstance(workflow_data, dict)
            else ""
        )
        if wf_id:
            self._last_workflow_sync[wf_id] = float(workflow_data.get("updated_at") or 0.0)

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
        """Broadcast the current status."""
        msg = SyncMessage(
            msg_id=f"status_{uuid.uuid4().hex[:8]}",
            msg_type="status_update",
            sender=self.device_id,
            receiver="broadcast",
            payload=status_data,
        )
        await self.send_message(msg)

    # ── Lifecycle ──

    async def start(self) -> None:
        """Start the sync service — listen on WebSocket for inbound connections."""
        self._running = True
        logger.info(f"Cross-device sync started: {self.host}:{self.port}")
        try:
            import websockets

            async def _ws_handler(websocket):
                self._ws_clients.add(websocket)
                remote = websocket.remote_address if hasattr(websocket, "remote_address") else "?"
                logger.info(f"WS device connected: {remote}")
                try:
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                            # Auth check (same as _handle_message): verify each message when a token is configured
                            if self.token:
                                from fusion_cowork.auth import verify_any_token

                                if verify_any_token(data.get("token", ""), self.token) is None:
                                    logger.warning(f"WS message authentication failed: {remote}, invalid token")
                                    continue
                            # R-7: the WS path refreshes the sender's last_seen too
                            self._touch_device(data.get("sender", ""))
                            msg = SyncMessage(
                                msg_id=data.get("msg_id", f"msg_{uuid.uuid4().hex[:8]}"),
                                msg_type=data.get("msg_type", "unknown"),
                                sender=data.get("sender", "remote"),
                                receiver=data.get("receiver", self.device_id),
                                payload=data.get("payload", {}),
                                timestamp=data.get("timestamp", time.time()),
                            )
                            handlers = self._message_handlers.get(msg.msg_type, [])
                            for h in handlers:
                                try:
                                    res = h(msg)
                                    if asyncio.iscoroutine(res):
                                        await res
                                except Exception as he:
                                    logger.error(f"WS message handler error: {he}")
                        except (json.JSONDecodeError, KeyError) as je:
                            logger.warning(f"WS message parse failed: {je}")
                except Exception as e:
                    logger.debug(f"WS connection ended: {e}")
                finally:
                    self._ws_clients.discard(websocket)

            ssl_ctx = self._build_server_ssl_context()
            self._ws_server = await websockets.serve(_ws_handler, self.host, self.port, ssl=ssl_ctx)
            scheme = "wss" if ssl_ctx else "ws"
            logger.info(f"WebSocket sync service listening: {scheme}://{self.host}:{self.port}")
        except ImportError:
            logger.warning(
                "websockets library not installed (pip install websockets), WS inbound listening degraded off"
            )
        except OSError as oe:
            # R-7: the old code only logged, leaving _running True + _ws_server None → half-dead state, callers wrongly assumed success.
            # fail-visible: roll back _running and raise so callers know startup failed.
            self._running = False
            logger.error(f"WebSocket listener failed to start: {oe}")
            raise

    async def stop(self) -> None:
        """Stop the sync service."""
        self._running = False
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Cross-device sync stopped")

    def _build_server_ssl_context(self):
        """Build the inbound WS server TLS ssl_context — HI-12.

        fail-closed: when ssl_cert/ssl_key are set but loading fails → raise, never degrade to plaintext.
        Not configured → None (plaintext).
        """
        if not self.ssl_cert or not self.ssl_key:
            return None
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=self.ssl_cert, keyfile=self.ssl_key)
            logger.info(f"CrossDeviceSync inbound TLS enabled: cert={self.ssl_cert}")
            return ctx
        except Exception as e:
            logger.error(
                f"CrossDeviceSync inbound TLS certificate load failed, refusing plaintext fallback (fail-closed): {e}"
            )
            # i18n: user-facing
            raise RuntimeError(f"入站 TLS 证书加载失败, 拒绝降级明文: {e}") from e

    def get_connected_devices(self) -> List[Dict[str, Any]]:
        """Get the list of connected devices."""
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
