from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProcessStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"
    UNKNOWN = "unknown"


class TransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"


@dataclass
class ManagedProcess:
    process_id: str
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    transport: TransportType = TransportType.STDIO
    status: ProcessStatus = ProcessStatus.STOPPED
    pid: Optional[int] = None
    started_at: float = 0.0
    last_health: float = 0.0
    restart_count: int = 0
    max_restarts: int = 3
    auto_restart: bool = True
    plugin_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["transport"] = self.transport.value
        d["status"] = self.status.value
        return d


@dataclass
class ClientSession:
    session_id: str
    client_info: Dict[str, Any] = field(default_factory=dict)
    transport: TransportType = TransportType.STDIO
    connected_at: float = 0.0
    last_activity: float = 0.0
    process_id: Optional[str] = None

    def __post_init__(self):
        if not self.connected_at:
            self.connected_at = time.time()
        if not self.last_activity:
            self.last_activity = time.time()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["transport"] = self.transport.value
        return d


class MCPGateway:
    def __init__(self, host: str = "127.0.0.1", port: int = 11438):
        self.host = host
        self.port = port
        self._processes: Dict[str, ManagedProcess] = {}
        self._subprocesses: Dict[str, asyncio.subprocess.Process] = {}
        self._clients: Dict[str, ClientSession] = {}
        self._health_interval = 30.0
        self._health_task: Optional[asyncio.Task] = None
        self._running = False

    async def spawn(
        self,
        name: str,
        command: str,
        args: List[str] = None,
        env: Dict[str, str] = None,
        transport: TransportType = TransportType.STDIO,
        plugin_name: str = "",
        auto_restart: bool = True,
        max_restarts: int = 3,
    ) -> ManagedProcess:
        process_id = f"proc_{uuid.uuid4().hex[:8]}"
        mp = ManagedProcess(
            process_id=process_id,
            name=name,
            command=command,
            args=args or [],
            env=env or {},
            transport=transport,
            auto_restart=auto_restart,
            max_restarts=max_restarts,
            plugin_name=plugin_name,
        )
        self._processes[process_id] = mp
        await self._start_process(mp)
        logger.info(f"MCPGateway.spawn id={process_id} name={name} transport={transport.value}")
        return mp

    async def _start_process(self, mp: ManagedProcess) -> bool:
        mp.status = ProcessStatus.STARTING
        try:
            proc_env = dict(os.environ)
            proc_env.update(mp.env)
            proc = await asyncio.create_subprocess_exec(
                mp.command,
                *mp.args,
                stdin=asyncio.subprocess.PIPE if mp.transport == TransportType.STDIO else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
            self._subprocesses[mp.process_id] = proc
            mp.pid = proc.pid
            mp.status = ProcessStatus.RUNNING
            mp.started_at = time.time()
            mp.last_health = time.time()
            logger.info(f"MCPGateway._start_process id={mp.process_id} pid={proc.pid}")
            asyncio.create_task(self._watch_process(mp))
            return True
        except Exception as e:
            mp.status = ProcessStatus.CRASHED
            logger.error(f"MCPGateway._start_process failed id={mp.process_id}: {e}")
            return False

    async def _watch_process(self, mp: ManagedProcess) -> None:
        proc = self._subprocesses.get(mp.process_id)
        if not proc:
            return
        try:
            returncode = await proc.wait()
            if mp.status == ProcessStatus.STOPPING:
                mp.status = ProcessStatus.STOPPED
                logger.info(f"MCPGateway process stopped id={mp.process_id} rc={returncode}")
                return
            mp.status = ProcessStatus.CRASHED
            logger.warning(f"MCPGateway process crashed id={mp.process_id} rc={returncode}")
            if mp.auto_restart and mp.restart_count < mp.max_restarts:
                mp.restart_count += 1
                logger.info(f"MCPGateway auto-restart id={mp.process_id} attempt={mp.restart_count}")
                await asyncio.sleep(2**mp.restart_count)
                await self._start_process(mp)
        except Exception as e:
            logger.error(f"MCPGateway._watch_process error id={mp.process_id}: {e}")
            mp.status = ProcessStatus.UNKNOWN

    async def kill(self, process_id: str) -> bool:
        mp = self._processes.get(process_id)
        if not mp:
            logger.warning(f"MCPGateway.kill unknown id={process_id}")
            return False
        mp.status = ProcessStatus.STOPPING
        proc = self._subprocesses.pop(process_id, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                logger.info(f"MCPGateway.kill id={process_id} pid={mp.pid}")
            except ProcessLookupError:
                logger.debug(f"MCPGateway.kill process already gone id={process_id}")
        mp.status = ProcessStatus.STOPPED
        mp.pid = None
        return True

    async def health(self, process_id: str) -> Dict[str, Any]:
        mp = self._processes.get(process_id)
        if not mp:
            return {"error": f"unknown process: {process_id}", "status": "unknown"}
        proc = self._subprocesses.get(process_id)
        alive = proc is not None and proc.returncode is None
        if alive:
            mp.last_health = time.time()
            mp.status = ProcessStatus.RUNNING
        elif mp.status == ProcessStatus.RUNNING:
            mp.status = ProcessStatus.CRASHED
        uptime = time.time() - mp.started_at if mp.started_at else 0
        return {
            "process_id": mp.process_id,
            "name": mp.name,
            "status": mp.status.value,
            "pid": mp.pid,
            "alive": alive,
            "uptime": round(uptime, 1),
            "restart_count": mp.restart_count,
            "last_health": mp.last_health,
            "transport": mp.transport.value,
            "plugin_name": mp.plugin_name,
        }

    async def health_all(self) -> Dict[str, Dict[str, Any]]:
        results = {}
        for pid in list(self._processes.keys()):
            results[pid] = await self.health(pid)
        return results

    def register_client(
        self,
        client_info: Dict[str, Any] = None,
        transport: TransportType = TransportType.STDIO,
        process_id: Optional[str] = None,
    ) -> ClientSession:
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        session = ClientSession(
            session_id=session_id,
            client_info=client_info or {},
            transport=transport,
            process_id=process_id,
        )
        self._clients[session_id] = session
        logger.info(f"MCPGateway.register_client id={session_id} transport={transport.value}")
        return session

    def unregister_client(self, session_id: str) -> bool:
        session = self._clients.pop(session_id, None)
        if session:
            logger.info(f"MCPGateway.unregister_client id={session_id}")
            return True
        return False

    def touch_client(self, session_id: str) -> None:
        session = self._clients.get(session_id)
        if session:
            session.last_activity = time.time()

    def list_clients(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self._clients.values()]

    def list_processes(self) -> List[Dict[str, Any]]:
        return [mp.to_dict() for mp in self._processes.values()]

    def get_process(self, process_id: str) -> Optional[ManagedProcess]:
        return self._processes.get(process_id)

    async def start_health_monitor(self) -> None:
        if self._health_task and not self._health_task.done():
            return
        self._running = True
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info("MCPGateway health monitor started")

    async def stop_health_monitor(self) -> None:
        self._running = False
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        logger.info("MCPGateway health monitor stopped")

    async def _health_loop(self) -> None:
        while self._running:
            try:
                await self.health_all()
            except Exception as e:
                logger.error(f"MCPGateway health loop error: {e}")
            await asyncio.sleep(self._health_interval)

    async def start_all(self) -> int:
        started = 0
        for mp in list(self._processes.values()):
            if mp.status in (ProcessStatus.STOPPED, ProcessStatus.CRASHED):
                mp.restart_count = 0
                ok = await self._start_process(mp)
                if ok:
                    started += 1
        logger.info(f"MCPGateway.start_all started={started}")
        return started

    async def stop_all(self) -> int:
        stopped = 0
        for pid in list(self._processes.keys()):
            ok = await self.kill(pid)
            if ok:
                stopped += 1
        await self.stop_health_monitor()
        logger.info(f"MCPGateway.stop_all stopped={stopped}")
        return stopped

    async def send_stdin(self, process_id: str, data: bytes) -> bool:
        proc = self._subprocesses.get(process_id)
        if not proc or proc.returncode is not None:
            return False
        if proc.stdin is None:
            return False
        try:
            proc.stdin.write(data + b"\n")
            await proc.stdin.drain()
            return True
        except Exception as e:
            logger.error(f"MCPGateway.send_stdin error id={process_id}: {e}")
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "processes": len(self._processes),
            "clients": len(self._clients),
            "running": self._running,
        }
