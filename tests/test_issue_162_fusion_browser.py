import json
import os
import socket
import struct
import threading

import pytest

import fusion_cowork.nodes
from fusion_cowork.engine.node import NodeRegistry


@pytest.fixture(autouse=True)
def _ensure_nodes_loaded():
    # 跨测试文件 NodeRegistry 可能被 clear(), 每测试前确保内置节点在册
    if "fusion_browser" not in NodeRegistry._registry:
        fusion_cowork.nodes.import_all_nodes()
    yield


HEADER = struct.Struct(">I")


def _send(sock, obj):
    data = json.dumps(obj).encode()
    sock.sendall(HEADER.pack(len(data)) + data)


def _recv_one(sock):
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (length,) = HEADER.unpack(hdr)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body)


class FakeEngine:
    def __init__(self, path, token="test-token"):
        self.path = str(path)
        self.token = token
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.server.settimeout(10)
        self.requests = []
        self._stop = False
        self.profiles_deleted = []
        self.sessions_closed = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop = True
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(self.path)
            probe.close()
        except OSError:
            pass
        self.server.close()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            auth = _recv_one(conn)
            if auth is None:
                return
            if auth.get("type") != "auth" or auth.get("token") != self.token:
                _send(conn, {"type": "error", "payload": {"code": "auth_denied", "message": "bad token"}})
                return
            _send(conn, {"type": "auth_ack", "payload": {"caps": 1023}})
            while True:
                frame = _recv_one(conn)
                if frame is None:
                    return
                self.requests.append(frame)
                if not self._dispatch(conn, frame):
                    return
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, conn, frame):
        t = frame.get("type")
        p = frame.get("payload") or {}
        if t == "create_session":
            _send(conn, {"type": "create_session", "payload": {"session_id": "sess-1", "credential_injected": False}})
        elif t == "execute":
            _send(
                conn,
                {
                    "type": "state",
                    "payload": {
                        "session_id": p.get("session_id"),
                        "url": p.get("payload_text") if p.get("action") == "navigate" else "https://example.com",
                        "title": "Example",
                        "ax_tree_markdown": "- [@e1] button Submit",
                        "interactive_nodes": [
                            {"node_id": "e1", "role": "button", "name": "Submit", "current_value": ""}
                        ],
                        "screenshot_png": "aGVsbG8=",
                        "execution_time_ms": 7,
                    },
                },
            )
        elif t == "import_cookies":
            _send(
                conn,
                {
                    "type": "import_cookies",
                    "payload": {
                        "session_id": p.get("session_id"),
                        "browser": p.get("browser"),
                        "imported": 12,
                        "skipped_app_bound": 3,
                        "skipped_other": 1,
                    },
                },
            )
        elif t == "close":
            self.sessions_closed.append(frame.get("session_id"))
            _send(conn, {"type": "closed", "sessionId": frame.get("session_id")})
        elif t == "delete_profile":
            self.profiles_deleted.append(p.get("profile_id"))
            _send(conn, {"type": "profile_deleted", "payload": {"profile_id": p.get("profile_id"), "deleted": True}})
        elif t == "metrics":
            _send(conn, {"type": "metrics", "payload": {"sessions": 0}})
        elif t == "stream_screenshots":
            _send(
                conn, {"type": "stream_ack", "payload": {"session_id": p.get("session_id"), "active": p.get("enabled")}}
            )
            for seq in (1, 2):
                _send(
                    conn,
                    {
                        "type": "screenshot_frame",
                        "session_id": p.get("session_id"),
                        "seq": seq,
                        "png": "aGVsbG8=",
                        "url": "https://example.com",
                        "title": "Example",
                        "ts_ms": 1761000000 + seq,
                    },
                )
            _send(conn, {"type": "stream_end", "session_id": p.get("session_id"), "reason": "session_closed"})
        else:
            _send(conn, {"type": "error", "payload": {"code": "unknown", "message": t}})
        return True


@pytest.fixture
def engine():
    import tempfile

    sock_dir = tempfile.mkdtemp(prefix="fc-fb-test-")
    server = FakeEngine(os.path.join(sock_dir, "engine.sock"))
    server.start()
    yield server
    server.stop()


