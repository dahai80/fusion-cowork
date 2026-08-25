"""R-7 跨设备同步加固测试 — last_seen 刷新 + _send_to_device 重试 + start() 半死态修复。"""

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


# ── last_seen 刷新 ──


def test_touch_device_refreshes_last_seen_and_online():
    sync = CrossDeviceSync()
    dev = _device()
    old = time.time() - 60  # 60s 前注册, 已超 30s 窗口
    dev.last_seen = old
    dev.status = DeviceStatus.OFFLINE
    sync._devices[dev.device_id] = dev

    sync._touch_device("peer_1")

    assert dev.last_seen > old, "收消息应刷新 last_seen"
    assert dev.status == DeviceStatus.ONLINE, "收消息应置 ONLINE"
    assert dev in sync.get_online_devices(), "刷新后应回到在线列表"


def test_touch_device_unknown_sender_noop():
    sync = CrossDeviceSync()
    sync._touch_device("ghost")  # 未注册设备
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

    assert dev.last_seen > old, "_handle_message 应经 _touch_device 刷新 last_seen"


# ── _send_to_device 重试 ──


@pytest.mark.asyncio
async def test_send_to_device_retries_on_unreachable():
    # port=1 (无监听) → OSError → 应重试 3 次后返 False (非一次失败即丢)
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
    assert ok is False, "不可达设备最终应返 False"
    # 3 次重试: 退避 0.5 + 1.0 = 1.5s 最小等待 (第3次失败不退避)
    assert elapsed >= 1.4, f"应经重试退避 (>=1.4s), 实际 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_send_to_device_no_retry_on_nontransient(monkeypatch):
    # 非瞬态异常不重试, 直接返 False (无退避等待)
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
    assert elapsed < 0.5, "非瞬态异常不应重试退避"


# ── start() 半死态 ──


@pytest.mark.asyncio
async def test_start_oserror_rolls_back_running_and_raises():
    # 绑定到已占用端口 → websockets.serve 抛 OSError → _running 应回滚 False + 抛出
    import websockets

    occupied = websockets.serve(lambda ws: None, "127.0.0.1", 0)
    server_a = await occupied
    sock = server_a.sockets[0]
    used_port = sock.getsockname()[1]

    sync_b = CrossDeviceSync(host="127.0.0.1", port=used_port)
    assert sync_b._running is False
    with pytest.raises(OSError):
        await sync_b.start()
    assert sync_b._running is False, "OSError 后 _running 应回滚 False (非半死态)"
    assert sync_b._ws_server is None
    server_a.close()
    await server_a.wait_closed()
