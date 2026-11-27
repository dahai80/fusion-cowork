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
from .registry import get_plugin_registry

logger = logging.getLogger(__name__)

_RESULT_MARKER = "__SANDBOX_RESULT__"


def _version_lt(a: str, b: str) -> bool:
    """a < b semantic version comparison (non-numeric segments ignored)."""

    def _t(v: str) -> tuple:
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    return _t(a) < _t(b)


class SandboxedNode(BaseNode):
    """Sandboxed node wrapper — P1-6.

    Wraps a third-party plugin node: node metadata (name/display_name/category/schema) registers in the
    main process, but execute() is delegated to a PluginSandbox subprocess, so plugin code never enters
    the main process.

    Each plugin node gets its own subclass generated via make_sandboxed_node_class(),
    fixing class-level name/display_name metadata to satisfy NodeRegistry by-name registration.
    """

    name = "sandboxed_node"
    # i18n: user-facing
    display_name = "沙箱插件节点"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "沙箱内执行的第三方插件节点"
    icon = "📦"
    # i18n: user-facing
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
                error="sandboxed node missing entry_file/class_name",
                summary="sandboxed node configuration incomplete",
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

        # LO-7: read manifest.timeout_seconds from node meta to override the sandbox default timeout (>0 overrides)
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
            logger.info(f"sandboxed node {self._class_name} timeout override: {meta_timeout}s (declared in manifest)")

        try:
            res = await sandbox.execute(
                plugin_name=f"sandbox_node:{self._class_name}",
                command=sys.executable,
                args=[runner_path],
                stdin_data=req,
                limits=call_limits,
            )
        except Exception as e:
            logger.error(f"sandboxed node execution exception {self._class_name}: {e}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"sandbox execution exception: {e}",
                summary="sandbox subprocess failed to start",
            )

        out = res.stdout or ""
        idx = out.find(_RESULT_MARKER)
        if idx < 0:
            # LO-5: no result-frame marker -> subprocess emitted no agreed result; do not treat the whole stdout as payload
            stdout_tail = out[-256:]
            logger.error(
                f"sandboxed node {self._class_name} produced no result frame (marker missing); "
                f"stdout tail={stdout_tail!r} stderr={(res.stderr or '')[-256:]!r}"
            )
            return NodeResult(
                status=NodeStatus.FAILED,
                error="child process produced no result frame (RESULT_MARKER missing)",
                summary=f"sandbox returned output without marker rc={res.exit_code}",
            )
        payload_str = out[idx + len(_RESULT_MARKER) :].strip()
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as e:
            err_tail = (res.stderr or "")[-512:]
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"sandbox result parse failed: {e}; stderr tail: {err_tail}",
                summary="sandbox returned non-JSON",
            )

        if not payload.get("ok"):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(payload.get("error", "unknown sandbox error")),
                summary="execution failed inside sandbox",
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
            summary=data.get("summary", "") or f"sandbox execution finished rc={res.exit_code}",
        )


