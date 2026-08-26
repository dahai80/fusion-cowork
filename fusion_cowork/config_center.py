from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_instance_lock = threading.Lock()

_CONFIG_DIR = os.environ.get("FUSION_CONFIG_DIR") or os.path.expanduser("~/.fusion-cowork")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")

# Stage 2: secret key 识别 — 日志/导出脱敏 (明文 value 不进日志, 防 secret 泄漏)
_SECRET_KEY_SUBSTR = ("token", "secret", "api_key", "apikey", "password", "passwd", "credential")
_REDACTED = "[REDACTED]"


def _is_secret_key(key: str) -> bool:
    if not isinstance(key, str) or not key:
        return False
    kl = key.lower()
    return any(sub in kl for sub in _SECRET_KEY_SUBSTR)


def _redact_value(key: str, value: Any) -> Any:
    """secret key 值脱敏 — 返 REDACTED 占位 (仅日志/导出用, 不改 _store 原值)。"""
    if _is_secret_key(key) and value not in (None, "", 0, False):
        return _REDACTED
    return value


@dataclass
class ConfigEntry:
    key: str
    value: Any
    updated_at: float = 0.0
    source: str = "user"

    def __post_init__(self):
        if not self.updated_at:
            self.updated_at = time.time()


@dataclass
class ConfigChange:
    key: str
    old_value: Any
    new_value: Any
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class ConfigObserver:
    def __init__(self, callback: Callable, keys: Optional[List[str]] = None):
        self.callback = callback
        self.keys = keys
        self._id = f"obs_{id(callback)}_{int(time.time() * 1000)}"

    def matches(self, key: str) -> bool:
        if self.keys is None:
            return True
        return key in self.keys


