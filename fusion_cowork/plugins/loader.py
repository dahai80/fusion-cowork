from __future__ import annotations

import asyncio
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
            "type": "object", "properties": {}, "required": [],
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

        try:
            res = await sandbox.execute(
                plugin_name=f"sandbox_node:{self._class_name}",
                command=sys.executable,
                args=[runner_path],
                stdin_data=req,
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
        payload_str = out[idx + len(_RESULT_MARKER):].strip() if idx >= 0 else out.strip()
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
    """
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
        payload_str = out[idx + len(_RESULT_MARKER):].strip() if idx >= 0 else out.strip()
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
        try:
            logger.info(f"从 URL 下载插件: {url}")
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
            if not url.lower().endswith(".zip"):
                logger.error(f"URL 插件仅支持 .zip: {url}")
                return False
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(resp.content)
                tmp_path = Path(tmp.name)
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
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                top_dirs = set()
                for n in names:
                    parts = n.split("/")
                    if len(parts) > 1:
                        top_dirs.add(parts[0])
                if not top_dirs:
                    logger.error("zip 文件结构无效: 无顶层目录")
                    return False
                plugin_name = top_dirs.pop()
                target = self._plugins_dir / plugin_name
                if target.exists():
                    shutil.rmtree(target)
                zf.extractall(str(self._plugins_dir))
                logger.info(f"zip 插件已安装: {plugin_name}")
                return True
        except zipfile.BadZipFile as e:
            logger.error(f"zip 文件无效: {e}")
            return False
        except Exception as e:
            logger.error(f"zip 安装失败: {e}")
            return False

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
            shutil.rmtree(target)
        shutil.copytree(str(src_dir), str(target))
        logger.info(f"插件已安装: {manifest.name} -> {target}")
        return True

    def uninstall(self, name: str) -> bool:
        self.unload(name)
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.exists():
            logger.warning(f"插件目录不存在: {name}")
            return False
        shutil.rmtree(plugin_dir)
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
            # 记录 MCP server spec 供运行时 MCP client 拉起
            (target / "mcp_server.json").write_text(
                json.dumps(
                    {"command": spec.get("command", ""), "args": spec.get("args", []), "env": spec.get("env", {})},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            imported.append(manifest.name)
            logger.info(f"已从 Claude Desktop 导入 MCP server: {name} -> {manifest.name}")
        return imported
