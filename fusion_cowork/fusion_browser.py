import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .vendor.fusion_browser_client import FBError, FusionBrowserClient, FusionBrowserStream

logger = logging.getLogger(__name__)

BINARY_ENV = "FUSION_BROWSER_BIN"
ORIGINS_ENV = "FUSION_BROWSER_ALLOWED_ORIGINS"
STATE_ROOT_ENV = "FUSION_COWORK_BROWSER_STATE"
DEFAULT_BINARY_CANDIDATES = (
    Path.home() / "fusion" / "fusion-browser" / ".build" / "release" / "fusion-browser",
    Path.home() / "claude-home" / "fusion-browser" / ".build" / "release" / "fusion-browser",
)
DEFAULT_STATE_ROOT = Path.home() / ".fusion-cowork" / "fusion-browser"
SOCKET_WAIT_SECONDS = 15.0
TERMINATE_WAIT_SECONDS = 5.0
MAX_SOCKET_PATH = 100


class FusionBrowserError(RuntimeError):
    pass


def find_fusion_browser_binary() -> str:
    override = os.environ.get(BINARY_ENV)
    if override:
        if not Path(override).is_file():
            raise FusionBrowserError(f"fusion-browser 二进制不存在: {override}")
        return override
    for candidate in DEFAULT_BINARY_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    searched = ", ".join(str(c) for c in DEFAULT_BINARY_CANDIDATES)
    raise FusionBrowserError(f"未找到 fusion-browser 二进制 (查过 {searched}); 可设 {BINARY_ENV}")


def _origin_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        origin += f":{parsed.port}"
    return origin


def resolve_allowed_origins(url: Optional[str] = None) -> list[str]:
    origins: list[str] = []
    raw = os.environ.get(ORIGINS_ENV, "")
    for item in raw.split(","):
        item = item.strip()
        if item:
            origins.append(item)
    derived = _origin_of(url)
    if derived and derived not in origins:
        origins.append(derived)
    return origins