class ConfigCenter:
    _instance: Optional[ConfigCenter] = None

    def __init__(self, config_file: str = ""):
        self._store: Dict[str, ConfigEntry] = {}
        self._observers: List[ConfigObserver] = []
        self._lock = threading.RLock()
        self._config_file = config_file or _CONFIG_FILE
        self._defaults: Dict[str, Any] = {
            "engine.max_retries": 3,
            "engine.retry_delay": 1.0,
            "engine.timeout": 300,
            "engine.concurrency": 4,
            "ai.base_url": "http://localhost:11432",
            "ai.model": "default",
            "ai.temperature": 0.7,
            "ai.max_tokens": 2048,
            "scheduler.timezone": "Asia/Shanghai",
            "scheduler.max_concurrent": 5,
            "mcp.host": "127.0.0.1",
            "mcp.port": 11438,
            "permission.level": "manual",
            "log.level": "INFO",
            "log.file": "",
            "plugin.dir": os.path.expanduser("~/.fusion-cowork/plugins"),
            "browser.enabled": True,
            "space.enabled": True,
            "workspace.scoped_folder": "",
            "workspace.enforce_scope": False,
        }

    @classmethod
    def get_instance(cls) -> ConfigCenter:
        with _instance_lock:
            if cls._instance is None:
                inst = cls()
                # A-7: 单例首次构造即从磁盘加载, 重启不丢配置
                # (scoped_folder/auth_token/trusted 白名单/权限 level 等)。
                # 测试用 reset_instance 置空后重建仍走此路径。
                inst._load_silent()
                cls._instance = inst
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _load_silent(self) -> None:
        # A-7: 加载失败不影响启动 (首次运行无文件属正常), 仅日志。
        try:
            self.load()
        except Exception as e:
            logger.warning(f"ConfigCenter 启动加载失败 (忽略, 走默认): {e}")

    def _persist_silent(self) -> None:
        # A-7: set/delete/reset 后自动落盘, 失败仅日志不抛 (内存值已生效)。
        try:
            self.save()
        except Exception as e:
            logger.warning(f"ConfigCenter 自动持久化失败 (内存值已生效): {e}")

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._store:
            return self._store[key].value
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value: Any, source: str = "user") -> ConfigChange:
        with self._lock:
            old_value = self.get(key)
            entry = ConfigEntry(key=key, value=value, source=source)
            self._store[key] = entry
            change = ConfigChange(key=key, old_value=old_value, new_value=value)
            # Stage 2: secret key 日志脱敏 — 不泄明文 value
            logger.info(
                f"ConfigCenter.set key={key} old={_redact_value(key, old_value)} "
                f"new={_redact_value(key, value)} source={source}"
            )
        self._notify_observers(change)
        # A-7: 接线 — 写即落盘, 重启不丢
        self._persist_silent()
        return change

    def delete(self, key: str) -> bool:
        with self._lock:
            if key not in self._store:
                logger.debug(f"ConfigCenter.delete key={key} not found")
                return False
            old_value = self._store.pop(key).value
            change = ConfigChange(key=key, old_value=old_value, new_value=None)
            logger.info(f"ConfigCenter.delete key={key}")
        self._notify_observers(change)
        # A-7: 接线 — 删即落盘
        self._persist_silent()
        return True

    def has(self, key: str) -> bool:
        return key in self._store or key in self._defaults

    def list_keys(self, prefix: str = "") -> List[str]:
        keys = set(self._store.keys()) | set(self._defaults.keys())
        if prefix:
            keys = {k for k in keys if k.startswith(prefix)}
        return sorted(keys)

    def get_all(self, prefix: str = "") -> Dict[str, Any]:
        result = {}
        for key in self.list_keys(prefix):
            result[key] = self.get(key)
        return result

    def set_default(self, key: str, value: Any) -> None:
        if key not in self._defaults:
            self._defaults[key] = value
            logger.debug(f"ConfigCenter.set_default key={key} value={value}")

    def observe(self, callback: Callable, keys: Optional[List[str]] = None) -> ConfigObserver:
        observer = ConfigObserver(callback, keys=keys)
        self._observers.append(observer)
        logger.info(f"ConfigCenter.observe registered observer={observer._id} keys={keys}")
        return observer

    def unobserve(self, observer: ConfigObserver) -> bool:
        before = len(self._observers)
        self._observers = [o for o in self._observers if o._id != observer._id]
        removed = before - len(self._observers)
        if removed:
            logger.info(f"ConfigCenter.unobserve removed observer={observer._id}")
        return removed > 0

    def _notify_observers(self, change: ConfigChange) -> None:
        # MD-11: 迭代快照, 防 callback 内 observe/unobserve 改 _observers 致 RuntimeError
        for observer in list(self._observers):
            if not observer.matches(change.key):
                continue
            try:
                if asyncio.iscoroutinefunction(observer.callback):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(observer.callback(change))
                    except RuntimeError:
                        asyncio.run(observer.callback(change))
                else:
                    observer.callback(change)
            except Exception as e:
                logger.error(f"ConfigCenter observer error key={change.key}: {e}")

    def save(self, path: str = "") -> None:
        # MD-12: 原子写 — temp + os.replace + 0o600 + flock, 防并发写撕裂/泄漏
        # Stage 6: secret 值加密落盘 (FUSION_ENCRYPTION_KEY 设了), 非密保明文。
        target = path or self._config_file
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with self._lock:
            data = {
                "entries": {k: self._encrypt_entry(k, v) for k, v in self._store.items()},
                "defaults": dict(self._defaults),
            }
            try:
                import fcntl

                use_flock = True
            except ImportError:
                use_flock = False
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp", prefix=".cfg_")
            try:
                if use_flock:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, target)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        logger.info(f"ConfigCenter.save path={target} entries={len(self._store)}")

    def _encrypt_entry(self, key: str, entry: ConfigEntry) -> Dict[str, Any]:
        """Stage 6: secret key 的 value 加密; 非密保明文。无 key 则明文 + WARN。"""
        raw = asdict(entry)
        value = raw.get("value")
        if _is_secret_key(key) and isinstance(value, str) and value:
            try:
                from fusion_cowork.security.encryption import encrypt_at_rest

                raw["value"] = encrypt_at_rest(value)
            except ImportError:
                logger.warning("security.encryption 不可用, secret 明文落盘")
        return raw

    def _decrypt_entry(self, key: str, value: Any) -> Any:
        """Stage 6: 加密值解密; 非密文 (无 fernet: 前缀) 原样返 (向后兼容明文)。"""
        if _is_secret_key(key) and isinstance(value, str) and value.startswith("fernet:"):
            try:
                from fusion_cowork.security.encryption import decrypt_at_rest

                return decrypt_at_rest(value)
            except ImportError:
                logger.warning("security.encryption 不可用, 密文无法解")
        return value

    def load(self, path: str = "") -> int:
        # LO-4: load 与 set/delete 互斥, 防并发改 _store
        target = path or self._config_file
        if not os.path.exists(target):
            logger.debug(f"ConfigCenter.load file not found: {target}")
            return 0
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        with self._lock:
            if "defaults" in data and isinstance(data["defaults"], dict):
                self._defaults.update(data["defaults"])
            entries = data.get("entries", {})
            loaded = 0
            for key, entry_data in entries.items():
                if isinstance(entry_data, dict):
                    self._store[key] = ConfigEntry(
                        key=entry_data.get("key", key),
                        value=self._decrypt_entry(key, entry_data.get("value")),
                        updated_at=entry_data.get("updated_at", 0.0),
                        source=entry_data.get("source", "user"),
                    )
                    loaded += 1
        logger.info(f"ConfigCenter.load path={target} entries={loaded}")
        return loaded

    def reset(self, key: str = "") -> int:
        if key:
            if key in self._store:
                old_value = self._store.pop(key).value
                change = ConfigChange(key=key, old_value=old_value, new_value=self.get(key))
                self._notify_observers(change)
                logger.info(f"ConfigCenter.reset key={key}")
                return 1
            return 0
        count = len(self._store)
        self._store.clear()
        logger.info(f"ConfigCenter.reset all entries={count}")
        # A-7: 全清也落盘, 防重启复活已删配置
        self._persist_silent()
        return count

    def to_dict(self) -> Dict[str, Any]:
        # Stage 2: secret key 值脱敏 (导出/展示不泄明文)
        def _redact_entry(k: str, v: ConfigEntry) -> Dict[str, Any]:
            d = asdict(v)
            d["value"] = _redact_value(k, d.get("value"))
            return d

        return {
            "entries": {k: _redact_entry(k, v) for k, v in self._store.items()},
            "defaults": {k: _redact_value(k, v) for k, v in self._defaults.items()},
            "observer_count": len(self._observers),
        }

    def import_dict(self, data: Dict[str, Any], source: str = "import") -> int:
        count = 0
        for key, value in data.items():
            self.set(key, value, source=source)
            count += 1
        logger.info(f"ConfigCenter.import_dict count={count} source={source}")
        return count