def make_sandboxed_node_class(meta: Dict[str, Any], sandbox: Any) -> type[BaseNode]:
    """Generate a sandboxed wrapper subclass for a single plugin node — P1-6.

    The subclass fixes class-level name/display_name/category metadata to satisfy NodeRegistry registration.
    LO-6: unknown meta keys are logged as warnings (never silently swallowed) so manifest field typos surface.
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
        logger.warning(f"sandboxed plugin node meta has unknown keys: {sorted(unknown)} (will be ignored)")

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
            # i18n: user-facing
            "display_name": meta.get("display_name", "沙箱插件节点"),
            "category": category,
            # i18n: user-facing
            "description": meta.get("description", "沙箱内执行的第三方插件节点"),
            "icon": meta.get("icon", "📦"),
            # i18n: user-facing
            "default_label": meta.get("default_label", "插件节点"),
        },
    )
    cls._sandbox_meta = meta  # type: ignore[attr-defined]
    cls._sandbox_instance = sandbox  # type: ignore[attr-defined]
    return cls


def _sandboxed_factory(cls: type[BaseNode]):
    """Override __init__ to inject entry_file/class_name/sandbox/schema."""
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
        require_signing = self._require_signing()
        registry = get_plugin_registry()
        for plugin_dir in sorted(self._plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "manifest.json"
            if not manifest_path.exists():
                logger.debug(f"skipping directory without manifest: {plugin_dir.name}")
                continue
            manifest = PluginManifest.from_json(manifest_path)
            if not manifest:
                continue
            # Stage 7: signature check — refuse unsigned/failed verification when require_signing=true
            if require_signing and not self._verify_signing(manifest):
                logger.warning(f"plugin {manifest.name} unsigned or verification failed, refused by require_signing")
                continue
            # Stage 7: version downgrade check — skip if registry already has a higher version (no silent downgrade)
            reg_v = registry.get_version(manifest.name)
            if reg_v and _version_lt(manifest.version, reg_v):
                logger.warning(
                    f"plugin {manifest.name} on-disk version {manifest.version} < registry installed {reg_v}, skipping (downgrade protection)"
                )
                continue
            manifests.append(manifest)
        logger.info(f"discovered {len(manifests)} plugins")
        return manifests

    def _require_signing(self) -> bool:
        """Read config plugins.require_signing (default false, opt-in)."""
        try:
            from ..config_center import ConfigCenter

            cc = ConfigCenter.get_instance()
            return bool(cc.get("plugins.require_signing", False))
        except Exception:
            return False

    def _verify_signing(self, manifest: PluginManifest) -> bool:
        """Stage 7: verify manifest.signature against the public key list configured in config."""
        from .signing import get_configured_public_keys, verify_any_key

        if not manifest.signature:
            return False
        # read the raw manifest dict (including signature) for verification
        manifest_path = self._plugins_dir / manifest.name / "manifest.json"
        try:
            import json

            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"failed to read manifest for plugin {manifest.name}, cannot verify: {e}")
            return False
        return verify_any_key(raw, get_configured_public_keys())

    def load(self, name: str) -> List[BaseNode]:
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.is_dir():
            logger.error(f"plugin directory does not exist: {name}")
            return []

        manifest_path = plugin_dir / "manifest.json"
        manifest = PluginManifest.from_json(manifest_path)
        if not manifest:
            logger.error(f"failed to load plugin manifest: {name}")
            return []

        entry_file = plugin_dir / f"{manifest.entry_point}.py"
        if not entry_file.exists():
            logger.error(f"plugin entry file does not exist: {entry_file}")
            return []

        if manifest.sandbox:
            return self._load_sandboxed(name, manifest, entry_file)

        # CR-21: sandbox=false plugins run exec_module in the main process = potential RCE,
        # they must be in the config plugins.trusted allowlist, otherwise loading is refused.
        trusted = self._get_trusted_plugins()
        if name not in trusted:
            logger.error(
                f"plugin {name} has sandbox=false and is not in the plugins.trusted allowlist, refusing to load"
                f" (explicit trust required: config set plugins.trusted '[...{name}...]' or launch arg --trust-plugin {name})"
            )
            return []

        module_name = f"fusion_cowork_plugin_{name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(entry_file))
            if spec is None or spec.loader is None:
                logger.error(f"cannot load plugin module: {entry_file}")
                return []
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f"plugin module execution failed {name}: {e}")
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
                    logger.info(f"registered plugin node: {attr.name} ({attr.__name__})")
            except TypeError:
                continue

        self._loaded[name] = manifest
        self._node_map[name] = [n.name for n in registered]
        logger.info(f"plugin {name} loaded: {len(registered)} nodes")
        return registered

    def _load_sandboxed(self, name: str, manifest: PluginManifest, entry_file: Path) -> List[BaseNode]:
        """Sandboxed load path — P1-6: plugin code never enters the main process.

        Uses the sandbox_runner subprocess introspect to discover node metadata,
        generates a SandboxedNode wrapper subclass per node registered into the main process,
        and runs back in the subprocess on execute(). The main process never exec_modules plugin code.
        """
        from .sandbox import PluginSandbox, SandboxStatus

        sandbox = PluginSandbox()
        runner_path = str(Path(__file__).parent / "sandbox_runner.py")
        req = json.dumps(
            {"action": "introspect", "entry_file": str(entry_file)},
            ensure_ascii=False,
        )
        logger.info(f"plugin {name} sandbox=true, introspecting node metadata in subprocess (rlimit isolation)")
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
            logger.error(f"plugin {name} sandbox introspect exception: {pe}")
            return []
        if pre.status != SandboxStatus.STOPPED:
            logger.error(f"plugin {name} sandbox introspect failed: status={pre.status} err={pre.error}")
            return []

        out = pre.stdout or ""
        idx = out.find(_RESULT_MARKER)
        payload_str = out[idx + len(_RESULT_MARKER) :].strip() if idx >= 0 else out.strip()
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as e:
            logger.error(f"plugin {name} introspect result parse failed: {e}; stderr={pre.stderr[-256:]}")
            return []
        if not payload.get("ok"):
            logger.error(f"plugin {name} introspect error: {payload.get('error')}")
            return []

        node_metas = payload.get("data", {}).get("nodes", [])
        if not node_metas:
            logger.warning(f"plugin {name} sandbox load found no nodes")
            return []

        registered = []
        for meta in node_metas:
            meta["entry_file"] = str(entry_file)
            # LO-7: pass manifest.timeout_seconds through to node meta, overriding the sandbox default timeout at execute time
            meta["timeout_seconds"] = manifest.timeout_seconds
            wrapper_cls = make_sandboxed_node_class(meta, sandbox)
            _sandboxed_factory(wrapper_cls)
            NodeRegistry.register(wrapper_cls)
            registered.append(wrapper_cls)
            logger.info(f"registered sandboxed plugin node: {wrapper_cls.name} ({wrapper_cls.__name__})")

        self._loaded[name] = manifest
        self._node_map[name] = [c.name for c in registered]
        self._sandboxes[name] = sandbox
        logger.info(f"plugin {name} sandbox load complete: {len(registered)} nodes (fully subprocess isolated)")
        return registered

    def _get_trusted_plugins(self) -> List[str]:
        """CR-21: read the config plugins.trusted allowlist (sandbox=false plugins require explicit trust)."""
        try:
            from ..config_center import ConfigCenter

            cc = ConfigCenter.get_instance()
            trusted = cc.get("plugins.trusted", [])
            if isinstance(trusted, list):
                return [str(t) for t in trusted]
            if isinstance(trusted, str):
                return [t.strip() for t in trusted.split(",") if t.strip()]
        except Exception as e:
            logger.debug(f"failed to read plugins.trusted, treating as empty allowlist: {e}")
        return []

    def _register_installed(self, name: str, manifest: Optional[PluginManifest] = None) -> None:
        """Stage 7: register into the registry after install (version + signing status + checksum).

        If manifest is missing, re-read from plugins_dir (the zip install path has no manifest object).
        """
        registry = get_plugin_registry()
        if manifest is None:
            manifest_path = self._plugins_dir / name / "manifest.json"
            if manifest_path.exists():
                manifest = PluginManifest.from_json(manifest_path)
        version = manifest.version if manifest else "0.0.0"
        sig_valid = False
        if manifest and manifest.signature:
            sig_valid = self._verify_signing(manifest)
        checksum = ""
        if manifest:
            mp = self._plugins_dir / name / "manifest.json"
            if mp.exists():
                checksum = hashlib.sha256(mp.read_bytes()).hexdigest()
        if not registry.register(name, version, sig_valid, checksum):
            logger.warning(f"plugin {name} registry registration failed (possibly a downgrade)")

    def load_all(self) -> Dict[str, List[BaseNode]]:
        results = {}
        for manifest in self.discover():
            nodes = self.load(manifest.name)
            results[manifest.name] = nodes
        return results

    def unload(self, name: str) -> bool:
        if name not in self._loaded:
            logger.warning(f"plugin not loaded: {name}")
            return False
        node_names = self._node_map.pop(name, [])
        for node_name in node_names:
            NodeRegistry.unregister(node_name)
            logger.info(f"unregistered plugin node: {node_name}")
        del self._loaded[name]
        self._sandboxes.pop(name, None)
        logger.info(f"plugin {name} unloaded")
        return True

    def install(self, path: str) -> bool:
        if path.startswith("http://") or path.startswith("https://"):
            return self._install_url(path)
        src = Path(path).expanduser().resolve()
        if not src.exists():
            logger.error(f"install source does not exist: {path}")
            return False

        if src.suffix == ".zip":
            return self._install_zip(src)
        elif src.is_dir():
            return self._install_dir(src)
        else:
            logger.error(f"unsupported install source type: {path}")
            return False

    def _install_url(self, url: str) -> bool:
        import tempfile

        try:
            import httpx
        except ImportError:
            logger.error("installing URL plugins requires httpx (pip install httpx)")
            return False
        # MD-16: only https allowed, http refused (prevents MITM tampering of plugin packages)
        if not url.lower().startswith("https://"):
            logger.error(f"URL plugins allow https:// only: {url} (http refused against tampering)")
            return False
        if not url.lower().endswith(".zip"):
            logger.error(f"URL plugins support .zip only: {url}")
            return False
        _MAX_DOWNLOAD = 50 * 1024 * 1024  # 50 MiB cap, prevents oversized packages exhausting memory
        try:
            logger.info(f"downloading plugin from URL: {url}")
            tmp_path = Path(tempfile.mktemp(suffix=".zip"))
            downloaded = 0
            sha = hashlib.sha256()
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                resp = client.get(url)
                # MD-16: explicitly refuse redirects (prevents open redirect to intranet/malicious sources); clients must use direct links
                if resp.is_redirect:
                    logger.error(f"URL plugin refused redirect: {url} -> {resp.headers.get('location', '?')}")
                    return False
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD:
                            logger.error(f"URL plugin exceeds size cap {_MAX_DOWNLOAD} bytes, aborting download: {url}")
                            f.close()
                            try:
                                tmp_path.unlink()
                            except OSError:
                                pass
                            return False
                        sha.update(chunk)
                        f.write(chunk)
            logger.info(f"URL plugin download complete: {downloaded} bytes, sha256={sha.hexdigest()[:16]}")
            try:
                return self._install_zip(tmp_path)
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"URL plugin install failed: {e}")
            return False

    def _install_zip(self, zip_path: Path) -> bool:
        base = self._plugins_dir.resolve()
        # E-13: zip bomb protection — cap entry count/total uncompressed size/compression ratio
        _MAX_ZIP_ENTRIES = 10000
        _MAX_UNCOMPRESSED = 512 * 1024 * 1024  # 512MB
        _MAX_RATIO = 200  # uncompressed/compressed ratio; above this is bomb suspicion
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                # E-13: entry count cap
                if len(names) > _MAX_ZIP_ENTRIES:
                    logger.error(f"zip bomb refused: entry count {len(names)} > {_MAX_ZIP_ENTRIES}")
                    return False
                # CR-22: per-entry zip-slip check — every extraction target must land inside plugins_dir
                total_uncompressed = 0
                total_compressed = 0
                for member in zf.infolist():
                    total_uncompressed += member.file_size
                    total_compressed += member.compress_size
                    if total_uncompressed > _MAX_UNCOMPRESSED:
                        logger.error(f"zip bomb refused: total uncompressed {total_uncompressed} > {_MAX_UNCOMPRESSED}")
                        return False
                    if member.is_dir():
                        continue
                    # refuse absolute paths / drive letters / ../ traversal
                    if member.filename.startswith(("/", "\\")) or ":" in member.filename.split("/")[0]:
                        logger.error(f"zip-slip refused: illegal path {member.filename!r}")
                        return False
                    dest = (base / member.filename).resolve()
                    try:
                        dest.relative_to(base)
                    except ValueError:
                        logger.error(f"zip-slip refused: {member.filename!r} escapes {base}")
                        return False
                # E-13: compression ratio cap (skip archives with compressed size 0)
                if total_compressed > 0 and total_uncompressed / total_compressed > _MAX_RATIO:
                    logger.error(f"zip bomb refused: compression ratio {total_uncompressed / total_compressed:.1f} > {_MAX_RATIO}")
                    return False

                top_dirs = set()
                for n in names:
                    parts = n.split("/")
                    if len(parts) > 1:
                        top_dirs.add(parts[0])
                if not top_dirs:
                    logger.error("invalid zip structure: no top-level directory")
                    return False
                plugin_name = top_dirs.pop()
                target = (base / plugin_name).resolve()
                # CR-22: verify target is inside plugins_dir before rmtree, preventing out-of-scope deletion
                try:
                    target.relative_to(base)
                except ValueError:
                    logger.error(f"plugin directory escapes plugins_dir, refusing deletion: {target}")
                    return False
                if target.exists():
                    shutil.rmtree(target)
                # CR-22: safe extraction — write only after per-entry checks (extractall replaced by the checks above)
                for member in zf.infolist():
                    zf.extract(member, str(base))
                logger.info(f"zip plugin installed: {plugin_name}")
                self._register_installed(plugin_name)
                return True
        except zipfile.BadZipFile as e:
            logger.error(f"invalid zip file: {e}")
            return False
        except Exception as e:
            logger.error(f"zip install failed: {e}")
            return False

    def _safe_rmtree(self, name: str) -> bool:
        """CR-22: only allow deleting directories inside plugins_dir, refuse escapes (prevents ../ traversal).

        E-13: target==base (name="" or ".") is refused — relative_to(base) does not raise for base itself,
        and the old version would rmtree(plugins_dir), deleting the plugin root.
        """
        base = self._plugins_dir.resolve()
        target = (base / name).resolve()
        if target == base:
            logger.error(f"refusing to delete plugin root directory: name={name!r} (E-13 prevents target==base)")
            return False
        try:
            target.relative_to(base)
        except ValueError:
            logger.error(f"plugin directory escapes plugins_dir, refusing deletion: {target}")
            return False
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
        return True

    def _install_dir(self, src_dir: Path) -> bool:
        manifest_path = src_dir / "manifest.json"
        if not manifest_path.exists():
            logger.error(f"plugin directory missing manifest.json: {src_dir}")
            return False
        manifest = PluginManifest.from_json(manifest_path)
        if not manifest:
            return False
        target = self._plugins_dir / manifest.name
        if target.exists():
            if not self._safe_rmtree(manifest.name):
                return False
        shutil.copytree(str(src_dir), str(target))
        logger.info(f"plugin installed: {manifest.name} -> {target}")
        self._register_installed(manifest.name, manifest)
        return True

    def uninstall(self, name: str) -> bool:
        self.unload(name)
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.exists():
            logger.warning(f"plugin directory does not exist: {name}")
            return False
        if not self._safe_rmtree(name):
            return False
        get_plugin_registry().unregister(name)
        logger.info(f"plugin uninstalled and deleted: {name}")
        return True

    def list_plugins(self) -> List[PluginManifest]:
        return list(self._loaded.values())

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded

    def import_from_claude_desktop(self, config_path: str = "") -> List[str]:
        """Import MCP servers from the Claude Desktop config — P2-8.

        Reads mcpServers from claude_desktop_config.json and writes each server as a cowork plugin
        manifest (external MCP server, no nodes loaded; records command/args/env for MCP client calls).
        Returns the list of imported plugin names.
        """
        import json
        import os

        if not config_path:
            config_path = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
        cfg = Path(config_path)
        if not cfg.exists():
            logger.warning(f"Claude Desktop config not found: {cfg}")
            return []
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"failed to parse Claude Desktop config: {e}")
            return []
        servers = data.get("mcpServers", {})
        if not servers:
            logger.info("no mcpServers in Claude Desktop config")
            return []
        imported = []
        for name, spec in servers.items():
            spec = spec or {}
            # MD-17: validate command — non-empty + absolute path or allowlisted executable name; refuse blind trust of external spec
            command = str(spec.get("command", "")).strip()
            args = spec.get("args", [])
            env = spec.get("env", {})
            if not command:
                logger.warning(f"skipping Claude Desktop MCP server (empty command): {name}")
                continue
            if not self._is_safe_mcp_command(command, args, env):
                logger.warning(f"skipping Claude Desktop MCP server (command validation failed): {name} command={command!r}")
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
            # MD-17: record source hash (command+args) to verify no tampering at runtime
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
                f"imported MCP server from Claude Desktop: {name} -> {manifest.name} source_hash={source_hash[:16]}"
            )
        return imported

    @staticmethod
    def _is_safe_mcp_command(command: str, args: Any, env: Any) -> bool:
        # MD-17: command must be non-empty and free of shell metacharacters; args must be a list; refuse obviously dangerous invocations
        if not isinstance(args, list):
            return False
        shell_meta = {";", "|", "&", "`", "$", "(", ")", ">", "<", "\n", "\r"}
        if any(ch in command for ch in shell_meta):
            return False
        # refuse dangerous command names (prevents spawning arbitrary processes)
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
        # MD-17: env keeps only string values, rejects non-string/empty keys (prevents malformed env injection)
        if not isinstance(env, dict):
            return {}
        safe: Dict[str, str] = {}
        for k, v in env.items():
            if isinstance(k, str) and k and isinstance(v, (str, int, float, bool)):
                safe[k] = str(v)
        return safe


# E-13: module-level singleton — the old version new'd a PluginLoader() per CLI command, scattering
# _loaded/_node_map state (loaded plugins invisible across instances: unload/list commands saw empty).
# One singleton keeps state consistent.
_DEFAULT_PLUGIN_LOADER: Optional[PluginLoader] = None


def get_plugin_loader(plugins_dir: str = "") -> PluginLoader:
    """E-13: return the process-level PluginLoader singleton (lazily constructed on first call)."""
    global _DEFAULT_PLUGIN_LOADER
    if _DEFAULT_PLUGIN_LOADER is None:
        _DEFAULT_PLUGIN_LOADER = PluginLoader(plugins_dir=plugins_dir)
        logger.debug("PluginLoader singleton constructed")
    return _DEFAULT_PLUGIN_LOADER
