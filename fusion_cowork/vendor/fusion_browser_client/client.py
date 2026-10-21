"""fusion-browser UDS JSON-RPC client implementation.

Length-prefixed JSON framing ([u32 BE len][JSON], snake_case keys). One frame per
request-response; the server closes after an error. Auth handshake is the first
frame on every fresh connection (the engine closes after one op when driven by a
per-call-dial proxy, so re-auth each connect).

Logging: module-level `logging.getLogger("fusion_browser_client")` — callers
configure handlers/level. Default no handler = silent under library use.
"""

import json
import logging
import socket
import struct
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("fusion_browser_client")

_DEFAULT_SOCKET = "/tmp/fusion-browser.sock"
_HEADER = struct.Struct(">I")


class FBError(Exception):
    """Structured engine error: {code, message, retryable}."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable

    @classmethod
    def from_payload(cls, payload: dict) -> "FBError":
        return cls(
            code=payload.get("code", "unknown"),
            message=payload.get("message", ""),
            retryable=bool(payload.get("retryable", False)),
        )


@dataclass
class BBox:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["BBox"]:
        if not d:
            return None
        return cls(
            x=float(d.get("x", 0)),
            y=float(d.get("y", 0)),
            width=float(d.get("width", 0)),
            height=float(d.get("height", 0)),
        )


@dataclass
class AXTreeNode:
    nodeId: str
    role: str
    name: str
    isDisabled: bool = False
    currentValue: str = ""
    bbox: Optional[BBox] = None

    @classmethod
    def from_dict(cls, d: dict) -> "AXTreeNode":
        return cls(
            nodeId=d.get("node_id", "") or d.get("nodeId", ""),
            role=d.get("role", ""),
            name=d.get("name", ""),
            isDisabled=bool(d.get("is_disabled", d.get("isDisabled", False))),
            currentValue=d.get("current_value", d.get("currentValue", "")),
            bbox=BBox.from_dict(d.get("bbox")),
        )


@dataclass
class SessionInfo:
    """Result of create_session + a subsequent state extract."""

    sessionId: str
    url: str = ""
    title: str = ""
    axTreeMarkdown: str = ""
    interactiveNodes: list = field(default_factory=list)
    screenshotPng: Optional[bytes] = None
    hasSecurityInjectionBlocked: bool = False
    executionTimeMs: int = 0
    sessionRecovered: bool = False
    evaluateResult: Optional[str] = None

    @classmethod
    def from_state(cls, d: dict) -> "SessionInfo":
        png = d.get("screenshot_png", d.get("screenshotPng"))
        png_bytes = None
        if png:
            import base64
            try:
                png_bytes = base64.b64decode(png)
            except Exception:
                png_bytes = None
        nodes = [AXTreeNode.from_dict(n) for n in d.get("interactive_nodes", d.get("interactiveNodes", []))]
        return cls(
            sessionId=d.get("session_id", d.get("sessionId", "")),
            url=d.get("url", ""),
            title=d.get("title", ""),
            axTreeMarkdown=d.get("ax_tree_markdown", d.get("axTreeMarkdown", "")),
            interactiveNodes=nodes,
            screenshotPng=png_bytes,
            hasSecurityInjectionBlocked=bool(d.get("has_security_injection_blocked", d.get("hasSecurityInjectionBlocked", False))),
            executionTimeMs=int(d.get("execution_time_ms", d.get("executionTimeMs", 0))),
            sessionRecovered=bool(d.get("session_recovered", d.get("sessionRecovered", False))),
            evaluateResult=d.get("evaluate_result", d.get("evaluateResult")),
        )


@dataclass
class CapacityInfo:
    nodeId: str
    maxSessions: int
    liveSessions: int
    maxTotalMemoryMb: int
    freeMemoryMb: int
    ramGb: int

    @classmethod
    def from_dict(cls, d: dict) -> "CapacityInfo":
        return cls(
            nodeId=d.get("node_id", ""),
            maxSessions=int(d.get("max_sessions", 0)),
            liveSessions=int(d.get("live_sessions", 0)),
            maxTotalMemoryMb=int(d.get("max_total_memory_mb", 0)),
            freeMemoryMb=int(d.get("free_memory_mb", 0)),
            ramGb=int(d.get("ram_gb", 0)),
        )


class FusionBrowserClient:
    """Synchronous UDS client. One connection per instance; reconnects on demand.

    The engine is per-call-dial friendly (a proxy may dial fresh per op), but this
    client keeps ONE persistent connection for its lifetime — the auth handshake
    runs once on connect. For proxy/per-call use, create a client per op or call
    `close()` + the next method (auto-reconnect).
    """

    def __init__(self, socket_path: str = _DEFAULT_SOCKET, token: Optional[str] = None, timeout: float = 30.0):
        self.socket_path = socket_path
        self.token = token
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._authed = False

    # -- framing -----------------------------------------------------------

    @staticmethod
    def _send(sock: socket.socket, obj: dict) -> None:
        data = json.dumps(obj).encode()
        sock.sendall(_HEADER.pack(len(data)) + data)

    # P-1 (1016 audit): hard cap on a single frame body. The engine never sends a frame
    # this large (AXTree markdown of a pathological page is ~1MB; screenshots ~2MB); a
    # bigger decoded length means a desynced/garbage stream — fail loud and fast instead
    # of blocking until the socket timeout trying to read e.g. 3GB.
    _MAX_FRAME = 64 * 1024 * 1024

    @staticmethod
    def _recv_one(sock: socket.socket) -> Optional[dict]:
        hdr = b""
        while len(hdr) < 4:
            chunk = sock.recv(4 - len(hdr))
            if not chunk:
                return None
            hdr += chunk
        (length,) = _HEADER.unpack(hdr)
        if length > FusionBrowserClient._MAX_FRAME:
            raise FBError("connection_closed",
                          "frame length %d exceeds cap (desynced stream)" % length)
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)

    # -- connection / auth -------------------------------------------------

    def _connect(self) -> None:
        if self._sock is not None:
            return
        log.debug("connect socket=%s", self.socket_path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.socket_path)
        self._sock = s
        try:
            self._auth()
        # P-1: an auth-handshake transport fault also leaves the connection unusable —
        # close so the next op auto-reconnects instead of reading a desynced stream.
        except (socket.timeout, OSError) as e:
            self.close()
            raise FBError("connection_closed",
                          "transport fault: %s" % type(e).__name__) from e

    def _auth(self) -> None:
        if self._authed or self._sock is None:
            return
        self._send(self._sock, {"type": "auth", "token": self.token})
        ack = self._recv_one(self._sock)
        if not ack or ack.get("type") != "auth_ack":
            self.close()
            payload = (ack or {}).get("payload", {}) if ack else {}
            # P-2 (1016 audit): the old code assumed any non-ack frame was a server error
            # frame and passed its payload to from_payload — a non-error frame type
            # produced a fabricated "unknown" code that hid what actually happened. Only
            # a real error frame maps through from_payload; anything else is honestly
            # "unexpected frame type".
            if ack is not None and ack.get("type") == "error":
                raise FBError.from_payload(payload)
            raise FBError("auth_denied",
                          "unexpected frame type: %s" % (ack or {}).get("type", "none"))
        self._authed = True
        log.debug("auth_ack received")

    def _request(self, frame: dict) -> dict:
        self._connect()
        assert self._sock is not None
        try:
            self._send(self._sock, frame)
            resp = self._recv_one(self._sock)
        # P-1 (1016 audit): a socket timeout or OSError mid-request leaves the connection
        # in an unknown/desynced state. The old code let the raw exception propagate with
        # the connection kept, so the NEXT call on this client read garbage (the previous
        # response's tail parsed as the next header). Kill the connection on any transport
        # fault; the next op auto-reconnects.
        except (socket.timeout, OSError) as e:
            self.close()
            raise FBError("connection_closed",
                          "transport fault: %s" % type(e).__name__) from e
        if resp is None:
            self.close()
            raise FBError("connection_closed", "server closed mid-frame")
        if resp.get("type") == "error":
            self.close()
            raise FBError.from_payload(resp.get("payload", {}))
        return resp

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._authed = False

    # -- ops ---------------------------------------------------------------

    def create_session(self, mode: str = "headless", initial_url: Optional[str] = None,
                       credentials: Optional[list] = None, profile_id: Optional[str] = None) -> str:
        payload: dict = {"mode": mode}
        if initial_url is not None:
            payload["initial_url"] = initial_url
        if credentials is not None:
            payload["credentials"] = credentials
        # Issue #25: a profile_id keeps this session's cookie/login state in a persistent
        # WKWebsiteDataStore, so a workflow run can resume a login across engine restarts.
        if profile_id is not None:
            payload["profile_id"] = profile_id
        resp = self._request({"type": "create_session", "payload": payload})
        cr = resp.get("payload", {})
        sid = cr.get("session_id")
        if not sid:
            raise FBError("internal_error", "create_session returned no session_id")
        log.info("create_session id=%s credential_injected=%s", sid,
                 cr.get("credential_injected", cr.get("credentialInjected")))
        return sid

    def execute(self, session_id: str, action: str, target_node_id: Optional[str] = None,
                payload_text: Optional[str] = None, trace_id: Optional[str] = None) -> SessionInfo:
        p: dict = {"session_id": session_id, "action": action}
        if target_node_id is not None:
            p["target_node_id"] = target_node_id
        if payload_text is not None:
            p["payload_text"] = payload_text
        if trace_id is not None:
            p["trace_id"] = trace_id
        resp = self._request({"type": "execute", "payload": p})
        return SessionInfo.from_state(resp.get("payload", {}))

    def navigate(self, session_id: str, url: str) -> SessionInfo:
        return self.execute(session_id, action="navigate", payload_text=url)

    def click(self, session_id: str, node_id: str) -> SessionInfo:
        return self.execute(session_id, action="click", target_node_id=node_id)

    def type_text(self, session_id: str, node_id: str, text: str) -> SessionInfo:
        return self.execute(session_id, action="type_text", target_node_id=node_id, payload_text=text)

    def scroll(self, session_id: str, payload_text: Optional[str] = None) -> SessionInfo:
        return self.execute(session_id, action="scroll", payload_text=payload_text)

    def screenshot(self, session_id: str) -> SessionInfo:
        return self.execute(session_id, action="screenshot")

    def evaluate(self, session_id: str, script: str) -> SessionInfo:
        return self.execute(session_id, action="evaluate", payload_text=script)

    def close_session(self, session_id: str) -> None:
        self._request({"type": "close", "session_id": session_id})
        log.info("close_session id=%s", session_id)

    def metrics(self) -> dict:
        resp = self._request({"type": "metrics"})
        return resp.get("payload", {})

    def capacity(self) -> CapacityInfo:
        resp = self._request({"type": "capacity"})
        return CapacityInfo.from_dict(resp.get("payload", {}))

    def import_cookies(self, session_id: str, browser: str,
                       profile: Optional[str] = None) -> dict:
        """Issue #25: import the user's browser cookies into a live session.

        Requires the token's `importCookies` capability. Returns the engine payload:
        {session_id, browser, imported, skipped_app_bound, skipped_other}. Chrome/Edge
        cookies encrypted with v20 app-bound keys cannot be decrypted by any third
        party — those are counted in skipped_app_bound, never imported.
        """
        p: dict = {"session_id": session_id, "browser": browser}
        if profile is not None:
            p["profile"] = profile
        resp = self._request({"type": "import_cookies", "payload": p})
        log.info("import_cookies browser=%s imported=%s", browser,
                 resp.get("payload", {}).get("imported"))
        return resp.get("payload", {})

    def delete_profile(self, profile_id: str) -> bool:
        """Issue #25: delete a persistent profile. True = a profile existed and is gone."""
        resp = self._request({"type": "delete_profile", "payload": {"profile_id": profile_id}})
        return bool(resp.get("payload", {}).get("deleted", False))

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "FusionBrowserClient":
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FusionBrowserStream:
    """Issue #25: screenshot push stream for a web side panel.

    Opens its OWN UDS connection — the command connection must stay free for
    create/execute/close, and the engine's `stream_screenshots` subscription lives
    for the life of that connection (fusion-gateway dials per op, so a proxied
    command connection cannot hold a subscription).

    `frames()` is a generator yielding each `screenshot_frame` event dict (png is
    base64); it returns when the engine sends `stream_end` (session closed) or the
    connection ends. Re-subscribing means a new stream object.
    """

    def __init__(self, session_id: str, socket_path: str = _DEFAULT_SOCKET,
                 token: Optional[str] = None, timeout: Optional[float] = 30.0):
        self.session_id = session_id
        self.socket_path = socket_path
        self.token = token
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._authed = False
        self._subscribed = False

    def _connect(self) -> None:
        if self._sock is not None:
            return
        log.debug("stream connect socket=%s session=%s", self.socket_path, self.session_id)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.socket_path)
        self._sock = s

    def _recv(self) -> Optional[dict]:
        assert self._sock is not None
        try:
            return FusionBrowserClient._recv_one(self._sock)
        except (socket.timeout, OSError) as e:
            self.close()
            raise FBError("connection_closed", "stream transport fault: %s" % type(e).__name__) from e

    def _auth(self) -> None:
        if self._authed or self._sock is None:
            return
        FusionBrowserClient._send(self._sock, {"type": "auth", "token": self.token})
        ack = self._recv()
        if not ack or ack.get("type") != "auth_ack":
            self.close()
            if ack is not None and ack.get("type") == "error":
                raise FBError.from_payload(ack.get("payload", {}))
            raise FBError("auth_denied", "unexpected frame type: %s" % (ack or {}).get("type", "none"))
        self._authed = True

    def _subscribe(self) -> None:
        if self._subscribed or self._sock is None:
            return
        FusionBrowserClient._send(self._sock, {
            "type": "stream_screenshots",
            "payload": {"session_id": self.session_id, "enabled": True}})
        resp = self._recv()
        if resp is None:
            self.close()
            raise FBError("connection_closed", "server closed before stream_ack")
        if resp.get("type") == "error":
            self.close()
            raise FBError.from_payload(resp.get("payload", {}))
        if resp.get("type") != "stream_ack" or not resp.get("payload", {}).get("active"):
            self.close()
            raise FBError("stream_denied",
                          "unexpected subscribe response: %s" % resp.get("type"))
        self._subscribed = True
        # After the ack the stream is idle most of the time — a socket timeout would
        # abort a healthy stream, so block until a frame arrives (close() still unblocks).
        if self._sock is not None:
            self._sock.settimeout(None)

    def frames(self):
        """Yield screenshot_frame event dicts until stream_end or EOF."""
        self._connect()
        self._auth()
        self._subscribe()
        while True:
            ev = self._recv()
            if ev is None:
                log.debug("stream EOF session=%s", self.session_id)
                return
            t = ev.get("type")
            if t == "stream_end":
                log.info("stream_end session=%s reason=%s", self.session_id, ev.get("reason"))
                return
            if t == "error":
                raise FBError.from_payload(ev.get("payload", {}))
            if t == "screenshot_frame":
                yield ev
            else:
                log.debug("stream drop unknown frame type=%s", t)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._authed = False
        self._subscribed = False

    def __enter__(self) -> "FusionBrowserStream":
        self._connect()
        self._auth()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
