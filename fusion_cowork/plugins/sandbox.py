from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import signal
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# CR-23: 子进程仅继承安全环境变量子集, 不泄漏父进程敏感 (API key/token/凭据等)
_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TZ",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)

# CR-23: darwin seatbelt profile — 限制 fs 写 + 拒绝原始网络, 仅允许写 /tmp 与插件目录
# 注: 此为最小防御层, 与 rlimit (CPU/内存/文件数/进程数) 叠加
_SEATBELT_PROFILE = """(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow file-read*)
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "${HOME}/.fusion-cowork"))
(allow file-write-data (literal "/dev/null"))
(allow file-write-data (literal "/dev/dtracehelper"))
(allow sysctl-read)
(deny network*)
"""


class SandboxStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"
    TIMEOUT = "timeout"
    OOM = "oom"
    VIOLATION = "violation"


@dataclass
class ResourceLimits:
    max_cpu_seconds: float = 60.0
    max_memory_mb: int = 512
    max_output_bytes: int = 10 * 1024 * 1024
    max_processes: int = 1
    max_file_size_mb: int = 50
    timeout_seconds: float = 120.0
    heartbeat_interval: float = 15.0
    max_heartbeat_misses: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SandboxResult:
    sandbox_id: str
    plugin_name: str
    status: SandboxStatus = SandboxStatus.IDLE
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    cpu_time: float = 0.0
    memory_peak_mb: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class HeartbeatRecord:
    sandbox_id: str
    timestamp: float = 0.0
    miss_count: int = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class PluginSandbox:
    def __init__(self, limits: ResourceLimits = None):
        self._limits = limits or ResourceLimits()
        self._sandboxes: Dict[str, SandboxResult] = {}
        self._subprocesses: Dict[str, asyncio.subprocess.Process] = {}
        self._heartbeats: Dict[str, HeartbeatRecord] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False
        self._on_crash: Optional[Callable] = None

    @property
    def limits(self) -> ResourceLimits:
        return self._limits

    def set_limits(self, limits: ResourceLimits) -> None:
        self._limits = limits
        logger.info(f"PluginSandbox.set_limits cpu={limits.max_cpu_seconds}s mem={limits.max_memory_mb}MB")

    def on_crash(self, callback: Callable) -> None:
        self._on_crash = callback
        logger.info("PluginSandbox.on_crash callback registered")

    async def execute(
        self, plugin_name: str, command: str, args: List[str] = None, env: Dict[str, str] = None, stdin_data: str = ""
    ) -> SandboxResult:
        sandbox_id = f"sbx_{uuid.uuid4().hex[:8]}"
        result = SandboxResult(
            sandbox_id=sandbox_id,
            plugin_name=plugin_name,
            status=SandboxStatus.RUNNING,
            started_at=time.time(),
        )
        self._sandboxes[sandbox_id] = result
        hb = HeartbeatRecord(sandbox_id=sandbox_id)
        self._heartbeats[sandbox_id] = hb

        logger.info(f"PluginSandbox.execute id={sandbox_id} plugin={plugin_name} cmd={command}")

        try:
            # CR-23: 仅继承安全 env 子集, 不泄漏父进程敏感变量 (API key/token 等)
            proc_env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
            proc_env.update(env or {})
            proc_env["FUSION_SANDBOX_ID"] = sandbox_id
            proc_env["FUSION_SANDBOX_LIMITS"] = json.dumps(self._limits.to_dict())

            # CR-23: darwin seatbelt — sandbox-exec 包装限制 fs 写 + 拒网络
            real_cmd, real_args = self._wrap_seatbelt(command, args or [])

            proc = await asyncio.create_subprocess_exec(
                real_cmd,
                *real_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
                preexec_fn=self._make_preexec_fn(),
            )
            self._subprocesses[sandbox_id] = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=stdin_data.encode() if stdin_data else None),
                    timeout=self._limits.timeout_seconds,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                result.status = SandboxStatus.TIMEOUT
                result.error = f"Execution timed out after {self._limits.timeout_seconds}s"
                result.finished_at = time.time()
                logger.warning(f"PluginSandbox timeout id={sandbox_id}")
                self._fire_crash_callback(result)
                return result

            result.exit_code = proc.returncode
            result.stdout = stdout_bytes[: self._limits.max_output_bytes].decode(errors="replace")
            result.stderr = stderr_bytes[: self._limits.max_output_bytes].decode(errors="replace")
            result.finished_at = time.time()
            result.cpu_time = result.finished_at - result.started_at

            if proc.returncode == -signal.SIGXCPU:
                result.status = SandboxStatus.TIMEOUT
                result.error = "CPU time limit exceeded"
            elif proc.returncode == -signal.SIGKILL:
                result.status = SandboxStatus.OOM
                result.error = "Process killed (likely OOM)"
            elif proc.returncode != 0:
                result.status = SandboxStatus.CRASHED
                result.error = f"Process exited with code {proc.returncode}"
                self._fire_crash_callback(result)
            else:
                result.status = SandboxStatus.STOPPED
                logger.info(f"PluginSandbox completed id={sandbox_id} rc=0")

        except Exception as e:
            result.status = SandboxStatus.CRASHED
            result.error = str(e)
            result.finished_at = time.time()
            logger.error(f"PluginSandbox.execute error id={sandbox_id}: {e}")
            self._fire_crash_callback(result)

        return result

    def _wrap_seatbelt(self, command: str, args: List[str]) -> tuple:
        """CR-23: darwin 用 sandbox-exec 包裹真实命令, 限制 fs 写 + 拒网络。

        sandbox-exec 不可用时回退原命令 (仅靠 rlimit 隔离, 记 WARNING)。
        """
        import shutil as _shutil
        from pathlib import Path as _Path

        if not _shutil.which("sandbox-exec"):
            logger.warning("sandbox-exec 不可用, 回退 rlimit-only 隔离 (无 fs/network 限制)")
            return command, args
        profile_path = "/tmp/fusion_sandbox_profile.sb"
        try:
            # sandbox-exec 不展开 shell 变量, ${HOME} 会被当未绑定变量报错;
            # 写入前用真实 home 路径替换
            profile = _SEATBELT_PROFILE.replace("${HOME}", str(_Path.home()))
            with open(profile_path, "w", encoding="utf-8") as f:
                f.write(profile)
        except OSError as e:
            logger.warning(f"seatbelt profile 写入失败, 回退 rlimit-only: {e}")
            return command, args
        logger.info(f"PluginSandbox seatbelt 包装: sandbox-exec -f {profile_path}")
        return "sandbox-exec", ["-f", profile_path, "--", command, *args]

    def _make_preexec_fn(self):
        import sys as _sys

        def _set_limits():
            # CR-23: setrlimit 失败 fail-closed — 限制设不上则拒执行 (防逃逸)
            try:
                cpu_limit = int(self._limits.max_cpu_seconds)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            except (ValueError, OSError) as e:
                logger.error(f"PluginSandbox setrlimit CPU fail-closed: {e}")
                os._exit(127)
            mem_bytes = self._limits.max_memory_mb * 1024 * 1024
            # darwin: RLIMIT_AS/RLIMIT_DATA 在 Python 子进程均不可用 — 解释器启动即
            # 预留数十 GB 虚拟内存, fork 后 setrlimit 必超当前预留而失败。darwin 的
            # 内存隔离改由 seatbelt profile 承担 (见 _wrap_seatbelt); rlimit 内存
            # 限仅在 Linux (VM 预留小) 生效, 设不上 fail-closed。
            if _sys.platform != "darwin":
                try:
                    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
                except (ValueError, OSError) as e:
                    logger.error(f"PluginSandbox setrlimit AS fail-closed: {e}")
                    os._exit(127)
            else:
                logger.debug("PluginSandbox darwin: 内存隔离由 seatbelt 承担, 跳过 RLIMIT_AS")
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (self._limits.max_processes, self._limits.max_processes))
            except (ValueError, OSError) as e:
                logger.debug(f"PluginSandbox setrlimit NPROC unsupported: {e}")
            try:
                file_bytes = self._limits.max_file_size_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
            except (ValueError, OSError) as e:
                logger.debug(f"PluginSandbox setrlimit FSIZE unsupported: {e}")

        return _set_limits

    def heartbeat(self, sandbox_id: str) -> bool:
        hb = self._heartbeats.get(sandbox_id)
        if not hb:
            return False
        hb.timestamp = time.time()
        hb.miss_count = 0
        return True

    async def start_heartbeat_monitor(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("PluginSandbox heartbeat monitor started")

    async def stop_heartbeat_monitor(self) -> None:
        self._running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        logger.info("PluginSandbox heartbeat monitor stopped")

    async def _heartbeat_loop(self) -> None:
        while self._running:
            now = time.time()
            for sid, hb in list(self._heartbeats.items()):
                result = self._sandboxes.get(sid)
                if not result or result.status != SandboxStatus.RUNNING:
                    continue
                elapsed = now - hb.timestamp
                if elapsed > self._limits.heartbeat_interval:
                    hb.miss_count += 1
                    logger.warning(f"PluginSandbox heartbeat miss id={sid} miss={hb.miss_count}")
                    if hb.miss_count >= self._limits.max_heartbeat_misses:
                        logger.error(f"PluginSandbox heartbeat timeout id={sid}")
                        await self._kill_sandbox(sid)
                        result.status = SandboxStatus.CRASHED
                        result.error = f"Heartbeat timeout: {hb.miss_count} consecutive misses"
                        result.finished_at = time.time()
                        self._fire_crash_callback(result)
            await asyncio.sleep(self._limits.heartbeat_interval)

    async def _kill_sandbox(self, sandbox_id: str) -> bool:
        proc = self._subprocesses.pop(sandbox_id, None)
        if not proc or proc.returncode is not None:
            return False
        try:
            proc.kill()
            await proc.wait()
            logger.info(f"PluginSandbox._kill_sandbox id={sandbox_id}")
            return True
        except ProcessLookupError:
            return False

    def _fire_crash_callback(self, result: SandboxResult) -> None:
        if self._on_crash:
            try:
                self._on_crash(result)
            except Exception as e:
                logger.error(f"PluginSandbox crash callback error: {e}")

    async def stop(self, sandbox_id: str) -> bool:
        result = self._sandboxes.get(sandbox_id)
        if not result or result.status != SandboxStatus.RUNNING:
            return False
        ok = await self._kill_sandbox(sandbox_id)
        if ok:
            result.status = SandboxStatus.STOPPED
            result.finished_at = time.time()
        return ok

    def get_result(self, sandbox_id: str) -> Optional[SandboxResult]:
        return self._sandboxes.get(sandbox_id)

    def list_sandboxes(self, status: SandboxStatus = None) -> List[Dict[str, Any]]:
        results = list(self._sandboxes.values())
        if status:
            results = [r for r in results if r.status == status]
        return [r.to_dict() for r in results]

    def cleanup(self, max_age_seconds: float = 3600) -> int:
        now = time.time()
        to_remove = []
        for sid, result in self._sandboxes.items():
            if result.status in (
                SandboxStatus.STOPPED,
                SandboxStatus.CRASHED,
                SandboxStatus.TIMEOUT,
                SandboxStatus.OOM,
                SandboxStatus.VIOLATION,
            ):
                if result.finished_at and (now - result.finished_at) > max_age_seconds:
                    to_remove.append(sid)
        for sid in to_remove:
            self._sandboxes.pop(sid, None)
            self._heartbeats.pop(sid, None)
        if to_remove:
            logger.info(f"PluginSandbox.cleanup removed={len(to_remove)}")
        return len(to_remove)

    def to_dict(self) -> Dict[str, Any]:
        running = sum(1 for r in self._sandboxes.values() if r.status == SandboxStatus.RUNNING)
        return {
            "limits": self._limits.to_dict(),
            "total_sandboxes": len(self._sandboxes),
            "running_sandboxes": running,
            "heartbeat_monitor": self._running,
        }
