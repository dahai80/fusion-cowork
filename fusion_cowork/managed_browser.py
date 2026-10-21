from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path.home() / ".fusion-cowork" / "browser-profile"
CHROMIUM_ENV = "FUSION_COWORK_CHROMIUM"

_MAC_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

_PATH_CANDIDATES = ["google-chrome", "chromium", "chromium-browser", "chrome", "microsoft-edge", "brave-browser"]


class ManagedBrowserError(RuntimeError):
    pass


@dataclass
class ManagedBrowserState:
    port: int
    profile_dir: str
    pid: int
    url: str
    headless: bool
    started_at: float = field(default_factory=time.time)
    cookies_imported: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "running": True,
            "port": self.port,
            "profile_dir": self.profile_dir,
            "pid": self.pid,
            "url": self.url,
            "headless": self.headless,
            "started_at": self.started_at,
            "cookies_imported": self.cookies_imported,
        }


def find_chromium_binary() -> str:
    override = os.environ.get(CHROMIUM_ENV, "").strip()
    if override:
        if Path(override).is_file():
            return override
        raise ManagedBrowserError(f"{CHROMIUM_ENV} 指向的文件不存在: {override}")
    for candidate in _MAC_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in _PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    raise ManagedBrowserError(
        "未找到 Chromium/Chrome 可执行文件 — 安装 Chrome 或设置 "
        f"{CHROMIUM_ENV}=<binary path> (例: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse_cookie_file(path: Path) -> List[Dict[str, Any]]:
    cookies: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, cpath, secure, expires, name, value = parts[:7]
        try:
            expires_at = float(expires)
        except ValueError:
            expires_at = -1.0
        cookie: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": cpath or "/",
            "secure": secure.upper() == "TRUE",
        }
        if expires_at > 0:
            cookie["expires"] = expires_at
        cookies.append(cookie)
    return cookies


class ManagedBrowser:
    def __init__(
        self,
        binary: str = "",
        profile_dir: Optional[Path] = None,
        launch_timeout: float = 20.0,
        poll_interval: float = 0.25,
    ) -> None:
        self.binary = binary
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR).expanduser()
        self.launch_timeout = launch_timeout
        self.poll_interval = poll_interval
        self._proc: Optional[subprocess.Popen] = None
        self._state: Optional[ManagedBrowserState] = None

    @property
    def state(self) -> Optional[ManagedBrowserState]:
        return self._state

    def status(self) -> Dict[str, Any]:
        if self._state is None or self._proc is None or self._proc.poll() is not None:
            return {"running": False}
        return self._state.to_dict()

    async def launch(
        self,
        url: str = "about:blank",
        port: int = 0,
        headless: bool = False,
        import_cookies: str = "",
    ) -> Dict[str, Any]:
        if self.status().get("running"):
            logger.info(f"托管浏览器已在运行: port={self._state.port}")
            return self._state.to_dict()
        binary = self.binary or find_chromium_binary()
        port = port or _free_port()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
        ]
        if headless:
            args.append("--headless=new")
        if url:
            args.append(url)
        logger.info(f"启动托管浏览器: port={port} headless={headless} profile={self.profile_dir}")
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await self._wait_ready(port)
        except Exception:
            self._proc.terminate()
            self._proc = None
            raise
        self._state = ManagedBrowserState(
            port=port,
            profile_dir=str(self.profile_dir),
            pid=self._proc.pid,
            url=url,
            headless=headless,
        )
        os.environ["FUSION_BROWSER_CDP"] = str(port)
        logger.info(f"托管浏览器就绪: pid={self._proc.pid} CDP=127.0.0.1:{port}")
        if import_cookies:
            imported = await self.import_cookies(import_cookies)
            self._state.cookies_imported = imported
        return self._state.to_dict()

    async def _wait_ready(self, port: int) -> None:
        import httpx

        deadline = time.time() + self.launch_timeout
        last_error = ""
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise ManagedBrowserError(f"浏览器进程提前退出 (code={self._proc.returncode})")
            try:
                async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    return
                last_error = f"status={resp.status_code}"
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(self.poll_interval)
        raise ManagedBrowserError(f"浏览器 CDP 端口 {port} 未就绪 ({self.launch_timeout}s): {last_error}")

    async def import_cookies(self, cookies_path: str) -> int:
        from .nodes.browser.cdp_client import CDPClient

        path = Path(cookies_path).expanduser()
        if not path.is_file():
            raise ManagedBrowserError(f"cookie 文件不存在: {path}")
        cookies = parse_cookie_file(path)
        if not cookies:
            logger.warning(f"cookie 文件无可导入项: {path}")
            return 0
        client = CDPClient(port=self._state.port if self._state else int(os.environ.get("FUSION_BROWSER_CDP", "0")))
        await client.connect()
        try:
            await client.send("Network.setCookies", {"cookies": cookies})
        finally:
            await client.disconnect()
        logger.info(f"已导入 {len(cookies)} 条 cookie: {path}")
        return len(cookies)

    async def close(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.poll() is None:
            logger.info(f"关闭托管浏览器: pid={self._proc.pid}")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("浏览器未在 5s 内退出, 强杀")
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._proc = None
        self._state = None
        os.environ.pop("FUSION_BROWSER_CDP", None)
        return True


_instance: Optional[ManagedBrowser] = None


def get_managed_browser() -> ManagedBrowser:
    global _instance
    if _instance is None:
        _instance = ManagedBrowser()
    return _instance


def reset_managed_browser() -> None:
    global _instance
    _instance = None
