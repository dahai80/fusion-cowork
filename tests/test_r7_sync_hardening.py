"""R-7 cross-device sync hardening tests — last_seen refresh + _send_to_device retry + start() half-dead-state fix."""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.server.sync import CrossDeviceSync, Device, DeviceStatus, SyncMessage


def _device(dev_id="peer_1", host="127.0.0.1", port=1):
    return Device(
        device_id=dev_id,
        name="peer",
        device_type="mac",
        host=host,
        port=port,
        status=DeviceStatus.ONLINE,
    )


# ── last_seen refresh ──


def test_touch_device_refreshes_last_seen_and_online():
    sync = CrossDeviceSync()
    dev = _device()
    old = time.time() - 60  # registered 60s ago, past the 30s window
    dev.last_seen = old
    dev.status = DeviceStatus.OFFLINE
    sync._devices[dev.device_id] = dev

    sync._touch_device("peer_1")

    assert dev.last_seen > old, "收消息应刷新 last_seen"  # i18n: user-facing
    assert dev.status == DeviceStatus.ONLINE, "收消息应置 ONLINE"  # i18n: user-facing
    assert dev in sync.get_online_devices(), "刷新后应回到在线列表"  # i18n: user-facing


def test_touch_device_unknown_sender_noop():
    sync = CrossDeviceSync()
    sync._touch_device("ghost")  # unregistered device
    assert "ghost" not in sync._devices


def test_touch_device_empty_sender_noop():
    sync = CrossDeviceSync()
    sync._touch_device("")


def test_handle_message_refreshes_sender_last_seen():
    sync = CrossDeviceSync(token=None)
    dev = _device()
    old = time.time() - 60
    dev.last_seen = old
    sync._devices[dev.device_id] = dev

    asyncio.run(sync._handle_message({"msg_type": "status_update", "sender": "peer_1", "payload": {}}))

    assert dev.last_seen > old, "_handle_message 应经 _touch_device 刷新 last_seen"  # i18n: user-facing


# ── _send_to_device retry ──


@pytest.mark.asyncio
async def test_send_to_device_retries_on_unreachable():
    # port=1 (no listener) -> OSError -> should retry 3 times then return False (not drop on first failure)
    sync = CrossDeviceSync()
    dev = _device(port=1)
    sync._devices[dev.device_id] = dev
    msg = SyncMessage(
        msg_id="m1",
        msg_type="status_update",
        sender="me",
        receiver="peer_1",
        payload={},
    )
    t0 = time.time()
    ok = await sync._send_to_device(dev, msg)
    elapsed = time.time() - t0
    assert ok is False, "不可达设备最终应返 False"  # i18n: user-facing
    # 3 retries: backoff 0.5 + 1.0 = 1.5s minimum wait (3rd failure does not back off)
    assert elapsed >= 1.4, f"应经重试退避 (>=1.4s), 实际 {elapsed:.2f}s"  # i18n: user-facing


@pytest.mark.asyncio
async def test_send_to_device_no_retry_on_nontransient(monkeypatch):
    # Non-transient exceptions are not retried, return False directly (no backoff wait)
    sync = CrossDeviceSync()
    dev = _device(port=1)
    sync._devices[dev.device_id] = dev
    msg = SyncMessage(msg_id="m1", msg_type="x", sender="me", receiver="peer_1", payload={})

    async def _boom(*a, **kw):
        raise ValueError("non-transient")

    monkeypatch.setattr(asyncio, "open_connection", _boom)
    t0 = time.time()
    ok = await sync._send_to_device(dev, msg)
    elapsed = time.time() - t0
    assert ok is False
    assert elapsed < 0.5, "非瞬态异常不应重试退避"  # i18n: user-facing


# ── start() half-dead state ──


@pytest.mark.asyncio
async def test_start_oserror_rolls_back_running_and_raises():
    # Bind to an occupied port -> websockets.serve raises OSError -> _running should roll back to False + raise
    # websockets is an optional [web] extra — skip when CI does not install it (not behavior of this patch)
    pytest.importorskip("websockets")
    import websockets

    occupied = websockets.serve(lambda ws: None, "127.0.0.1", 0)
    server_a = await occupied
    sock = server_a.sockets[0]
    used_port = sock.getsockname()[1]

    sync_b = CrossDeviceSync(host="127.0.0.1", port=used_port)
    assert sync_b._running is False
    with pytest.raises(OSError):
        await sync_b.start()
    assert sync_b._running is False, "OSError 后 _running 应回滚 False (非半死态)"  # i18n: user-facing
    assert sync_b._ws_server is None
    server_a.close()
    await server_a.wait_closed()