def _managed(engine, tmp_path, **kwargs):
    from fusion_cowork.fusion_browser import FusionBrowserManaged

    managed = FusionBrowserManaged(state_root=tmp_path / "state", **kwargs)
    managed.socket_path = engine.path
    managed.token = engine.token
    return managed


class FakeProc:
    instances = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None
        self.terminated = False
        self.killed = False
        FakeProc.instances.append(self)

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.terminated = True
        self.returncode = 0

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_find_binary_env_override(monkeypatch, tmp_path):
    from fusion_cowork import fusion_browser as fb

    fake = tmp_path / "fusion-browser"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(fb.BINARY_ENV, str(fake))
    assert fb.find_fusion_browser_binary() == str(fake)

    monkeypatch.setenv(fb.BINARY_ENV, str(tmp_path / "missing"))
    with pytest.raises(fb.FusionBrowserError):
        fb.find_fusion_browser_binary()


def test_launch_isolated_config_and_env(monkeypatch, tmp_path, engine):
    from fusion_cowork import fusion_browser as fb

    FakeProc.instances.clear()
    fake_bin = tmp_path / "fusion-browser"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(fb.BINARY_ENV, str(fake_bin))
    monkeypatch.setattr(fb.subprocess, "Popen", FakeProc)
    managed = fb.FusionBrowserManaged(state_root=tmp_path / "state")
    monkeypatch.setattr(managed, "_wait_socket", lambda: None)
    info = managed.launch(url="https://login.example.com/signin")
    assert info["running"] is True

    proc = FakeProc.instances[-1]
    assert proc.kwargs["env"]["FUSION_BROWSER_CONFIG"] == str(managed.config_path)
    config = json.loads(managed.config_path.read_text(encoding="utf-8"))
    assert config["authToken"] == managed.token
    assert config["tokenCapabilities"] == ["all"]
    assert "https://login.example.com" in config["allowedOrigins"]
    assert config["socketPath"] == managed.socket_path
    assert (os.stat(managed.config_path).st_mode & 0o777) == 0o600
    assert str(managed.socket_path).startswith(str(managed.run_dir))
    managed.close()


def test_launch_rolls_back_when_socket_never_ready(monkeypatch, tmp_path):
    from fusion_cowork import fusion_browser as fb

    FakeProc.instances.clear()
    fake_bin = tmp_path / "fusion-browser"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(fb.BINARY_ENV, str(fake_bin))
    monkeypatch.setattr(fb.subprocess, "Popen", FakeProc)
    managed = fb.FusionBrowserManaged(state_root=tmp_path / "state")

    def _timeout():
        raise fb.FusionBrowserError("not ready")

    monkeypatch.setattr(managed, "_wait_socket", _timeout)
    with pytest.raises(fb.FusionBrowserError):
        managed.launch()
    assert FakeProc.instances[-1].terminated is True
    assert managed.proc is None
    assert not managed.run_dir or not managed.run_dir.exists()


def test_session_import_execute_and_finish(engine, tmp_path):
    managed = _managed(engine, tmp_path)
    session = managed.create_session(run_id="r1", initial_url="https://example.com/login")
    assert session["session_id"] == "sess-1"
    assert session["profile_id"] == "run-r1"
    assert managed.profiles == {"r1": "run-r1"}

    counters = managed.import_cookies("sess-1", "firefox")
    assert counters == {"browser": "firefox", "imported": 12, "skipped_app_bound": 3, "skipped_other": 1}
    assert managed.sessions["sess-1"]["cookies"] == counters

    state = managed.execute("sess-1", "navigate", payload_text="https://example.com/form")
    assert state["url"] == "https://example.com/form"
    assert state["interactive_nodes"][0]["node_id"] == "e1"
    assert state["screenshot_png"] == 5

    png = managed.screenshot_png("sess-1")
    assert png == b"hello"

    result = managed.finish("r1")
    assert result["closed_sessions"] == ["sess-1"]
    assert engine.sessions_closed == ["sess-1"]
    assert engine.profiles_deleted == ["run-r1"]
    assert "r1" not in managed.profiles


