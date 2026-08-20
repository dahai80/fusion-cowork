from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

from ..engine.node import BaseNode, NodeRegistry
from .manifest import PluginManifest

logger = logging.getLogger(__name__)


class PluginLoader:
    def __init__(self, plugins_dir: str = ""):
        self._plugins_dir = (
            Path(plugins_dir).expanduser() if plugins_dir else Path.home() / ".fusion-cowork" / "plugins"
        )
        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        self._loaded: Dict[str, PluginManifest] = {}
        self._node_map: Dict[str, List[str]] = {}

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
            import asyncio as _asyncio

            from .sandbox import PluginSandbox, SandboxStatus

            sandbox = PluginSandbox()
            logger.info(f"插件 {name} 标记 sandbox=true，执行预检 (rlimit 子进程)")
            try:
                loop = _asyncio.new_event_loop()
                pre = loop.run_until_complete(
                    sandbox.execute(
                        plugin_name=name,
                        command=sys.executable,
                        args=[str(entry_file)],
                    )
                )
                loop.close()
            except Exception as pe:
                logger.error(f"插件 {name} 沙箱预检异常: {pe}")
                return []
            if pre.status != SandboxStatus.STOPPED:
                logger.error(f"插件 {name} 沙箱预检失败: status={pre.status} exit={pre.exit_code} err={pre.error}")
                return []
            logger.info(f"插件 {name} 沙箱预检通过")

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
            (target / "manifest.json").write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            # 记录 MCP server spec 供运行时 MCP client 拉起
            (target / "mcp_server.json").write_text(
                json.dumps({"command": spec.get("command", ""), "args": spec.get("args", []), "env": spec.get("env", {})}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            imported.append(manifest.name)
            logger.info(f"已从 Claude Desktop 导入 MCP server: {name} -> {manifest.name}")
        return imported
