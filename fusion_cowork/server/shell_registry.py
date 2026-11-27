"""Union Web shell registration — issue #97 (PRD §5.2, aligned with agent-studio shell_registry).

On web serve startup, registers web/manifest.json with the fusion-gateway shell registry
(POST /api/v1/shell/manifests, fg-key auth) and keeps it alive with periodic heartbeats
(the gateway grays it out after heartbeat_timeout). env-gated — requires all of:
- FUSION_SHELL_GATEWAY_URL + FUSION_ADMIN_KEY
Optional FUSION_SHELL_MANIFEST overrides the manifest path. Silently skipped when unset (default OFF).
The /shell/fusion-cowork/ static proxy is itself gateway configuration — the dynamic manifest only drives the menu entry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

GATEWAY_URL_ENV = "FUSION_SHELL_GATEWAY_URL"
MANIFEST_PATH_ENV = "FUSION_SHELL_MANIFEST"
UPSTREAM_OVERRIDE_ENV = "FUSION_COWORK_WEB_UPSTREAM"
REGISTER_INTERVAL_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 5.0
# issue #448: once the consecutive-failure threshold is hit, at most one ERROR summary per hour (replaces per-beat WARNING spam)
FAIL_STREAK_ERROR_THRESHOLD = 10
ERROR_SUMMARY_INTERVAL_SECONDS = 3600.0

# issue #448: registration state exposed via /health (supervisor/monitoring can alert on shell_registered=false)
_REGISTRATION_STATE: Dict[str, Any] = {
    "enabled": False,
    "registered": False,
    "gateway": "",
    "last_error": "",
}

# issue #168 (K4): persist path (0600) for the gateway-issued fm- module key.
# The static env key remains the back-compat override path; once issued, the file takes precedence over the env value.
MODULE_KEY_PATH = Path.home() / ".fusion-cowork" / "web_module_key"


def load_module_key() -> str:
    try:
        return MODULE_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_module_key(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    MODULE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODULE_KEY_PATH.write_text(key + "\n", encoding="utf-8")
    MODULE_KEY_PATH.chmod(0o600)
    logger.info(f"Gateway-issued module key persisted: {MODULE_KEY_PATH} ({_mask_key(key)})")
    return True


def _gateway_admin_key() -> str:
    # issue #204: admin key single-point resolution goes through fusion_core.config.admin_key (canonical name
    # FUSION_ADMIN_KEY; the prod-missing RuntimeError is contained inside fusion-core). On bare installs without
    # fusion-core, the fallback accepts only the canonical name — no duplicate alias chain in this repo.
    try:
        from fusion_core.config import admin_key

        return admin_key()
    except ImportError:
        key = os.environ.get("FUSION_ADMIN_KEY", "")
        if not key:
            logger.warning(
                "FUSION_ADMIN_KEY not set: shell gateway registration unavailable (request it from the fusion-gateway admin API)"
            )
        return key


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _extract_issued_key(payload: Any) -> str:
    # issue #173/#174: the fusion-gateway K1 issuance response (writeShellKeyResponse) carries the plaintext in raw_key,
    # remaining field names kept for old/custom gateway compatibility
    if not isinstance(payload, dict):
        return ""
    for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("data")
    if isinstance(nested, dict):
        for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
            value = nested.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "web" / "manifest.json"


def shell_registration_enabled() -> bool:
    # Short-circuit when the URL is unset, without triggering admin key resolution (the prod-missing-key RuntimeError
    # is only a configuration error when registration is actually attempted)
    if not os.environ.get(GATEWAY_URL_ENV, "").strip():
        return False
    return bool(_gateway_admin_key().strip())


def registration_health() -> Dict[str, Any]:
    # issue #448: read-only snapshot — consumed by /health and monitoring, never contains the admin key
    return dict(_REGISTRATION_STATE)


def _gateway_url_valid(gateway: str) -> bool:
    # issue #448: startup self-check — the URL must parse as http(s)://host[:port], otherwise fail fast
    try:
        parsed = urlparse(gateway)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _classify_registry_error(e: Exception, gateway: str) -> str:
    # issue #448: error classification — connection errors name the target URL, the rest pass through as-is; operators can tell
    # configuration mistakes (wrong host/port) apart from gateway failures
    try:
        import httpx

        if isinstance(e, httpx.ConnectError):
            return f"connection refused/unreachable: {gateway} ({e})"
        if isinstance(e, httpx.HTTPError):
            return f"http error against {gateway}: {e}"
    except ImportError:
        pass
    return str(e)


def _mark_registration(registered: bool, gateway: str = "", error: str = "") -> None:
    _REGISTRATION_STATE["registered"] = registered
    if gateway:
        _REGISTRATION_STATE["gateway"] = gateway
    _REGISTRATION_STATE["last_error"] = error


def load_manifest() -> Dict[str, Any]:
    path = Path(os.environ.get(MANIFEST_PATH_ENV, "").strip() or _default_manifest_path())
    if not path.exists():
        # i18n: user-facing
        raise FileNotFoundError(f"shell manifest 不存在: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not manifest.get("id"):
        # i18n: user-facing
        raise ValueError(f"shell manifest 缺少 id: {path}")
    # issue #125: gateway dynamic proxy (#232 contract) resolves upstream/api_key —
    # the manifest declares defaults; FUSION_COWORK_WEB_UPSTREAM overrides deployment-specific ports;
    # api_key is injected via the runtime variable api_key_env points to (empty values are not injected or persisted).
    # issue #168 (K4): when env is unset, no api_key is sent in the registration body — the gateway (K1) issues
    # an fm- key and returns it once in the response, persisted by _registry_loop_body; with env configured
    # the static override path is kept (back-compat with old gateways).
    override = os.environ.get(UPSTREAM_OVERRIDE_ENV, "").strip()
    if override:
        manifest["upstream"] = override
    if not manifest.get("upstream"):
        manifest["upstream"] = "http://127.0.0.1:11461"
    key_env = str(manifest.get("api_key_env", "")).strip()
    api_key = os.environ.get(key_env, "").strip() if key_env else ""
    if api_key:
        manifest["api_key"] = api_key
    else:
        manifest.pop("api_key", None)
    logger.info(
        f"shell manifest loaded: {path} (id={manifest['id']} upstream={manifest['upstream']} api_key={'set' if api_key else 'unset'})"
    )
    return manifest


async def _registry_loop_body(client: Any, manifest: Dict[str, Any], interval: float) -> None:
    module_id = str(manifest["id"])
    gateway = os.environ.get(GATEWAY_URL_ENV, "").strip().rstrip("/")
    # issue #448: startup self-check — invalid URL fails fast, no infinite loop
    if not _gateway_url_valid(gateway):
        logger.error(
            f"shell registration disabled (FUSION_SHELL_GATEWAY_URL invalid, must be http(s)://host[:port]): '{gateway}'"
        )
        _mark_registration(False, gateway=gateway, error="invalid gateway url")
        return
    admin_key = _gateway_admin_key().strip()
    admin_headers = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}
    registered = False
    # issue #448: log the resolved target URL at loop start (never the admin key) — ps -E cannot read
    # the daemon env, so this log is the only place operators can see where registration points
    logger.info(f"shell registration loop started: gateway={gateway} module={module_id} interval={interval:g}s")
    _REGISTRATION_STATE["enabled"] = True
    _mark_registration(False, gateway=gateway, error="")
    consecutive_failures = 0
    last_error_log_at = 0.0
    while True:
        try:
            if not registered:
                resp = await client.post(f"{gateway}/api/v1/shell/manifests", json=manifest, headers=admin_headers)
                if resp.status_code == 200:
                    registered = True
                    consecutive_failures = 0
                    _mark_registration(True, error="")
                    logger.info(f"shell manifest registered: {module_id}")
                    # issue #168 (K4): persist the gateway-issued key when the registration response carries one (plaintext returned only this once)
                    issued = _extract_issued_key(_safe_json(resp))
                    if issued and issued != load_module_key():
                        save_module_key(issued)
                    elif issued:
                        logger.info("Gateway-issued key matches the stored value, not rewriting the file")
                else:
                    # i18n: user-facing
                    raise RuntimeError(f"shell manifest 注册失败: {resp.status_code} {resp.text[:200]}")
            else:
                # issue #168 (K4): heartbeat authenticates with the issued fm- key; falls back to the admin key when none was issued
                module_key = load_module_key()
                beat_key = module_key or admin_key
                headers = {"Authorization": f"Bearer {beat_key}", "Content-Type": "application/json"}
                # issue #177: heartbeat carries the manifest (gateway #284 contract) — re-read from disk each beat,
                # so on-disk edits (adding/removing pages etc.) converge into the gateway registry within one heartbeat interval, no restart needed;
                # a failed re-read keeps the version from registration time, heartbeat keepalive is not interrupted
                try:
                    beat_manifest = load_manifest()
                except Exception as e:
                    logger.warning(f"shell heartbeat manifest re-read failed, keeping registration-time version: {e}")
                    beat_manifest = manifest
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests/{module_id}/heartbeat", json=beat_manifest, headers=headers
                )
                if resp.status_code == 404:
                    logger.info(f"shell heartbeat 404, re-registering {module_id}")
                    registered = False
                elif resp.status_code == 401 and module_key:
                    # Issued key rotated/revoked — re-register via the admin key to obtain a fresh one
                    logger.warning(f"shell heartbeat 401 (issued key invalid), re-registering {module_id}")
                    registered = False
                elif resp.status_code != 200:
                    # i18n: user-facing
                    raise RuntimeError(f"shell 心跳失败: {resp.status_code} {resp.text[:200]}")
                # Heartbeat succeeded — reset the failure streak, /health reports registered again
                consecutive_failures = 0
                _mark_registration(True, error="")
        except asyncio.CancelledError:
            logger.info(f"shell registration loop cancelled (module={module_id})")
            raise
        except Exception as e:
            consecutive_failures += 1
            detail = _classify_registry_error(e, gateway)
            _mark_registration(False, error=detail)
            now = time.monotonic()
            if consecutive_failures >= FAIL_STREAK_ERROR_THRESHOLD:
                # issue #448: sustained-failure down-sampling — after the threshold, at most one ERROR summary per hour
                # (with the failure streak count and target URL), no more per-beat WARNING spam
                if consecutive_failures == FAIL_STREAK_ERROR_THRESHOLD or now - last_error_log_at >= (
                    ERROR_SUMMARY_INTERVAL_SECONDS
                ):
                    logger.error(
                        f"shell registration failed {consecutive_failures} times in a row (latest error): {detail}"
                    )
                    last_error_log_at = now
            else:
                logger.warning(f"shell registration loop error (will retry): {detail}")
        await asyncio.sleep(interval)


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


async def shell_registry_loop(client: Any = None, interval: float = REGISTER_INTERVAL_SECONDS) -> None:
    """Register once, keep alive with heartbeats; re-register after a gateway restart (404). Failures only warn and retry, the service is unaffected."""
    try:
        manifest = load_manifest()
    except Exception as e:
        logger.error(f"shell registration disabled (manifest unavailable): {e}")
        return
    if client is None:
        import httpx

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as own:
            await _registry_loop_body(own, manifest, interval)
    else:
        await _registry_loop_body(client, manifest, interval)
