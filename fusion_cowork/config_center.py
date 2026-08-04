from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.fusion-cowork")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")


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
        self._id = f"obs_{id(callback)}_{int(time.time()*1000)}"

    def matches(self, key: str) -> bool:
        if self.keys is None:
            return True
        return key in self.keys


class ConfigCenter:
    _instance: Optional[ConfigCenter] = None

    def __init__(self, config_file: str = ""):
        self._store: Dict[str, ConfigEntry] = {}
        self._observers: List[ConfigObserver] = []
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
        }

    @classmethod
    def get_instance(cls) -> ConfigCenter:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._store:
            return self._store[key].value
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value: Any, source: str = "user") -> ConfigChange:
        old_value = self.get(key)
        entry = ConfigEntry(key=key, value=value, source=source)
        self._store[key] = entry
        change = ConfigChange(key=key, old_value=old_value, new_value=value)
        logger.info(f"ConfigCenter.set key={key} old={old_value} new={value} source={source}")
        self._notify_observers(change)
        return change

    def delete(self, key: str) -> bool:
        if key not in self._store:
            logger.debug(f"ConfigCenter.delete key={key} not found")
            return False
        old_value = self._store.pop(key).value
        change = ConfigChange(key=key, old_value=old_value, new_value=None)
        logger.info(f"ConfigCenter.delete key={key}")
        self._notify_observers(change)
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
        for observer in self._observers:
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
        target = path or self._config_file
        os.makedirs(os.path.dirname(target), exist_ok=True)
        data = {
            "entries": {k: asdict(v) for k, v in self._store.items()},
            "defaults": dict(self._defaults),
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"ConfigCenter.save path={target} entries={len(self._store)}")

    def load(self, path: str = "") -> int:
        target = path or self._config_file
        if not os.path.exists(target):
            logger.debug(f"ConfigCenter.load file not found: {target}")
            return 0
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        if "defaults" in data and isinstance(data["defaults"], dict):
            self._defaults.update(data["defaults"])
        entries = data.get("entries", {})
        loaded = 0
        for key, entry_data in entries.items():
            if isinstance(entry_data, dict):
                self._store[key] = ConfigEntry(
                    key=entry_data.get("key", key),
                    value=entry_data.get("value"),
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
        return count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": {k: asdict(v) for k, v in self._store.items()},
            "defaults": dict(self._defaults),
            "observer_count": len(self._observers),
        }

    def import_dict(self, data: Dict[str, Any], source: str = "import") -> int:
        count = 0
        for key, value in data.items():
            self.set(key, value, source=source)
            count += 1
        logger.info(f"ConfigCenter.import_dict count={count} source={source}")
        return count