def test_profiles_reload_across_engine_restart(engine, tmp_path):
    from fusion_cowork.fusion_browser import FusionBrowserManaged

    managed = _managed(engine, tmp_path)
    managed.create_session(run_id="resume-me")
    again = FusionBrowserManaged(state_root=tmp_path / "state")
    assert again.profile_id_for("resume-me") == "run-resume-me"
    assert again.profile_id_for("fresh-run") == "run-fresh-run"


def test_stream_frames_two_frames_then_end(engine, tmp_path):
    managed = _managed(engine, tmp_path)
    frames = list(managed.stream_frames("sess-1"))
    assert [f["seq"] for f in frames] == [1, 2]
    assert frames[0]["png"] == "aGVsbG8="


def test_execute_rejects_unknown_action(engine, tmp_path):
    from fusion_cowork.fusion_browser import FusionBrowserError

    managed = _managed(engine, tmp_path)
    with pytest.raises(FusionBrowserError):
        managed.execute("sess-1", "rm -rf")


def test_node_requires_session_id(monkeypatch):
    import asyncio

    from fusion_cowork import fusion_browser as fb
    from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
    from fusion_cowork.nodes import import_all_nodes

    import_all_nodes()
    fake = fb.FusionBrowserManaged()
    monkeypatch.setattr(fb, "get_fusion_browser", lambda: fake)
    node = NodeRegistry.create(
        "fusion_browser",
        config=NodeConfig(params={"action": "import_cookies", "browser": "firefox"}),
    )
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.FAILED
    assert "session_id" in result.error


def test_node_import_cookies_reports_honest_counters(engine, tmp_path, monkeypatch):
    import asyncio

    from fusion_cowork import fusion_browser as fb
    from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
    from fusion_cowork.nodes import import_all_nodes

    import_all_nodes()
    managed = _managed(engine, tmp_path)
    monkeypatch.setattr(fb, "get_fusion_browser", lambda: managed)
    node = NodeRegistry.create(
        "fusion_browser",
        config=NodeConfig(params={"action": "import_cookies", "session_id": "sess-1", "browser": "firefox"}),
    )
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS
    assert result.data["imported"] == 12
    assert result.data["skipped_app_bound"] == 3
    assert "skipped_app_bound 3" in result.summary


class FakeManaged:
    socket_path = "/tmp/fake.sock"
    allowed_origins = []

    def __init__(self):
        self.frames = [
            {"seq": 1, "png": "aGVsbG8=", "url": "https://example.com", "title": "Example", "ts_ms": 1},
            {"seq": 2, "png": "aGVsbG8=", "url": "https://example.com", "title": "Example", "ts_ms": 2},
        ]

    def status(self):
        return {"running": True, "socket_path": self.socket_path}

    def stream_frames(self, session_id):
        return iter(self.frames)


class TestFusionBrowserWebApi:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "fc-test-162")
        web_api._WEB_DIR = tmp_path / "web"
        app = web_api.create_web_app(
            engine=WorkflowEngine(),
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
        )
        return TestClient(app, headers={"x-api-key": "fc-test-162"})

    def test_status_uses_singleton(self, client, monkeypatch):
        from fusion_cowork import fusion_browser as fb

        monkeypatch.setattr(fb, "get_fusion_browser", lambda: FakeManaged())
        resp = client.get("/api/fusion-browser/status")
        assert resp.status_code == 200
        assert resp.json()["running"] is True

    def test_screenshot_requires_session(self, client):
        resp = client.get("/api/fusion-browser/screenshot")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "missing_session"

    def test_stream_sse_frames(self, client, monkeypatch):
        from fusion_cowork import fusion_browser as fb

        monkeypatch.setattr(fb, "get_fusion_browser", lambda: FakeManaged())
        with client.stream("GET", "/api/fusion-browser/stream?session_id=s1") as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        assert "event: screenshot_frame" in body
        assert '"seq": 2' in body
        assert "event: stream_end" in body

    def test_stream_409_when_engine_not_running(self, client, monkeypatch):
        from fusion_cowork import fusion_browser as fb

        fake = FakeManaged()
        fake.socket_path = ""
        monkeypatch.setattr(fb, "get_fusion_browser", lambda: fake)
        resp = client.get("/api/fusion-browser/stream?session_id=s1")
        assert resp.status_code == 409
