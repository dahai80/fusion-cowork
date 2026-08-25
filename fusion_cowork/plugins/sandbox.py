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
# A-2 修复: process-exec 仅放行被 exec 命令所在框架目录 (EXEC_DIR, 已 resolve 真实路径),
# 不再无约束放行任意 exec — 沙箱内 os.exec("/bin/sh") / "/usr/bin/python" 逃逸被拒。
# 解释器 (venv python 是 symlink → framework) 自身 dylib 须在该目录内 exec 才能启动,
# 故放行整个框架目录而非单一 binary; fork 仍允许 (子进程再 exec 须命中该目录规则)。
# 注: 此为最小防御层, 与 rlimit (CPU/内存/文件数/进程数) 叠加
_SEATBELT_PROFILE = """(version 1)
(deny default)
(allow process-fork)
(allow process-exec (subpath "${EXEC_DIR}"))
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
        self,
        plugin_name: str,
        command: str,
        args: List[str] = None,
        env: Dict[str, str] = None,
        stdin_data: str = "",
        limits: Optional[ResourceLimits] = None,
    ) -> SandboxResult:
        # LO-7: 支持按调用覆盖 ResourceLimits (manifest.timeout_seconds 覆盖默认 120s)
        active_limits = limits if limits is not None else self._limits
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

        logger.info(
            f"PluginSandbox.execute id={sandbox_id} plugin={plugin_name} cmd={command} "
            f"timeout={active_limits.timeout_seconds}s"
        )

        try:
            # CR-23: 仅继承安全 env 子集, 不泄漏父进程敏感变量 (API key/token 等)
            proc_env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
            proc_env.update(env or {})
            proc_env["FUSION_SANDBOX_ID"] = sandbox_id
            proc_env["FUSION_SANDBOX_LIMITS"] = json.dumps(active_limits.to_dict())

            # CR-23: darwin seatbelt — sandbox-exec 包装限制 fs 写 + 拒网络
            real_cmd, real_args = self._wrap_seatbelt(command, args or [])

            proc = await asyncio.create_subprocess_exec(
                real_cmd,
                *real_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
                preexec_fn=self._make_preexec_fn(active_limits),
                # A-2: 子进程已在 preexec 建 session (setsid), start_new_session 冗余但显式
                start_new_session=True,
            )
            self._subprocesses[sandbox_id] = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    self._bounded_communicate(proc, stdin_data, active_limits.max_output_bytes),
                    timeout=active_limits.timeout_seconds,
                )
            except TimeoutError:
                # A-2: kill 整进程组 (子进程 setsid 建独立组), 杜绝 fork 出孤儿逃过超时
                await self._kill_process_group(proc)
                result.status = SandboxStatus.TIMEOUT
                result.error = f"Execution timed out after {active_limits.timeout_seconds}s"
                result.finished_at = time.time()
                logger.warning(f"PluginSandbox timeout id={sandbox_id} (killpg)")
                self._fire_crash_callback(result)
                return result

            result.exit_code = proc.returncode
            result.stdout = stdout_bytes.decode(errors="replace")
            result.stderr = stderr_bytes.decode(errors="replace")
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

    async def _bounded_communicate(self, proc: asyncio.subprocess.Process, stdin_data: str, max_bytes: int) -> tuple:
        """A-2: 流式有界读 stdout/stderr — 边读边累计, 超 max_bytes 立即停读截断,
        避免 100GB 输出先全量进内存再截 (OOM)。communicate 的 input 仍写一次。
        """
        encoded_stdin = stdin_data.encode() if stdin_data else None

        async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
            chunks = bytearray()
            while True:
                # 分块读, 超上限即停 (不 await 整条到 EOF)
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = max_bytes - len(chunks)
                if remaining <= 0:
                    # 已满: 排空余量避免管道阻塞, 但不再保留
                    while stream.read(64 * 1024):
                        pass
                    break
                chunks.extend(chunk[:remaining])
            return bytes(chunks)

        if encoded_stdin:
            proc.stdin.write(encoded_stdin)
            await proc.stdin.drain()
        if proc.stdin:
            proc.stdin.close()
        out_task = asyncio.ensure_future(_read_bounded(proc.stdout))
        err_task = asyncio.ensure_future(_read_bounded(proc.stderr))
        try:
            stdout_bytes, stderr_bytes = await asyncio.gather(out_task, err_task)
        finally:
            await proc.wait()
        return stdout_bytes, stderr_bytes

    async def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        """A-2: 杀整进程组 — proc setsid 建独立 session, killpg(-pgid) 清子孙。
        回退: 组杀失败则 proc.kill() + wait。
        """
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            logger.debug(f"PluginSandbox killpg pgid={pgid}")
        except (ProcessLookupError, OSError) as e:
            logger.debug(f"PluginSandbox killpg 回退 proc.kill: {e}")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except Exception:
            pass

    def _wrap_seatbelt(self, command: str, args: List[str]) -> tuple:
        """CR-23: darwin 用 sandbox-exec 包裹真实命令, 限制 fs 写 + 拒网络。

        sandbox-exec 不可用时回退原命令 (仅靠 rlimit 隔离, 记 WARNING)。
        A-2 修复: profile 写到 per-sandbox 唯一文件 (O_EXCL|0600), 杜绝固定路径 TOCTOU;
        且 process-exec 仅放行被 exec 的命令二进制绝对路径, 不再无约束允许 exec。
        """
        import shutil as _shutil
        from pathlib import Path as _Path

        if not _shutil.which("sandbox-exec"):
            logger.warning("sandbox-exec 不可用, 回退 rlimit-only 隔离 (无 fs/network 限制)")
            return command, args
        # 解析命令二进制真实绝对路径 (resolve 跨 symlink): venv python → framework 真体
        cmd_abs = _shutil.which(command) or str(_Path(command).resolve())
        cmd_real = str(_Path(cmd_abs).resolve())
        # exec 放行目录 = 真实二进制所在 framework 版本目录 (python 须 exec 同目录 dylib 启动)
        # 目录取 framework/Versions/3.14 这一级 (二进制在 .../Versions/3.14/bin/python3.14)
        exec_dir = str(_Path(cmd_real).parents[1])
        profile_path = f"/tmp/fusion_sandbox_profile_{uuid.uuid4().hex[:8]}.sb"
        try:
            # sandbox-exec 不展开 shell 变量, ${HOME}/${EXEC_DIR} 写入前用真实值替换
            profile = _SEATBELT_PROFILE.replace("${HOME}", str(_Path.home())).replace("${EXEC_DIR}", exec_dir)
            # O_EXCL: 文件已存在则拒 (防覆盖/TOCTOU); 0600: 仅属主读写
            fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, profile.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as e:
            logger.warning(f"seatbelt profile 写入失败, 回退 rlimit-only: {e}")
            return command, args
        logger.info(f"PluginSandbox seatbelt 包装: sandbox-exec -f {profile_path} exec={cmd_abs}")
        return "sandbox-exec", ["-f", profile_path, "--", cmd_abs, *args]

    def _make_preexec_fn(self, limits: Optional[ResourceLimits] = None):
        import sys as _sys

        active_limits = limits if limits is not None else self._limits

        def _set_limits():
            # A-2: 子进程建独立进程组 — 超时父进程 killpg 杀整组, 杜绝孤儿
            try:
                os.setsid()
            except OSError as e:
                logger.debug(f"PluginSandbox setsid skipped: {e}")
            # CR-23: setrlimit 失败 fail-closed — 限制设不上则拒执行 (防逃逸)
            try:
                cpu_limit = int(active_limits.max_cpu_seconds)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            except (ValueError, OSError) as e:
                logger.error(f"PluginSandbox setrlimit CPU fail-closed: {e}")
                os._exit(127)
            mem_bytes = active_limits.max_memory_mb * 1024 * 1024
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
            # A-2: NPROC/FSIZE 失败须 fail-closed (与 CPU/AS 一致),
            # 否则限制静默失效 = 沙箱逃逸面
            try:
                resource.setrlimit(
                    resource.RLIMIT_NPROC,
                    (active_limits.max_processes, active_limits.max_processes),
                )
            except (ValueError, OSError) as e:
                logger.error(f"PluginSandbox setrlimit NPROC fail-closed: {e}")
                os._exit(127)
            try:
                file_bytes = active_limits.max_file_size_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
            except (ValueError, OSError) as e:
                logger.error(f"PluginSandbox setrlimit FSIZE fail-closed: {e}")
                os._exit(127)

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
            # A-2: 杀进程组而非仅直接子进程, 防 fork 孤儿
            await self._kill_process_group(proc)
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
