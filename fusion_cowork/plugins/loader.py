from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import logging
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine.node import BaseNode, NodeCategory, NodeConfig, NodeRegistry, NodeResult, NodeStatus
from .manifest import PluginManifest

logger = logging.getLogger(__name__)

_RESULT_MARKER = "__SANDBOX_RESULT__"


class SandboxedNode(BaseNode):
    """沙箱节点包装器 — P1-6。

    包装第三方插件节点: 节点元数据 (name/display_name/category/schema) 在主进程注册,
    但 execute() 委托给 PluginSandbox 子进程执行, 插件代码不进主进程。

    每个插件节点通过 make_sandboxed_node_class() 生成独立子类,
    使类级 name/display_name 等元数据固定, 满足 NodeRegistry 按 name 注册的要求。
    """

    name = "sandboxed_node"
    display_name = "沙箱插件节点"
    category = NodeCategory.TOOL
    description = "沙箱内执行的第三方插件节点"
    icon = "📦"
    default_label = "插件节点"

    def __init__(
        self,
        node_id: str = "",
        config: Optional[NodeConfig] = None,
        *,
        entry_file: str = "",
        class_name: str = "",
        sandbox: Optional[Any] = None,
        params_schema: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(node_id=node_id, config=config)
        self._entry_file = entry_file
        self._class_name = class_name
        self._sandbox = sandbox
        self._params_schema = params_schema or {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def get_params_schema(self) -> Dict[str, Any]:
        return self._params_schema

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        if not self._entry_file or not self._class_name:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="沙箱节点缺少 entry_file/class_name",
                summary="沙箱节点配置不完整",
            )

        runner_path = str(Path(__file__).parent / "sandbox_runner.py")
        req = json.dumps(
            {
                "action": "run",
                "entry_file": self._entry_file,
                "class_name": self._class_name,
                "inputs": inputs or {},
            },
            ensure_ascii=False,
        )

        sandbox = self._sandbox
        if sandbox is None:
            from .sandbox import PluginSandbox

            sandbox = PluginSandbox()

        # LO-7: 从节点 meta 读 manifest.timeout_seconds 覆盖沙箱默认超时 (>0 才覆盖)
        call_limits = None
        meta = getattr(type(self), "_sandbox_meta", {}) or {}
        meta_timeout = float(meta.get("timeout_seconds", 0.0) or 0.0)
        if meta_timeout > 0:
            from .sandbox import ResourceLimits

            base = sandbox.limits
            call_limits = ResourceLimits(
                max_cpu_seconds=base.max_cpu_seconds,
                max_memory_mb=base.max_memory_mb,
                max_output_bytes=base.max_output_bytes,
                max_processes=base.max_processes,
                max_file_size_mb=base.max_file_size_mb,
                timeout_seconds=meta_timeout,
                heartbeat_interval=base.heartbeat_interval,
                max_heartbeat_misses=base.max_heartbeat_misses,
            )
            logger.info(f"沙箱节点 {self._class_name} 超时覆盖: {meta_timeout}s (manifest 声明)")

        try:
            res = await sandbox.execute(
                plugin_name=f"sandbox_node:{self._class_name}",
                command=sys.executable,
                args=[runner_path],
                stdin_data=req,
                limits=call_limits,
            )
        except Exception as e:
            logger.error(f"沙箱节点执行异常 {self._class_name}: {e}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"沙箱执行异常: {e}",
                summary="沙箱子进程启动失败",
            )

        out = res.stdout or ""
        idx = out.find(_RESULT_MARKER)
        if idx < 0:
            # LO-5: 无结果帧标记 → 子进程未输出约定结果, 不把整 stdout 当 payload
            stdout_tail = out[-256:]
            logger.error(
                f"沙箱节点 {self._class_name} 未输出结果帧 (marker 缺失); "
                f"stdout尾={stdout_tail!r} stderr={(res.stderr or '')[-256:]!r}"
            )
            return NodeResult(
                status=NodeStatus.FAILED,
                error="子进程未输出结果帧 (缺少 RESULT_MARKER)",
                summary=f"沙箱返回无标记输出 rc={res.exit_code}",
            )
        payload_str = out[idx + len(_RESULT_MARKER) :].strip()
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as e:
            err_tail = (res.stderr or "")[-512:]
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"沙箱结果解析失败: {e}; stderr尾: {err_tail}",
                summary="沙箱返回非 JSON",
            )

        if not payload.get("ok"):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(payload.get("error", "未知沙箱错误")),
                summary="沙箱内执行失败",
            )

        data = payload.get("data", {}) or {}
        status_raw = str(data.get("status", "failed")).lower()
        status = NodeStatus.SUCCESS if status_raw in ("success", "completed") else NodeStatus.FAILED
        if res.status and res.status.value in ("timeout", "oom", "crashed"):
            status = NodeStatus.FAILED
        node_data = data.get("data") if isinstance(data.get("data"), dict) else data
        return NodeResult(
            status=status,
            data=node_data,
            error=data.get("error"),
            execution_time=float(data.get("execution_time", res.cpu_time or 0.0)),
            output_files=data.get("output_files", []),
            summary=data.get("summary", "") or f"沙箱执行完成 rc={res.exit_code}",
        )