class FusionBrowserManaged:
    def __init__(
        self,
        binary: Optional[str] = None,
        allowed_origins: Optional[list[str]] = None,
        state_root: Optional[Path] = None,
        mode: str = "headless",
    ):
        self.binary = binary
        self.allowed_origins = list(allowed_origins or [])
        self.state_root = Path(state_root) if state_root else Path(os.environ.get(STATE_ROOT_ENV, DEFAULT_STATE_ROOT))
        self.mode = mode
        self.token = ""
        self.socket_path = ""
        self.config_path: Optional[Path] = None
        self.run_dir: Optional[Path] = None
        self.proc: Optional[subprocess.Popen] = None
        self._client: Optional[FusionBrowserClient] = None
        self.sessions: dict[str, dict] = {}
        self.profiles: dict[str, str] = {}
        self._load_profiles()

    def _profiles_file(self) -> Path:
        return self.state_root / "profiles.json"

    def _load_profiles(self) -> None:
        path = self._profiles_file()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.profiles = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        except (OSError, ValueError) as e:
            logger.warning(f"fusion-browser profile 状态读取失败 (忽略): {e}")

    def _save_profiles(self) -> None:
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            tmp = self._profiles_file().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.profiles, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._profiles_file())
        except OSError as e:
            logger.warning(f"fusion-browser profile 状态写入失败: {e}")

    def profile_id_for(self, run_id: str) -> str:
        return self.profiles.get(run_id) or f"run-{run_id}"

    def status(self) -> dict[str, Any]:
        running = self.proc is not None and self.proc.poll() is None
        return {
            "running": running,
            "pid": self.proc.pid if running and self.proc else None,
            "socket_path": self.socket_path or None,
            "binary": self.binary,
            "mode": self.mode,
            "sessions": {sid: dict(meta) for sid, meta in self.sessions.items()},
            "profiles": dict(self.profiles),
            "allowed_origins": list(self.allowed_origins),
        }

    def launch(self, url: Optional[str] = None) -> dict[str, Any]:
        if self.proc is not None and self.proc.poll() is None:
            return self.status()
        self.binary = self.binary or find_fusion_browser_binary()
        if not Path(self.binary).is_file():
            raise FusionBrowserError(f"fusion-browser 二进制不存在: {self.binary}")
        for origin in resolve_allowed_origins(url):
            if origin not in self.allowed_origins:
                self.allowed_origins.append(origin)
        self.state_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix=f"{uuid.uuid4().hex[:10]}-", dir=self.state_root))
        socket_path = run_dir / "engine.sock"
        if len(str(socket_path)) > MAX_SOCKET_PATH:
            run_dir = Path(tempfile.mkdtemp(prefix="fc-fb-", dir="/tmp"))
            socket_path = run_dir / "engine.sock"
        self.run_dir = run_dir
        self.socket_path = str(socket_path)
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        self.config_path = run_dir / "config.json"
        config = {
            "socketPath": self.socket_path,
            "authToken": self.token,
            "tokenCapabilities": ["all"],
            "allowedOrigins": list(self.allowed_origins),
            "logLevel": os.environ.get("FUSION_BROWSER_LOG_LEVEL", "info"),
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        log_path = run_dir / "engine.log"
        with open(log_path, "ab") as log_file:
            self.proc = subprocess.Popen(
                [self.binary],
                env={**os.environ, "FUSION_BROWSER_CONFIG": str(self.config_path)},
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        logger.info(f"fusion-browser 启动 pid={self.proc.pid} socket={self.socket_path} log={log_path}")
        try:
            self._wait_socket()
        except FusionBrowserError:
            self._terminate_proc()
            shutil.rmtree(run_dir, ignore_errors=True)
            self.run_dir = None
            raise
        self._client = FusionBrowserClient(socket_path=self.socket_path, token=self.token)
        return self.status()

    def _wait_socket(self) -> None:
        deadline = time.monotonic() + SOCKET_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise FusionBrowserError(f"fusion-browser 提前退出 (code {self.proc.returncode})")
            if Path(self.socket_path).exists():
                client = FusionBrowserClient(socket_path=self.socket_path, token=self.token, timeout=5.0)
                try:
                    client.metrics()
                    client.close()
                    logger.info("fusion-browser socket 就绪并且鉴权通过")
                    return
                except FBError as e:
                    if e.code not in ("connection_closed", "internal_error"):
                        raise FusionBrowserError(f"fusion-browser 鉴权失败: {e}") from e
                    client.close()
            time.sleep(0.05)
        raise FusionBrowserError(f"fusion-browser socket 在 {SOCKET_WAIT_SECONDS}s 内未就绪: {self.socket_path}")

    def _require_client(self) -> FusionBrowserClient:
        if self._client is None:
            if not self.socket_path:
                raise FusionBrowserError("fusion-browser 未启动, 先调用 launch")
            self._client = FusionBrowserClient(socket_path=self.socket_path, token=self.token)
        return self._client

    def _call(self, label: str, func, *args, **kwargs):
        self._require_client()
        try:
            return func(*args, **kwargs)
        except FBError as e:
            logger.error(f"fusion-browser {label} 失败 code={e.code} retryable={e.retryable}: {e.message}")
            raise FusionBrowserError(f"{label} 失败: {e.code}: {e.message}") from e

    def create_session(
        self,
        run_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        initial_url: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> dict[str, Any]:
        if self.proc is not None and self.proc.poll() is not None:
            raise FusionBrowserError(f"fusion-browser 进程已退出 (code {self.proc.returncode})")
        if not self.socket_path:
            raise FusionBrowserError("fusion-browser 未运行, 先调用 launch")
        if profile_id is None and run_id:
            profile_id = self.profile_id_for(run_id)
        session_id = self._call(
            "create_session",
            self._require_client().create_session,
            mode=mode or self.mode,
            initial_url=initial_url,
            profile_id=profile_id,
        )
        meta = {
            "session_id": session_id,
            "run_id": run_id,
            "profile_id": profile_id,
            "initial_url": initial_url,
            "cookies": None,
        }
        self.sessions[session_id] = meta
        if run_id and profile_id:
            self.profiles[run_id] = profile_id
            self._save_profiles()
        logger.info(f"fusion-browser 会话创建 session={session_id} profile={profile_id} run={run_id}")
        return dict(meta)

    def import_cookies(self, session_id: str, browser: str, profile: Optional[str] = None) -> dict[str, Any]:
        payload = self._call(
            "import_cookies",
            self._require_client().import_cookies,
            session_id,
            browser,
            profile=profile,
        )
        counters = {
            "browser": payload.get("browser", browser),
            "imported": int(payload.get("imported", 0)),
            "skipped_app_bound": int(payload.get("skipped_app_bound", 0)),
            "skipped_other": int(payload.get("skipped_other", 0)),
        }
        meta = self.sessions.get(session_id)
        if meta is not None:
            meta["cookies"] = counters
        logger.info(
            f"fusion-browser cookie 导入 browser={counters['browser']} imported={counters['imported']} "
            f"skipped_app_bound={counters['skipped_app_bound']} skipped_other={counters['skipped_other']}"
        )
        return counters

    def execute(
        self,
        session_id: str,
        action: str,
        target_node_id: Optional[str] = None,
        payload_text: Optional[str] = None,
    ) -> dict[str, Any]:
        if action not in ("navigate", "click", "type_text", "scroll", "screenshot", "evaluate", "state", "close"):
            raise FusionBrowserError(f"未知 action: {action}")
        info = self._call(
            f"execute:{action}",
            self._require_client().execute,
            session_id,
            action,
            target_node_id=target_node_id,
            payload_text=payload_text,
        )
        return {
            "session_id": info.sessionId,
            "url": info.url,
            "title": info.title,
            "ax_tree_markdown": info.axTreeMarkdown,
            "interactive_nodes": [
                {
                    "node_id": n.nodeId,
                    "role": n.role,
                    "name": n.name,
                    "current_value": n.currentValue,
                    "bbox": None
                    if n.bbox is None
                    else {
                        "x": n.bbox.x,
                        "y": n.bbox.y,
                        "width": n.bbox.width,
                        "height": n.bbox.height,
                    },
                }
                for n in info.interactiveNodes
            ],
            "screenshot_png": None if info.screenshotPng is None else len(info.screenshotPng),
            "evaluate_result": info.evaluateResult,
            "execution_time_ms": info.executionTimeMs,
        }

    def screenshot_png(self, session_id: str) -> bytes:
        info = self._call("screenshot", self._require_client().screenshot, session_id)
        if info.screenshotPng is None:
            raise FusionBrowserError("fusion-browser 截图返回空")
        return info.screenshotPng

    def stream_frames(self, session_id: str):
        if not self.socket_path:
            raise FusionBrowserError("fusion-browser 未启动, 先调用 launch")
        stream = FusionBrowserStream(session_id, socket_path=self.socket_path, token=self.token)
        logger.info(f"fusion-browser 面板流订阅 session={session_id}")
        return stream.frames()

    def close_session(self, session_id: str) -> bool:
        self._call("close_session", self._require_client().close_session, session_id)
        self.sessions.pop(session_id, None)
        logger.info(f"fusion-browser 会话关闭 session={session_id}")
        return True

    def delete_profile(self, profile_id: str) -> bool:
        deleted = self._call("delete_profile", self._require_client().delete_profile, profile_id)
        for run_id, stored in list(self.profiles.items()):
            if stored == profile_id:
                self.profiles.pop(run_id, None)
        self._save_profiles()
        logger.info(f"fusion-browser profile 删除 profile={profile_id} deleted={deleted}")
        return bool(deleted)

    def finish(self, run_id: Optional[str] = None, delete_profile: bool = True) -> dict[str, Any]:
        closed: list[str] = []
        for session_id, meta in list(self.sessions.items()):
            if run_id is not None and meta.get("run_id") != run_id:
                continue
            try:
                self.close_session(session_id)
                closed.append(session_id)
            except FusionBrowserError as e:
                logger.warning(f"fusion-browser 会话关闭失败 session={session_id}: {e}")
        deleted: list[str] = []
        if delete_profile:
            targets = []
            if run_id is not None and run_id in self.profiles:
                targets.append(self.profiles[run_id])
            elif run_id is None:
                targets = sorted(set(self.profiles.values()))
            for profile_id in targets:
                try:
                    if self.delete_profile(profile_id):
                        deleted.append(profile_id)
                except FusionBrowserError as e:
                    logger.warning(f"fusion-browser profile 删除失败 profile={profile_id}: {e}")
        shutting_down = run_id is None
        if shutting_down:
            self.close()
        return {"closed_sessions": closed, "deleted_profiles": deleted, "engine_running": self.proc is not None}

    def _terminate_proc(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError as e:
                logger.warning(f"fusion-browser SIGTERM 发送失败: {e}")
            try:
                proc.wait(timeout=TERMINATE_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning("fusion-browser SIGTERM 超时, 改发 SIGKILL")
                proc.kill()
                proc.wait(timeout=TERMINATE_WAIT_SECONDS)

    def close(self) -> dict[str, Any]:
        if self._client is not None:
            try:
                self._client.close()
            except OSError as e:
                logger.warning(f"fusion-browser 连接关闭失败: {e}")
            self._client = None
        self._terminate_proc()
        if self.run_dir is not None and self.run_dir.is_dir():
            shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir = None
        self.sessions.clear()
        logger.info("fusion-browser 引擎已停止")
        return self.status()


_managed: Optional[FusionBrowserManaged] = None


def get_fusion_browser() -> FusionBrowserManaged:
    global _managed
    if _managed is None:
        _managed = FusionBrowserManaged()
    return _managed


def reset_fusion_browser() -> None:
    global _managed
    if _managed is not None:
        _managed.close()
    _managed = None
