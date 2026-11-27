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

# Stage 2: secret key detection — log/export redaction (plaintext values never enter logs, prevents secret leakage)
_SECRET_KEY_SUBSTR = ("token", "secret", "api_key", "apikey", "password", "passwd", "credential")
_REDACTED = "[REDACTED]"


def _is_secret_key(key: str) -> bool:
    if not isinstance(key, str) or not key:
        return False
    kl = key.lower()
    return any(sub in kl for sub in _SECRET_KEY_SUBSTR)


def _redact_value(key: str, value: Any) -> Any:
    """Secret key value redaction — returns a REDACTED placeholder (log/export use only, never mutates _store)."""
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
                # A-7: singleton loads from disk on first construction, config survives restart
                # (scoped_folder/auth_token/trusted allowlist/permission level etc.).
                # Tests using reset_instance then rebuilding follow the same path.
                inst._load_silent()
                cls._instance = inst
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _load_silent(self) -> None:
        # A-7: load failure does not block startup (missing file on first run is normal), log only.
        try:
            self.load()
        except Exception as e:
            logger.warning(f"ConfigCenter startup load failed (ignored, using defaults): {e}")

    def _persist_silent(self) -> None:
        # A-7: auto-persist after set/delete/reset; failure logs only, never raises (in-memory value already effective).
        try:
            self.save()
        except Exception as e:
            logger.warning(f"ConfigCenter auto-persist failed (in-memory value effective): {e}")

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
            # Stage 2: secret key log redaction — never leaks plaintext values
            logger.info(
                f"ConfigCenter.set key={key} old={_redact_value(key, old_value)} "
                f"new={_redact_value(key, value)} source={source}"
            )
        self._notify_observers(change)
        # A-7: wiring — write implies persist, survives restart
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
        # A-7: wiring — delete implies persist
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
        # MD-11: iterate over a snapshot; prevents RuntimeError when a callback observes/unobserves _observers mid-iteration
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
        # MD-12: atomic write — temp + os.replace + 0o600 + flock, prevents torn/leaky concurrent writes
        # Stage 6: secret values encrypted at rest (when FUSION_ENCRYPTION_KEY is set), non-secrets stay plaintext.
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
        """Stage 6: encrypt a secret key's value; non-secrets stay plaintext. Without a key, plaintext + WARN."""
        raw = asdict(entry)
        value = raw.get("value")
        if _is_secret_key(key) and isinstance(value, str) and value:
            try:
                from fusion_cowork.security.encryption import encrypt_at_rest

                raw["value"] = encrypt_at_rest(value)
            except ImportError:
                logger.warning("security.encryption unavailable, secret stored in plaintext")
        return raw

    def _decrypt_entry(self, key: str, value: Any) -> Any:
        """Stage 6: decrypt an encrypted value; non-ciphertext (no fernet: prefix) returned as-is (back compatible with plaintext)."""
        if _is_secret_key(key) and isinstance(value, str) and value.startswith("fernet:"):
            try:
                from fusion_cowork.security.encryption import decrypt_at_rest

                return decrypt_at_rest(value)
            except ImportError:
                logger.warning("security.encryption unavailable, ciphertext cannot be decrypted")
        return value

    def load(self, path: str = "") -> int:
        # LO-4: load is mutually exclusive with set/delete, prevents concurrent _store mutation
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
        # A-7: full clear also persists, prevents deleted config resurrecting after restart
        self._persist_silent()
        return count

    def to_dict(self) -> Dict[str, Any]:
        # Stage 2: secret key value redaction (export/display never leaks plaintext)
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
