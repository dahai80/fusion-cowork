# issue #595: QQ IMAP settings persistence for the demand web API.
# The env file (~/.fusion-cowork/demand_imap.env) is the operator-editable
# source (PUT /api/demand/settings); process env vars keep priority over the
# file so deployments can still override via the environment. The password is
# write-only: read_settings never returns it, only a presence flag, and no
# log line ever includes credential values.

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_KEYS = (
    "FUSION_QQ_IMAP_HOST",
    "FUSION_QQ_IMAP_PORT",
    "FUSION_QQ_IMAP_USER",
    "FUSION_QQ_IMAP_PASSWORD",
    "FUSION_QQ_IMAP_FOLDER",
)


def env_file_path() -> Path:
    return Path(
        os.environ.get("FUSION_COWORK_DEMAND_IMAP_ENV", str(Path.home() / ".fusion-cowork" / "demand_imap.env"))
    )


def last_test_path() -> Path:
    return env_file_path().with_suffix(".test.json")


def load_env_file() -> Dict[str, str]:
    # parse KEY=VALUE lines into os.environ (file first, process env wins);
    # returns the parsed file values for reporting
    path = env_file_path()
    parsed: Dict[str, str] = {}
    if not path.exists():
        return parsed
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in _KEYS:
                parsed[key] = value.strip()
                os.environ.setdefault(key, parsed[key])
    except Exception as e:
        logger.warning(f"demand settings env file unreadable: {e}")
    return parsed


def write_settings(host: str, port: int, user: str, password: str, folder: str) -> Path:
    # password empty = keep the previously stored one (write-only semantics)
    path = env_file_path()
    existing = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() in _KEYS:
                    existing[key.strip()] = value.strip()
    values = {
        "FUSION_QQ_IMAP_HOST": host or existing.get("FUSION_QQ_IMAP_HOST", ""),
        "FUSION_QQ_IMAP_PORT": str(port or existing.get("FUSION_QQ_IMAP_PORT", "")),
        "FUSION_QQ_IMAP_USER": user or existing.get("FUSION_QQ_IMAP_USER", ""),
        "FUSION_QQ_IMAP_PASSWORD": password or existing.get("FUSION_QQ_IMAP_PASSWORD", ""),
        "FUSION_QQ_IMAP_FOLDER": folder or existing.get("FUSION_QQ_IMAP_FOLDER", ""),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    logger.info(f"demand settings written: {path} (keys={len(values)})")
    return path


def read_settings() -> Dict[str, Any]:
    load_env_file()
    return {
        "host": os.environ.get("FUSION_QQ_IMAP_HOST", ""),
        "port": int(os.environ.get("FUSION_QQ_IMAP_PORT", "993") or 993),
        "user": os.environ.get("FUSION_QQ_IMAP_USER", ""),
        "folder": os.environ.get("FUSION_QQ_IMAP_FOLDER", "INBOX"),
        "password_present": bool(os.environ.get("FUSION_QQ_IMAP_PASSWORD", "")),
        "env_path": str(env_file_path()),
        "last_test": read_last_test(),
    }


def read_last_test() -> Optional[Dict[str, Any]]:
    path = last_test_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def record_last_test(result: Dict[str, Any]) -> None:
    path = last_test_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**result, "tested_at": time.time()}, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)


async def test_imap() -> Dict[str, Any]:
    # read-only EXAMINE connect — reports latency + UIDNEXT cursor probe;
    # never logs (or returns) the credential
    import imaplib

    cfg_host = os.environ.get("FUSION_QQ_IMAP_HOST", "")
    cfg_port = int(os.environ.get("FUSION_QQ_IMAP_PORT", "993") or 993)
    cfg_user = os.environ.get("FUSION_QQ_IMAP_USER", "")
    cfg_password = os.environ.get("FUSION_QQ_IMAP_PASSWORD", "")
    cfg_folder = os.environ.get("FUSION_QQ_IMAP_FOLDER", "INBOX")
    if not cfg_user or not cfg_password:
        return {"ok": False, "error": "FUSION_QQ_IMAP_USER/FUSION_QQ_IMAP_PASSWORD not set"}
    start = time.monotonic()

    def _probe() -> int:
        mail = imaplib.IMAP4_SSL(cfg_host, cfg_port)
        try:
            mail.login(cfg_user, cfg_password)
            status, data = mail.select(cfg_folder, readonly=True)  # EXAMINE semantics
            if status != "OK":
                raise RuntimeError(f"select failed: {status}")
            status, uid_data = mail.uid("search", None, "ALL")
            if status != "OK":
                raise RuntimeError(f"uid search failed: {status}")
            uids = uid_data[0].split() if uid_data and uid_data[0] else []
            return int(uids[-1]) if uids else 0
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    try:
        cursor = await asyncio.to_thread(_probe)
        result = {
            "ok": True,
            "latency_ms": round((time.monotonic() - start) * 1000),
            "cursor": cursor,
            "folder": cfg_folder,
        }
    except Exception as e:
        result = {"ok": False, "latency_ms": round((time.monotonic() - start) * 1000), "error": type(e).__name__}
    record_last_test(result)
    logger.info(f"demand settings test: ok={result['ok']} latency_ms={result['latency_ms']}")
    return result