def make_sandboxed_node_class(meta: Dict[str, Any], sandbox: Any) -> type[BaseNode]:
    """为单个插件节点生成沙箱包装子类 — P1-6。

    子类固定类级 name/display_name/category 等元数据, 满足 NodeRegistry 注册需求。
    LO-6: 未知 meta 键记 warning (不静默吞), 便于插件清单字段错拼写暴露。
    """
    _EXPECTED_META = {
        "class_name",
        "name",
        "display_name",
        "category",
        "description",
        "icon",
        "default_label",
        "entry_file",
        "params_schema",
        "timeout_seconds",
    }
    unknown = set(meta.keys()) - _EXPECTED_META
    if unknown:
        logger.warning(f"沙箱插件节点 meta 含未知键: {sorted(unknown)} (将被忽略)")

    cat_raw = meta.get("category", "tool")
    try:
        category = NodeCategory(cat_raw)
    except ValueError:
        category = NodeCategory.TOOL

    cls = type(
        f"SandboxedNode_{meta.get('class_name', 'Plugin')}",
        (SandboxedNode,),
        {
            "name": meta.get("name", "sandboxed_node"),
            "display_name": meta.get("display_name", "沙箱插件节点"),
            "category": category,
            "description": meta.get("description", "沙箱内执行的第三方插件节点"),
            "icon": meta.get("icon", "📦"),
            "default_label": meta.get("default_label", "插件节点"),
        },
    )
    cls._sandbox_meta = meta  # type: ignore[attr-defined]
    cls._sandbox_instance = sandbox  # type: ignore[attr-defined]
    return cls


def _sandboxed_factory(cls: type[BaseNode]):
    """覆盖 __init__ 以注入 entry_file/class_name/sandbox/schema。"""
    meta = getattr(cls, "_sandbox_meta", {})
    sandbox = getattr(cls, "_sandbox_instance", None)

    def __init__(self, node_id: str = "", config: Optional[NodeConfig] = None, **_kwargs):
        SandboxedNode.__init__(
            self,
            node_id=node_id,
            config=config,
            entry_file=meta.get("entry_file", ""),
            class_name=meta.get("class_name", ""),
            sandbox=sandbox,
            params_schema=meta.get("params_schema"),
        )

    cls.__init__ = __init__  # type: ignore[assignment]
    return cls


class PluginLoader:
    def __init__(self, plugins_dir: str = ""):
        self._plugins_dir = (
            Path(plugins_dir).expanduser() if plugins_dir else Path.home() / ".fusion-cowork" / "plugins"
        )
        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        self._loaded: Dict[str, PluginManifest] = {}
        self._node_map: Dict[str, List[str]] = {}
        self._sandboxes: Dict[str, Any] = {}

    def discover(self) -> List[PluginManifest]:
        manifests = []
        for plugin_dir in sorted(self._plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "manifest.json"
            if not manifest_path.exists():
                logger.debug(f"跳过无清单目录: {plugin_dir.name}")
                continue
            manifest = PluginManifest.from_json(manifest_path)
            if manifest:
                manifests.append(manifest)
        logger.info(f"发现 {len(manifests)} 个插件")
        return manifests

    def load(self, name: str) -> List[BaseNode]:
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.is_dir():
            logger.error(f"插件目录不存在: {name}")
            return []

        manifest_path = plugin_dir / "manifest.json"
        manifest = PluginManifest.from_json(manifest_path)
        if not manifest:
            logger.error(f"插件清单加载失败: {name}")
            return []

        entry_file = plugin_dir / f"{manifest.entry_point}.py"
        if not entry_file.exists():
            logger.error(f"插件入口文件不存在: {entry_file}")
            return []

        if manifest.sandbox:
            return self._load_sandboxed(name, manifest, entry_file)

        # CR-21: sandbox=false 插件走主进程 exec_module = 潜在 RCE,
        # 须在 config plugins.trusted 白名单, 否则拒绝加载。
        trusted = self._get_trusted_plugins()
        if name not in trusted:
            logger.error(
                f"插件 {name} sandbox=false 且未在 plugins.trusted 白名单, 拒绝加载"
                f" (需显式信任: config set plugins.trusted '[...{name}...]' 或启动参数 --trust-plugin {name})"
            )
            return []

        module_name = f"fusion_cowork_plugin_{name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(entry_file))
            if spec is None or spec.loader is None:
                logger.error(f"无法加载插件模块: {entry_file}")
                return []
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f"插件模块执行失败 {name}: {e}")
            return []

        registered = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if attr is None:
                continue
            try:
                if isinstance(attr, type) and issubclass(attr, BaseNode) and attr is not BaseNode:
                    NodeRegistry.register(attr)
                    registered.append(attr)
                    logger.info(f"注册插件节点: {attr.name} ({attr.__name__})")
            except TypeError:
                continue

        self._loaded[name] = manifest
        self._node_map[name] = [n.name for n in registered]
        logger.info(f"插件 {name} 加载完成: {len(registered)} 个节点")
        return registered

    def _load_sandboxed(self, name: str, manifest: PluginManifest, entry_file: Path) -> List[BaseNode]:
        """沙箱加载路径 — P1-6: 插件代码不进主进程。

        通过 sandbox_runner 子进程 introspect 发现节点元数据,
        为每个节点生成 SandboxedNode 包装子类注册到主进程,
        execute() 时再回子进程运行。主进程永不 exec_module 插件代码。
        """
        from .sandbox import PluginSandbox, SandboxStatus

        sandbox = PluginSandbox()
        runner_path = str(Path(__file__).parent / "sandbox_runner.py")
        req = json.dumps(
            {"action": "introspect", "entry_file": str(entry_file)},
            ensure_ascii=False,
        )
        logger.info(f"插件 {name} sandbox=true, 子进程 introspect 节点元数据 (rlimit 隔离)")
        try:
            loop = asyncio.new_event_loop()
            pre = loop.run_until_complete(
                sandbox.execute(
                    plugin_name=name,
                    command=sys.executable,
                    args=[runner_path],
                    stdin_data=req,
                )
            )
            loop.close()
        except Exception as pe:
            logger.error(f"插件 {name} 沙箱 introspect 异常: {pe}")
            return []
        if pre.status != SandboxStatus.STOPPED:
            logger.error(f"插件 {name} 沙箱 introspect 失败: status={pre.status} err={pre.error}")
            return []

        out = pre.stdout or ""
        idx = out.find(_RESULT_MARKER)
        payload_str = out[idx + len(_RESULT_MARKER) :].strip() if idx >= 0 else out.strip()
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as e:
            logger.error(f"插件 {name} introspect 结果解析失败: {e}; stderr={pre.stderr[-256:]}")
            return []
        if not payload.get("ok"):
            logger.error(f"插件 {name} introspect 报错: {payload.get('error')}")
            return []

        node_metas = payload.get("data", {}).get("nodes", [])
        if not node_metas:
            logger.warning(f"插件 {name} sandbox 加载未发现节点")
            return []

        registered = []
        for meta in node_metas:
            meta["entry_file"] = str(entry_file)
            # LO-7: 透传 manifest.timeout_seconds 到节点 meta, execute 时覆盖沙箱默认超时
            meta["timeout_seconds"] = manifest.timeout_seconds
            wrapper_cls = make_sandboxed_node_class(meta, sandbox)
            _sandboxed_factory(wrapper_cls)
            NodeRegistry.register(wrapper_cls)
            registered.append(wrapper_cls)
            logger.info(f"注册沙箱插件节点: {wrapper_cls.name} ({wrapper_cls.__name__})")

        self._loaded[name] = manifest
        self._node_map[name] = [c.name for c in registered]
        self._sandboxes[name] = sandbox
        logger.info(f"插件 {name} 沙箱加载完成: {len(registered)} 个节点 (全程子进程隔离)")
        return registered

    def _get_trusted_plugins(self) -> List[str]:
        """CR-21: 读 config plugins.trusted 白名单 (sandbox=false 插件须显式信任)。"""
        try:
            from ..config_center import ConfigCenter

            cc = ConfigCenter.get_instance()
            trusted = cc.get("plugins.trusted", [])
            if isinstance(trusted, list):
                return [str(t) for t in trusted]
            if isinstance(trusted, str):
                return [t.strip() for t in trusted.split(",") if t.strip()]
        except Exception as e:
            logger.debug(f"读取 plugins.trusted 失败, 视为空白名单: {e}")
        return []

    def load_all(self) -> Dict[str, List[BaseNode]]:
        results = {}
        for manifest in self.discover():
            nodes = self.load(manifest.name)
            results[manifest.name] = nodes
        return results

    def unload(self, name: str) -> bool:
        if name not in self._loaded:
            logger.warning(f"插件未加载: {name}")
            return False
        node_names = self._node_map.pop(name, [])
        for node_name in node_names:
            NodeRegistry.unregister(node_name)
            logger.info(f"注销插件节点: {node_name}")
        del self._loaded[name]
        self._sandboxes.pop(name, None)
        logger.info(f"插件 {name} 已卸载")
        return True

    def install(self, path: str) -> bool:
        if path.startswith("http://") or path.startswith("https://"):
            return self._install_url(path)
        src = Path(path).expanduser().resolve()
        if not src.exists():
            logger.error(f"安装源不存在: {path}")
            return False

        if src.suffix == ".zip":
            return self._install_zip(src)
        elif src.is_dir():
            return self._install_dir(src)
        else:
            logger.error(f"不支持的安装源类型: {path}")
            return False

    def _install_url(self, url: str) -> bool:
        import tempfile

        try:
            import httpx
        except ImportError:
            logger.error("安装 URL 插件需要 httpx (pip install httpx)")
            return False
        # MD-16: 仅允许 https, 拒 http (防中间人篡改插件包)
        if not url.lower().startswith("https://"):
            logger.error(f"URL 插件仅允许 https://: {url} (拒 http 防篡改)")
            return False
        if not url.lower().endswith(".zip"):
            logger.error(f"URL 插件仅支持 .zip: {url}")
            return False
        _MAX_DOWNLOAD = 50 * 1024 * 1024  # 50 MiB 上限, 防超大包耗尽内存
        try:
            logger.info(f"从 URL 下载插件: {url}")
            tmp_path = Path(tempfile.mktemp(suffix=".zip"))
            downloaded = 0
            sha = hashlib.sha256()
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                resp = client.get(url)
                # MD-16: 显式拒重定向 (防开放重定向到内网/恶意源); 客户端须用直链
                if resp.is_redirect:
                    logger.error(f"URL 插件拒重定向: {url} -> {resp.headers.get('location', '?')}")
                    return False
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD:
                            logger.error(f"URL 插件超大小上限 {_MAX_DOWNLOAD} 字节, 中止下载: {url}")
                            f.close()
                            try:
                                tmp_path.unlink()
                            except OSError:
                                pass
                            return False
                        sha.update(chunk)
                        f.write(chunk)
            logger.info(f"URL 插件下载完成: {downloaded} 字节, sha256={sha.hexdigest()[:16]}")
            try:
                return self._install_zip(tmp_path)
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"URL 插件安装失败: {e}")
            return False

    def _install_zip(self, zip_path: Path) -> bool:
        base = self._plugins_dir.resolve()
        # E-13: zip 炸弹防护 — 限文件数/解压总大小/压缩比
        _MAX_ZIP_ENTRIES = 10000
        _MAX_UNCOMPRESSED = 512 * 1024 * 1024  # 512MB
        _MAX_RATIO = 200  # 解压/压缩 比, 超过即炸弹嫌疑
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                # E-13: 文件数上限
                if len(names) > _MAX_ZIP_ENTRIES:
                    logger.error(f"zip 炸弹拒绝: 条目数 {len(names)} > {_MAX_ZIP_ENTRIES}")
                    return False
                # CR-22: 逐条校验防 zip-slip — 每个解压目标必须落在 plugins_dir 内
                total_uncompressed = 0
                total_compressed = 0
                for member in zf.infolist():
                    total_uncompressed += member.file_size
                    total_compressed += member.compress_size
                    if total_uncompressed > _MAX_UNCOMPRESSED:
                        logger.error(f"zip 炸弹拒绝: 解压总大小 {total_uncompressed} > {_MAX_UNCOMPRESSED}")
                        return False
                    if member.is_dir():
                        continue
                    # 拒绝对路径 / 驱动符 / ../ 遍历
                    if member.filename.startswith(("/", "\\")) or ":" in member.filename.split("/")[0]:
                        logger.error(f"zip-slip 拒绝: 非法路径 {member.filename!r}")
                        return False
                    dest = (base / member.filename).resolve()
                    try:
                        dest.relative_to(base)
                    except ValueError:
                        logger.error(f"zip-slip 拒绝: {member.filename!r} 越界 {base}")
                        return False
                # E-13: 压缩比上限 (跳过压缩大小为 0 的存档)
                if total_compressed > 0 and total_uncompressed / total_compressed > _MAX_RATIO:
                    logger.error(f"zip 炸弹拒绝: 压缩比 {total_uncompressed / total_compressed:.1f} > {_MAX_RATIO}")
                    return False

                top_dirs = set()
                for n in names:
                    parts = n.split("/")
                    if len(parts) > 1:
                        top_dirs.add(parts[0])
                if not top_dirs:
                    logger.error("zip 文件结构无效: 无顶层目录")
                    return False
                plugin_name = top_dirs.pop()
                target = (base / plugin_name).resolve()
                # CR-22: rmtree 前校验 target 在 plugins_dir 内, 防越界删除
                try:
                    target.relative_to(base)
                except ValueError:
                    logger.error(f"插件目录越界, 拒绝删除: {target}")
                    return False
                if target.exists():
                    shutil.rmtree(target)
                # CR-22: 安全解压 — 逐条校验后再写 (extractall 已被上述校验替代)
                for member in zf.infolist():
                    zf.extract(member, str(base))
                logger.info(f"zip 插件已安装: {plugin_name}")
                return True
        except zipfile.BadZipFile as e:
            logger.error(f"zip 文件无效: {e}")
            return False
        except Exception as e:
            logger.error(f"zip 安装失败: {e}")
            return False

    def _safe_rmtree(self, name: str) -> bool:
        """CR-22: 仅允许删除 plugins_dir 内的目录, 拒越界 (防 ../ 遍历)。

        E-13: target==base (name="" 或 ".") 拒绝 — relative_to(base) 对 base 本身不抛,
        旧版会 rmtree(plugins_dir) 删插件根目录。
        """
        base = self._plugins_dir.resolve()
        target = (base / name).resolve()
        if target == base:
            logger.error(f"拒删除插件根目录: name={name!r} (E-13 防 target==base)")
            return False
        try:
            target.relative_to(base)
        except ValueError:
            logger.error(f"插件目录越界, 拒绝删除: {target}")
            return False
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
        return True

    def _install_dir(self, src_dir: Path) -> bool:
        manifest_path = src_dir / "manifest.json"
        if not manifest_path.exists():
            logger.error(f"插件目录缺少 manifest.json: {src_dir}")
            return False
        manifest = PluginManifest.from_json(manifest_path)
        if not manifest:
            return False
        target = self._plugins_dir / manifest.name
        if target.exists():
            if not self._safe_rmtree(manifest.name):
                return False
        shutil.copytree(str(src_dir), str(target))
        logger.info(f"插件已安装: {manifest.name} -> {target}")
        return True

    def uninstall(self, name: str) -> bool:
        self.unload(name)
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.exists():
            logger.warning(f"插件目录不存在: {name}")
            return False
        if not self._safe_rmtree(name):
            return False
        logger.info(f"插件已卸载删除: {name}")
        return True

    def list_plugins(self) -> List[PluginManifest]:
        return list(self._loaded.values())

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded

    def import_from_claude_desktop(self, config_path: str = "") -> List[str]:
        """从 Claude Desktop 配置导入 MCP server — P2-8。

        读取 claude_desktop_config.json 的 mcpServers, 每个 server 写为 cowork 插件
        manifest (external MCP server, 不加载节点, 记录 command/args/env 供 MCP client 调用)。
        返回导入的插件名列表。
        """
        import json
        import os

        if not config_path:
            config_path = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
        cfg = Path(config_path)
        if not cfg.exists():
            logger.warning(f"Claude Desktop 配置不存在: {cfg}")
            return []
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"Claude Desktop 配置解析失败: {e}")
            return []
        servers = data.get("mcpServers", {})
        if not servers:
            logger.info("Claude Desktop 配置无 mcpServers")
            return []
        imported = []
        for name, spec in servers.items():
            spec = spec or {}
            # MD-17: 校验 command — 非空 + 绝对路径或白名单可执行名, 拒盲信外部 spec
            command = str(spec.get("command", "")).strip()
            args = spec.get("args", [])
            env = spec.get("env", {})
            if not command:
                logger.warning(f"跳过 Claude Desktop MCP server (command 为空): {name}")
                continue
            if not self._is_safe_mcp_command(command, args, env):
                logger.warning(f"跳过 Claude Desktop MCP server (command 校验失败): {name} command={command!r}")
                continue
            manifest = PluginManifest(
                name=f"mcp_{name}",
                version="0.1.0",
                description=f"Imported from Claude Desktop: {name}",
                author="claude-desktop-import",
                nodes=[],
                dependencies=[],
                entry_point="external_mcp",
                sandbox=False,
            )
            target = self._plugins_dir / manifest.name
            target.mkdir(parents=True, exist_ok=True)
            (target / "manifest.json").write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # MD-17: 记录 source hash (command+args) 供运行时校验未被篡改
            source_hash = hashlib.sha256(
                json.dumps({"command": command, "args": args}, sort_keys=True).encode()
            ).hexdigest()
            (target / "mcp_server.json").write_text(
                json.dumps(
                    {
                        "command": command,
                        "args": args,
                        "env": self._sanitize_mcp_env(env),
                        "source_hash": source_hash,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            imported.append(manifest.name)
            logger.info(
                f"已从 Claude Desktop 导入 MCP server: {name} -> {manifest.name} source_hash={source_hash[:16]}"
            )
        return imported

    @staticmethod
    def _is_safe_mcp_command(command: str, args: Any, env: Any) -> bool:
        # MD-17: command 须非空且不含 shell 元字符; args 须为 list; 拒明显危险调用
        if not isinstance(args, list):
            return False
        shell_meta = {";", "|", "&", "`", "$", "(", ")", ">", "<", "\n", "\r"}
        if any(ch in command for ch in shell_meta):
            return False
        # 拒危险命令名 (防拉起任意进程)
        dangerous = {"rm", "rmdir", "mkfs", "dd", "shred", "curl", "wget", "nc", "bash", "sh"}
        base = Path(command).name
        if base in dangerous:
            return False
        for a in args:
            if not isinstance(a, (str, int, float, bool)):
                return False
            if isinstance(a, str) and any(ch in a for ch in shell_meta):
                return False
        return True

    @staticmethod
    def _sanitize_mcp_env(env: Any) -> Dict[str, str]:
        # MD-17: env 仅保留字符串值, 拒非字符串/空键 (防注入畸形 env)
        if not isinstance(env, dict):
            return {}
        safe: Dict[str, str] = {}
        for k, v in env.items():
            if isinstance(k, str) and k and isinstance(v, (str, int, float, bool)):
                safe[k] = str(v)
        return safe


# E-13: 模块级单例 — 旧版各 CLI 命令各 new PluginLoader() 致 _loaded/_node_map 状态分散,
# 已加载插件在不同实例间不可见 (卸载/列表命令看到空)。统一单例保证状态一致。
_DEFAULT_PLUGIN_LOADER: Optional[PluginLoader] = None


def get_plugin_loader(plugins_dir: str = "") -> PluginLoader:
    """E-13: 返回进程级 PluginLoader 单例 (首次调用惰性构造)。"""
    global _DEFAULT_PLUGIN_LOADER
    if _DEFAULT_PLUGIN_LOADER is None:
        _DEFAULT_PLUGIN_LOADER = PluginLoader(plugins_dir=plugins_dir)
        logger.debug("PluginLoader 单例已构造")
    return _DEFAULT_PLUGIN_LOADER
