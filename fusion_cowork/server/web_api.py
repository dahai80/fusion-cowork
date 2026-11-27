"""Union Web module API — issue #94 (PRD N-6).

Backend HTTP surface of the fusion-cowork web/ UI package:
- REST: node catalog / workflow definition CRUD (file storage) / run lifecycle (async run + cancel)
- Static hosting: web/dist SPA (gateway /shell/fusion-cowork/ proxies to this service)
- Auth: FUSION_COWORK_WEB_API_KEY static key (Bearer / x-api-key), injected by gateway proxy;
  allow when unset (default binds 127.0.0.1 only, local-first) — aligns with the agent-studio #327 pattern
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# issue #463: fire-and-forget audit tasks need a strong ref or the loop GC's them mid-run
_PENDING_AUDIT_TASKS: set = set()

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_DEFAULT_SPACE_DATA_DIR = str(Path.home() / ".fusion-cowork" / "spaces")
_RUN_TTL_SECONDS = 3600


def _api_key() -> str:
    return os.environ.get("FUSION_COWORK_WEB_API_KEY", "")


def _module_key() -> str:
    # issue #168 (K4): fm- module key issued at gateway registration (persisted 0600); empty when not issued.
    try:
        from .shell_registry import load_module_key

        return load_module_key()
    except Exception:
        return ""


def _service_key() -> str:
    # issue #565: dedicated service-to-service credential for storefront bff
    # engine calls (NOT a user key) — provisioned via env/secret per node
    return os.environ.get("FUSION_COWORK_SERVICE_KEY", "")


def _accepted_keys() -> List[str]:
    # Dual-active window: env static key and gateway-issued key are both valid (24h grace, aligned with gateway K1).
    keys = [k for k in (_api_key(), _module_key(), _service_key()) if k]
    return list(dict.fromkeys(keys))


def auth_configured() -> bool:
    return bool(_accepted_keys())


def check_static_key(key: str) -> Optional[Dict[str, Any]]:
    accepted = _accepted_keys()
    if not accepted or not key:
        return None
    if key == _api_key():
        return {"valid": True, "key_id": "static-env", "permissions": ["admin"]}
    if key == _module_key():
        return {"valid": True, "key_id": "module-issued", "permissions": ["admin"]}
    if key == _service_key():
        # issue #565: service credential — scoped to the run lifecycle
        # endpoints (scope enforced by the auth middleware, not here)
        return {"valid": True, "key_id": "service", "permissions": ["runs"]}
    return None


def extract_caller_key(headers: Any) -> str:
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("x-api-key") or "").strip()


# ── issue #608: demand-mail-rail seed — workflow definition + hourly cron ──
# Nodes + API shipped in #585-#588/#595; this wires them into the pipeline:
# the workflow def is seeded idempotently at app build (existing file wins,
# operator edits survive); the hourly cron task is created on first serve
# from the startup hook — AFTER scheduler.start() has restored persisted
# tasks, so the workflow_id check never duplicates across restarts, and a
# restored task gets its executor re-bound (P1-12 auto-pause lifts).
_DEMAND_MAIL_RAIL_DEF = {
    "name": "demand-mail-rail",
    "description": "Demand mail rail: poll_qq_imap -> normalize_lead -> match (threshold 3.0) -> dual_draft; hourly cron from the demand pipeline tab",
    "workflow": {
        "nodes": {
            "poll": {
                "id": "poll",
                "name": "poll_qq_imap",
                "params": {},
                "config": {"continue_on_error": True, "label": "IMAP 轮询"},  # i18n: user-facing
            },
            "normalize": {
                "id": "normalize",
                "name": "normalize_lead",
                "params": {},
                "config": {"continue_on_error": True, "label": "线索归一化"},  # i18n: user-facing
            },
            "match": {
                "id": "match",
                "name": "match",
                "params": {"threshold": 3.0},
                "config": {"label": "能力匹配"},  # i18n: user-facing
            },
            "draft": {
                "id": "draft",
                "name": "dual_draft",
                "params": {},
                "config": {"continue_on_error": True, "label": "双模型草稿"},  # i18n: user-facing
            },
        },
        "edges": [
            {"source_id": "poll", "target_id": "normalize"},
            {"source_id": "normalize", "target_id": "match"},
            {"source_id": "match", "target_id": "draft"},
        ],
    },
}


class WorkflowDefStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root) if root else Path.home() / ".fusion-cowork" / "web" / "workflows"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, workflow_id: str) -> Path:
        safe = "".join(c for c in workflow_id if c.isalnum() or c in "-_")
        if not safe or safe != workflow_id:
            raise ValueError(f"非法工作流 id: {workflow_id}")  # i18n: user-facing
        return self._root / f"{safe}.json"

    def list(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for p in sorted(self._root.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                items.append(
                    {
                        "id": data.get("id", p.stem),
                        "name": data.get("name", p.stem),
                        "description": data.get("description", ""),
                        "updated_at": data.get("updated_at", 0),
                        "node_count": len((data.get("workflow") or {}).get("nodes") or []),
                    }
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"workflow definition read failed {p.name}: {e}")
        return items

    def get(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._path(workflow_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return None

    def save(self, workflow_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = {
            "id": workflow_id,
            "name": payload.get("name", workflow_id),
            "description": payload.get("description", ""),
            "workflow": payload.get("workflow", {}),
            "updated_at": time.time(),
        }
        # issue #485: template seed metadata rides along (seed-refresh bookkeeping,
        # per-run params re-assembly via template_id on /api/runs)
        for _meta_key in ("template_id", "source", "alias_of", "_seed_hash"):
            if _meta_key in payload:
                record[_meta_key] = payload[_meta_key]
        path = self._path(workflow_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=4), encoding="utf-8")
        os.replace(tmp, path)
        logger.info(f"workflow definition saved: {workflow_id}")
        return record

    def delete(self, workflow_id: str) -> bool:
        try:
            self._path(workflow_id).unlink()
            logger.info(f"workflow definition deleted: {workflow_id}")
            return True
        except (FileNotFoundError, ValueError):
            return False


_TEXT_EXTS = {".md", ".markdown", ".txt", ".html", ".htm", ".csv", ".json", ".yaml", ".yml"}
_MAX_ARTIFACT_BYTES = 64 * 1024
_MAX_ARTIFACTS = 20

# issue #142: block-level review sentinel — all-reject accepted_blocks → confirm returns confirmed=False
_EDIT_ALL_REJECTED = object()


def _narrow_pending_edit(permission_manager: Any, action_id: str, accepted_raw: List[Any]) -> Any:
    # Per-block accept/reject → rebuild a new value containing only the accepted blocks from the
    # pending entry's original old/new pair. Returns None when no narrowing applies (all accepted /
    # no edit pair); _EDIT_ALL_REJECTED means everything was rejected.
    try:
        entries = permission_manager.list_pending()
    except Exception as e:
        logger.warning(f"block review: failed to read pending queue: {e}")
        return None
    entry = next((e for e in entries if isinstance(e, dict) and e.get("action_id") == action_id), None)
    if not entry:
        return None
    raw_edit = entry.get("raw_edit") or {}
    old_val = raw_edit.get("old")
    new_val = raw_edit.get("new")
    if not isinstance(old_val, str) or not isinstance(new_val, str):
        return None
    try:
        from ..engine.ast_diff import apply_block_selection, text_diff_blocks

        blocks = text_diff_blocks(old_val, new_val)
        non_equal = {b["index"] for b in blocks if b["tag"] != "equal"}
        accepted = {int(i) for i in accepted_raw if isinstance(i, (int, float)) and float(i).is_integer()}
    except Exception as e:
        logger.warning(f"block review: block rebuild failed: {e}")
        return None
    selected = accepted & non_equal
    if not selected:
        return _EDIT_ALL_REJECTED
    if selected == non_equal:
        return None
    edited_new = apply_block_selection(old_val, new_val, sorted(selected))
    edited = dict(entry.get("params") or {})
    edited[str(raw_edit.get("new_key") or "new_text")] = edited_new
    logger.info(
        f"block review: action={action_id} accepted {len(selected)}/{len(non_equal)} blocks, narrowed {raw_edit.get('new_key')}"
    )
    return edited


def _team_poll_interval() -> float:
    # issue #190: fallback polling interval (seconds) for peers without push capability
    raw = os.environ.get("FUSION_INBOX_POLL_INTERVAL", "5")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(f"invalid FUSION_INBOX_POLL_INTERVAL: {raw!r}, falling back to 5s")
        return 5.0
    return value if value > 0 else 5.0


def _iso_to_epoch(value: Any) -> float:
    """Convert an ISO time string to epoch; invalid/empty values return 0."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _artifact_type(suffix: str) -> str:
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix in (".html", ".htm"):
        return "html"
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix in (".diff", ".patch"):
        # issue #142: diff/patch files render as unified diff
        return "diff"
    return "text"


def _step_diff_content(step: Any, out: Dict[str, Any]) -> str:
    # issue #142: diff artifact source — output carries its own diff field, or step input holds an edit pair (old/new)
    if isinstance(out.get("diff"), str) and out["diff"].strip():
        return out["diff"]
    inp = getattr(step, "input_data", None) or {}
    if not isinstance(inp, dict):
        return ""
    from ..engine.ast_diff import unified_diff_text

    for old_key, new_key in (("old_text", "new_text"), ("old_string", "new_string")):
        old_val = inp.get(old_key)
        new_val = inp.get(new_key)
        if isinstance(old_val, str) and isinstance(new_val, str) and (old_val or new_val):
            target = next(
                (inp.get(k) for k in ("file_path", "path") if isinstance(inp.get(k), str) and inp.get(k)),
                "",
            )
            return unified_diff_text(old_val, new_val, fromfile=target or old_key, tofile=target or new_key)
    return ""


ARTIFACT_REF_PATTERN = re.compile(r"\[Artifact:\s*(?P<name>[^|\]]+?)\s*\|\s*ID:\s*(?P<id>[A-Za-z0-9_\-]+)[^\]]*\]")

_ARTIFACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def find_artifact_refs(text: Any) -> List[Dict[str, str]]:
    # issue #233: extract lightweight artifact references from text in order (order-preserving, dedup); payload keeps the ~30 token ref unexpanded
    if not isinstance(text, str) or not text:
        return []
    seen: set = set()
    refs: List[Dict[str, str]] = []
    for m in ARTIFACT_REF_PATTERN.finditer(text):
        ref_id = m.group("id")
        if ref_id in seen:
            continue
        seen.add(ref_id)
        refs.append({"artifact_id": ref_id, "name": m.group("name").strip()})
    return refs


def _artifact_ref_text(card: Dict[str, Any]) -> str:
    # issue #233: rebuild the engine's lightweight ref_text (the ~30 token artifact.get format)
    return (
        f"[Artifact: {card.get('name', '')} | ID: {card.get('artifact_id', '')} | "
        f"Version: v{card.get('current_version') or 1} | Type: {card.get('type', '')} | "
        f"Summary: {card.get('summary', '')}]"
    )


async def _fetch_artifact_card_with_status(
    artifact_id: str, transport: Any = None
) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    # issue #237: return (card, upstream HTTP status). The status lets the caller tell
    # "missing / no permission" apart from "engine unreachable"; transport errors /
    # invalid id return (None, None).
    if not artifact_id or not _ARTIFACT_ID_PATTERN.match(artifact_id):
        return None, None
    base_url = os.environ.get("FUSION_ARTIFACTS_URL", "http://127.0.0.1:11451").rstrip("/")
    api_key = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
            resp = await client.get(f"{base_url}/api/v1/artifacts/{artifact_id}", headers=headers)
        if resp.status_code != 200:
            logger.warning("[artifacts] card fetch %s -> HTTP %s", artifact_id, resp.status_code)
            return None, resp.status_code
        data = resp.json()
    except Exception as exc:
        logger.warning("[artifacts] card fetch %s failed: %s", artifact_id, exc)
        return None, None
    payload: Any = data
    if isinstance(data, dict):
        # issue #243: upstream envelope three shapes — {data:{…}} (gateway style) / {artifact:{…}}
        # (engine-observed) / flat. First seen {artifact:{…}} left all card fields empty, so
        # deep-link prefill only had an empty name.
        for key in ("data", "artifact"):
            wrapped = data.get(key)
            if isinstance(wrapped, dict):
                payload = wrapped
                break
    if not isinstance(payload, dict):
        return None, 200
    card_id = str(payload.get("id") or artifact_id)
    card = {
        "artifact_id": card_id,
        "name": str(payload.get("name") or ""),
        "type": str(payload.get("type") or ""),
        "kind": str(payload.get("kind") or ""),
        "current_version": payload.get("current_version") or 1,
        "summary": str(payload.get("summary") or ""),
        "source_module": str(payload.get("source_module") or ""),
        "is_starred": bool(payload.get("is_starred")),
        "deep_link": f"/shell/fusion-artifacts/#/a/{card_id}",
    }
    return card, 200


async def _fetch_artifact_card(artifact_id: str, transport: Any = None) -> Optional[Dict[str, Any]]:
    # issue #233 compat entry: timeline cards only care about success/failure; the status is consumed by the #237 route.
    card, _ = await _fetch_artifact_card_with_status(artifact_id, transport=transport)
    return card


def _collect_artifacts(steps: List[Any]) -> List[Dict[str, Any]]:
    """W10: extract previewable artifacts from step outputs (file path first, then inline text)."""
    artifacts: List[Dict[str, Any]] = []
    for step in steps or []:
        if len(artifacts) >= _MAX_ARTIFACTS:
            break
        out = getattr(step, "output_data", None) or {}
        if not isinstance(out, dict):
            continue
        node_id = getattr(step, "node_id", "")
        node_name = getattr(step, "node_display_name", "") or getattr(step, "node_name", "")
        # issue #150: structured references in step output (file/message refs) surface with the artifacts
        citations = out.get("citations") if isinstance(out.get("citations"), list) else []
        # issue #142: edit-type steps produce a unified diff card — apply_edit old/new pair → diff artifact
        diff_content = _step_diff_content(step, out)
        if diff_content:
            artifacts.append(
                {
                    "artifact_id": f"{node_id}:diff",
                    "node_id": node_id,
                    "node_name": node_name,
                    "name": f"{node_name} · diff",
                    "type": "diff",
                    "path": "",
                    "content": diff_content[:_MAX_ARTIFACT_BYTES],
                    "truncated": len(diff_content) > _MAX_ARTIFACT_BYTES,
                }
            )
        path_val = next(
            (out[k] for k in ("file_path", "output_path", "path") if isinstance(out.get(k), str) and out.get(k)),
            "",
        )
        if path_val:
            p = Path(path_val).expanduser()
            suffix = p.suffix.lower()
            item: Dict[str, Any] = {
                "artifact_id": f"{node_id}:path",
                "node_id": node_id,
                "node_name": node_name,
                "name": p.name,
                "type": _artifact_type(suffix),
                "path": str(p),
                "content": "",
                "truncated": False,
                "citations": citations,
            }
            if suffix in _TEXT_EXTS:
                try:
                    raw = p.read_text(encoding="utf-8", errors="replace")
                    item["content"] = raw[:_MAX_ARTIFACT_BYTES]
                    item["truncated"] = len(raw) > _MAX_ARTIFACT_BYTES
                except OSError as e:
                    logger.warning(f"artifact read failed {p}: {e}")
            artifacts.append(item)
            continue
        for key in ("markdown", "html", "content", "report", "text"):
            val = out.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            artifacts.append(
                {
                    "artifact_id": f"{node_id}:{key}",
                    "node_id": node_id,
                    "node_name": node_name,
                    "name": f"{node_name} · {key}",
                    "type": "markdown" if key in ("markdown", "report") else key,
                    "path": "",
                    "content": val[:_MAX_ARTIFACT_BYTES],
                    "truncated": len(val) > _MAX_ARTIFACT_BYTES,
                    "citations": citations,
                }
            )
            break
    return artifacts


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        logger.warning(f"{name} invalid: {raw!r}, using {default}")
        return default


def _token_rollup(steps: List[Dict[str, Any]]) -> Dict[str, int]:
    # issue #461: per-run token attribution — aggregate per-step usage (issue #143 step-level data)
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for s in steps or []:
        t = s.get("tokens") if isinstance(s, dict) else None
        if not isinstance(t, dict):
            continue
        for k in tot:
            try:
                tot[k] += int(t.get(k, 0) or 0)
            except (TypeError, ValueError):
                continue
    return tot


def _spend_usd(total_tokens: int) -> float:
    # issue #461: spend estimation — constant 0.0 when unit price (USD per million tokens) is unset (honestly not estimated)
    price = _float_env("FUSION_COWORK_SPEND_USD_PER_MTOK", 0.0)
    if price <= 0 or total_tokens <= 0:
        return 0.0
    return round(total_tokens / 1_000_000 * price, 6)


def _failure_attribution(run_id: str, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # issue #558 (#132): edge-level (A caused B) handoff candidates for failed
    # runs — kernel FailureAnalyzer deterministic keyword heuristics (pinpointer
    # None = no model call, no replay); steps map to trajectory records
    # (event="error" on failed steps / error text, else "tool_call"); additive
    # response key only, any failure here degrades to "absent" (never raises).
    try:
        from types import SimpleNamespace

        from fusion_harness import FailureAnalyzer

        recs = []
        for idx, s in enumerate(record.get("steps") or []):
            err = str(s.get("error") or "")
            status = str(s.get("status") or "")
            text = " ".join(str(x) for x in (s.get("summary") or "", err) if x)
            recs.append(
                SimpleNamespace(
                    run_id=run_id,
                    ts=idx,
                    node_id=str(s.get("node_name") or s.get("node_id") or "workflow"),
                    # only failed steps carry "error" events — successful steps stay
                    # eventless so the kernel's repeated-tool-call heuristic does not
                    # fire on identical-looking DAG step payloads
                    event="error" if (err or status in ("failed", "denied", "blocked")) else "",
                    payload={"error": err, "status": status, "message": text[:200]},
                )
            )
        if not recs:
            return None
        report = FailureAnalyzer().analyze(recs)
        run_report = next(iter((report.get("runs") or {}).values()), None)
        if not run_report or not run_report.get("handoff_candidates"):
            return None
        return {
            "handoff_candidates": run_report.get("handoff_candidates") or [],
            "decisive_moment": run_report.get("decisive_moment") or {},
            "responsible_agent": run_report.get("responsible_agent") or "",
        }
    except Exception as exc:
        logger.warning(f"failure attribution skipped run={run_id}: {exc}")
        return None


class RunRegistry:
    def __init__(self, engine: Any, event_emitter: Any = None, run_store: Any = None):
        self._engine = engine
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # issue #372: stall watchdog — run-level last-event timestamp + periodic sweep (disabled when emitter/engine lack heartbeat support)
        self._last_event: Dict[str, float] = {}
        self._sweeper_started = False
        # B2: run event subscription — one queue per SSE client
        self._subs: Dict[str, List[asyncio.Queue]] = {}
        self._emitter = event_emitter
        # issue #191: run control-state persistence layer (not injected = pure in-memory, zero behavior change)
        self.run_store = run_store
        # issue #154: webhook push egress (not injected = no push, zero behavior change)
        self.push = None
        # issue #579: at-least-once completion notification — persistent outbox (not injected = original best-effort path)
        self.reliable_push = None
        self._push_flushing = False
        # issue #228: capability usage recording callback (not injected = no accounting, zero behavior change)
        self.capability_recorder = None
        # issue #516: golden run recording callback (not injected = no recording, zero behavior change)
        self.asset_recorder = None
        # issue #461: cross-run concurrency gate — per-key limit + optional global cap (env unset = no gate, zero behavior change)
        from ..engine.run_gate import RunGate

        self._gate = RunGate.from_env()
        if event_emitter is not None:
            event_emitter.on_event(self._on_event)

    @staticmethod
    def _public(record: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in record.items() if k != "workflow_def"}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        queues = self._subs.get(run_id)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._subs.pop(run_id, None)

    def _broadcast(self, run_id: str, item: Dict[str, Any]) -> None:
        for queue in list(self._subs.get(run_id, [])):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning(f"SSE queue full, dropped frame: {run_id}")

    def _schedule_push_flush(self, reliable: Any) -> None:
        # issue #579: dedup-scheduled flush — skip when a flush is already running; skip when no running event loop
        if getattr(self, "_push_flushing", False):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._push_flushing = True

        async def _flush() -> None:
            try:
                await reliable.flush_pending()
            except Exception as e:
                logger.warning(f"push outbox flush error (left for next attempt): {e}")
            finally:
                self._push_flushing = False

        loop.create_task(_flush())

    def _on_event(self, event: Any) -> None:
        run_id = getattr(event, "execution_id", "")
        record = self._runs.get(run_id)
        if record is None:
            return
        self._last_event[run_id] = time.time()
        try:
            execution = self._engine.get_execution(run_id)
        except Exception:
            execution = None
        if execution is not None:
            record["steps"] = [RunRegistry._step_dict(s) for s in execution.steps]
            # issue #461: per-run token/spend attribution — rolled live with events
            record["tokens"] = _token_rollup(record["steps"])
            record["spend_usd"] = _spend_usd(record["tokens"]["total_tokens"])
            # issue #571: MAST attribution rolled live (visible once failed nodes land a verdict)
            _attr = getattr(execution, "attribution", None)
            if _attr:
                record["attribution"] = _attr
            status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
            # issue #191: a paused record is never flipped back to running by run events —
            # engine pause checkpoints / HITL boundaries do not change execution.status; status flips are driven explicitly by resume() only
            if record.get("status") != "paused":
                record["status"] = status
        # issue #579: at-least-once node completion notification — node_end events enter the persistent outbox
        # (event_type is a str-mixin enum; compare directly with the string, no str())
        if self.reliable_push is not None and getattr(event, "event_type", "") == "node_end":
            evt_data = getattr(event, "data", None)
            evt_status = str(evt_data.get("status", "")) if isinstance(evt_data, dict) else ""
            self.reliable_push.enqueue(
                "node_finished",
                {
                    "run_id": run_id,
                    "node_id": getattr(event, "node_id", ""),
                    "node_name": getattr(event, "node_name", ""),
                    "status": evt_status,
                },
            )
            self._schedule_push_flush(self.reliable_push)
        self._broadcast(
            run_id,
            {
                "event": getattr(event, "event_type", "run_update"),
                "node_id": getattr(event, "node_id", ""),
                "at": getattr(event, "timestamp", time.time()),
                "run": self._public(record),
            },
        )
        # issue #191: event-as-checkpoint — persist progress + control state after every step event
        self._persist(run_id)

    def _persist(self, run_id: str) -> None:
        # issue #191: active runs land in SQLite; failures only WARN and never affect execution (persistence is a side channel)
        if self.run_store is None:
            return
        record = self._runs.get(run_id)
        if record is None or record.get("status") not in ("running", "paused"):
            return
        paused_reason = str(record.get("paused_reason") or "")
        if record.get("status") == "running":
            try:
                if self._engine.hitl_pending(run_id):
                    paused_reason = "hitl"
            except Exception:
                pass
        try:
            self.run_store.save(record, paused_reason=paused_reason)
        except Exception as e:
            logger.warning(f"run state persist failed (ignored) {run_id}: {e}")

    @staticmethod
    def _step_dict(s: Any) -> Dict[str, Any]:
        return {
            "node_id": s.node_id,
            "node_name": s.node_name,
            "node_display_name": s.node_display_name,
            "status": s.status.value if hasattr(s.status, "value") else str(s.status),
            "execution_time": s.execution_time,
            "summary": s.summary,
            "error": s.error,
            "output_data": s.output_data,
            "input_data": s.input_data,
            # issue #143: per-node token usage (None = node made no LLM call)
            "tokens": getattr(s, "tokens", None),
            # issue #182: retry observability — final attempt index + per-attempt errors
            "attempt": getattr(s, "attempt", 1),
            "attempt_errors": getattr(s, "attempt_errors", []),
            # issue #227: user-correction marker step (landing point of mid-run directive injection)
            "user_correction": bool(getattr(s, "user_correction", False)),
            # issue #460: session-level checkpoint — step-level session attribution (None on old snapshots/engines)
            "session_id": getattr(s, "session_id", None),
            # issue #554: LLM JSON repair-ladder trigger count for this step (0 = no repair)
            "json_repairs": int(getattr(s, "json_repairs", 0) or 0),
        }

    def list(self, space_id: str = "") -> List[Dict[str, Any]]:
        self._gc()
        # issue #223: space_id filter — the home page "recent tasks (this directory)" scopes by workspace
        items = sorted(self._runs.values(), key=lambda x: -x["created_at"])
        if space_id:
            items = [r for r in items if str(r.get("space_id") or "") == space_id]
        out = [
            {
                "run_id": r["run_id"],
                "workflow_id": r["workflow_id"],
                "workflow_name": r["workflow_name"],
                "status": r["status"],
                "created_at": r["created_at"],
                "finished_at": r.get("finished_at"),
                "error": r.get("error"),
                "review": r.get("review"),
                # issue #207: source/labels surface with the list (default None, zero change for old callers)
                "source": r.get("source"),
                "labels": r.get("labels"),
                # issue #223: owning workspace (default "" = unbound directory, zero change for old data)
                "space_id": str(r.get("space_id") or ""),
                # issue #461: tokens/spend surface with the list (for /api/usage aggregation, default None, zero change for old callers)
                "tokens": r.get("tokens"),
                "spend_usd": r.get("spend_usd"),
            }
            for r in items
        ]
        # issue #399: runs after restart/eviction only have a terminal summary row — the list backfills
        # persisted rows (in-memory records win, same run_id skipped); capped at 100 rows, no full-table scan
        if self.run_store is not None and hasattr(self.run_store, "load_summaries"):
            try:
                rows = self.run_store.load_summaries(limit=100)
            except Exception as e:
                logger.warning(f"terminal summary list read failed (ignored) {e}")
                rows = []
            seen = set(self._runs.keys())
            extra: List[Dict[str, Any]] = []
            for row in rows:
                rid = str(row.get("run_id", ""))
                if not rid or rid in seen:
                    continue
                row_space = str(row.get("space_id") or "")
                if space_id and row_space != space_id:
                    continue
                extra.append(
                    {
                        "run_id": rid,
                        "workflow_id": str(row.get("workflow_id", "")),
                        "workflow_name": str(row.get("workflow_name", "")),
                        "status": str(row.get("status", "")),
                        "created_at": float(row.get("created_at") or 0.0),
                        "finished_at": row.get("finished_at") or None,
                        "error": row.get("error"),
                        "review": None,
                        "source": str(row.get("source", "")),
                        "labels": row.get("labels") or {},
                        "space_id": row_space,
                        "summary_row": True,
                    }
                )
            if extra:
                out = sorted(out + extra, key=lambda x: -x["created_at"])
        return out

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self._runs.get(run_id)
        if run is not None:
            return run
        # issue #388: in-memory registry lacks this run (restart/eviction) → terminal summary row fallback;
        # shape aligned with _public (no workflow_def/directives or other recovery fields)
        if self.run_store is None or not hasattr(self.run_store, "load_summary"):
            return None
        try:
            row = self.run_store.load_summary(run_id)
        except Exception as e:
            logger.warning(f"terminal summary read failed {run_id}: {e}")
            return None
        if row is None:
            return None
        return {
            "run_id": row.get("run_id", run_id),
            "workflow_id": row.get("workflow_id", ""),
            "workflow_name": row.get("workflow_name", ""),
            "status": row.get("status", ""),
            "paused_reason": "",
            "created_at": row.get("created_at") or 0.0,
            "started_at": row.get("created_at") or 0.0,
            "finished_at": row.get("finished_at") or 0.0,
            "steps": row.get("steps") or [],
            "result": None,
            "error": row.get("error"),
            "review": None,
            "artifacts": [],
            "memory": False,
            "space_id": "",
            "source": row.get("source", ""),
            "labels": row.get("labels") or {},
            "summary_row": True,
        }

    def start(
        self,
        workflow_def: Dict[str, Any],
        workflow_id: str = "",
        workflow_name: str = "",
        memory: bool = True,
        message_input: Optional[Dict[str, Any]] = None,
        source: str = "",
        labels: Optional[Dict[str, str]] = None,
        space_id: str = "",
        resume_steps: Optional[List[Dict[str, Any]]] = None,
        ancestor_summary: Optional[List[Dict[str, Any]]] = None,
        hitl_pause: Optional[bool] = None,
        identity_token: str = "",
        concurrency_key: str = "",
        concurrency: int = 0,
        workflow_version: int = 0,
        workflow_digest: str = "",
    ) -> Dict[str, Any]:
        from ..engine import Workflow

        wf = Workflow.from_dict(workflow_def)
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        # issue #260: per-run HITL override must land before create_task — otherwise the first node is already past the gate check
        if hitl_pause is not None:
            setter = getattr(self._engine, "set_run_hitl_pause", None)
            if callable(setter):
                setter(run_id, bool(hitl_pause))
        # issue #594: admission-time reservation — the slot is taken here,
        # synchronously, so a same-key run can never be admitted as running
        # while an earlier same-key run is pending its _execute
        gate_key = str(concurrency_key or "default")
        gate_reserved = bool(self._gate.enabled and self._gate.try_reserve(gate_key))
        record: Dict[str, Any] = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name or wf.name or workflow_id,
            # issue #461: start as queued when the gate is saturated; _execute flips to running once a slot is acquired
            "status": "queued" if (self._gate.enabled and not gate_reserved) else "running",
            # issue #594: reservation flag — _execute skips re-acquire when held
            "gate_reserved": gate_reserved,
            "gate_key": gate_key,
            "created_at": time.time(),
            "started_at": time.time(),
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
            "review": None,
            "workflow_def": workflow_def,
            "artifacts": [],
            # issue #157: per-run memory switch (set at task start), default on
            "memory": bool(memory),
            # issue #191: pause reason (manual|hitl|crash), used by resume semantics
            "paused_reason": "",
            # issue #207: source/labels (caller attribution), default empty
            "source": str(source or ""),
            "labels": dict(labels or {}),
            # issue #223: run's owning workspace (Space) — home page filters/aggregates by directory
            "space_id": str(space_id or ""),
            # issue #249: ancestor run rolling summary (resume chain) — summary rows enter the run record, whole-chain step bodies stay out of context
            "ancestor_summary": list(ancestor_summary or []),
            # issue #461: cross-run concurrency gate key + same-run wave parallelism (0 = engine default)
            "concurrency_key": str(concurrency_key or ""),
            "concurrency": int(concurrency or 0),
            # issue #516: run↔asset version binding — every run is traceable to the asset version it executed
            "workflow_version": int(workflow_version or 0),
            "workflow_digest": str(workflow_digest or ""),
        }
        self._runs[run_id] = record
        self._last_event[run_id] = time.time()
        self._ensure_stall_sweeper()
        self._persist(run_id)
        task = asyncio.create_task(
            self._execute(
                run_id, wf, message_input=message_input, resume_steps=resume_steps, identity_token=identity_token
            )
        )
        self._tasks[run_id] = task
        logger.info(
            f"workflow run started: {run_id} ({record['workflow_name']}) memory={record['memory']} "
            f"source={record['source'] or '-'} labels={len(record['labels'])} status={record['status']}"
        )
        return {"run_id": run_id, "status": record["status"]}

    async def _execute(
        self,
        run_id: str,
        wf: Any,
        start_paused: bool = False,
        resume_steps: Optional[List[Dict[str, Any]]] = None,
        message_input: Optional[Dict[str, Any]] = None,
        identity_token: str = "",
    ) -> None:
        record = self._runs[run_id]
        # issue #288: background run carries the caller's identity JWT — set the
        # contextvar for this task so every LLM call below meters under the
        # caller's `sub` (task context dies with the task, no reset needed).
        from ..auth.identity_propagation import set_current_identity_token

        set_current_identity_token(identity_token)
        # issue #461: cross-run concurrency gate — mark the run record queued while waiting; flip to running once a slot is acquired
        gate_key = str(record.get("concurrency_key") or record.get("gate_key") or "default")
        gate_acquired = False
        if self._gate.enabled:
            if record.get("gate_reserved"):
                # issue #594: slot already reserved at admission — no re-acquire
                gate_acquired = True
            else:
                record["status"] = "queued"
                record["queued_at"] = time.time()
                self._persist(run_id)
                logger.info(f"[{run_id}] run queued waiting for concurrency gate key={gate_key}")
                await self._gate.acquire(gate_key)
                gate_acquired = True
            record["status"] = "running"
            record["started_at"] = time.time()
            self._persist(run_id)
            logger.info(f"[{run_id}] run acquired concurrency gate key={gate_key}")
        try:
            # issue #157: run-level memory integration — startup recall injected into the first node / end-of-run trajectory write-back.
            # Bridge unavailable (no key configured) or switch off → behavior identical to the old version; bridge failure has zero impact on the run.
            bridge = None
            if record.get("memory", True):
                from ..memory_bridge import MemoryBridge

                bridge = MemoryBridge.from_env()
            initial_input: Optional[Dict[str, Any]] = None
            # issue #190: mail-dispatched message body becomes the first node input (memory recall yields to it)
            if message_input:
                initial_input = dict(message_input)
                logger.info(f"[{run_id}] message input injected into first node ({len(initial_input)} fields)")
            # issue #191: resumed runs do not recall again — already injected in the first round; re-injecting would overwrite the snapshot output of the completed first node
            if bridge is not None and not resume_steps:
                query = f"{wf.name} {getattr(wf, 'description', '') or ''}".strip()
                ctx = await bridge.retrieve_context(query)
                if ctx:
                    record["memory_context"] = ctx
                    base = dict(initial_input or {})
                    base["memory_context"] = ctx
                    initial_input = base
                    logger.info(f"[{run_id}] memory context injected into first node input ({len(ctx)} chars)")
            # issue #191: only pass the new kwargs in resume scenarios — injected old/test engines without these kwargs stay unbroken
            engine_kwargs: Dict[str, Any] = {}
            if resume_steps is not None:
                engine_kwargs["resume_steps"] = resume_steps
            if start_paused:
                engine_kwargs["start_paused"] = True
            # issue #228: pass the run workspace to the engine — capability gate judges against the workspace capability pool
            setter = getattr(self._engine, "set_run_workspace", None)
            if callable(setter):
                setter(run_id, str(record.get("space_id") or ""))
            # issue #461: per-run wave parallelism — old engines without this kwarg stay unbroken
            wave = int(record.get("concurrency") or 0)
            if wave > 0:
                engine_kwargs["concurrency"] = wave
            execution = await self._engine.execute(
                wf,
                initial_input=initial_input,
                execution_id=run_id,
                **engine_kwargs,
            )
            record["steps"] = [RunRegistry._step_dict(s) for s in execution.steps]
            record["artifacts"] = _collect_artifacts(execution.steps)
            # issue #461: terminal token/spend attribution
            record["tokens"] = _token_rollup(record["steps"])
            record["spend_usd"] = _spend_usd(record["tokens"]["total_tokens"])
            status = execution.status.value if hasattr(execution.status, "value") else str(execution.status)
            record["status"] = status
            record["result"] = {
                "summary": execution.result_summary,
            }
            # issue #571: run summary MAST attribution (failure attribution routing; default None)
            _attr = getattr(execution, "attribution", None)
            if _attr:
                record["result"]["attribution"] = _attr
                record["attribution"] = _attr
            if execution.error:
                record["error"] = execution.error
            # issue #157: write back on successful run end (failed/cancelled runs are not stored — only valid output settles)
            if bridge is not None and status == "success":
                from ..memory_bridge import MemoryBridge

                interaction = MemoryBridge.build_interaction(
                    session_id=run_id,
                    workflow_id=record["workflow_id"],
                    workflow_name=record["workflow_name"],
                    description=getattr(wf, "description", "") or "",
                    steps=execution.steps,
                    result_summary=execution.result_summary or "",
                    execution_id=run_id,
                )
                ids = await bridge.write_back(interaction)
                record["memory_writeback"] = "ok" if ids is not None else "failed"
                if ids is not None:
                    record["memory_ids"] = ids
        except asyncio.CancelledError:
            logger.info(f"run task cancelled: {run_id}")
            record["status"] = "cancelled"
        except Exception as e:
            logger.error(f"workflow run failed {run_id}: {e}")
            record["status"] = "failed"
            record["error"] = str(e)
        finally:
            record["finished_at"] = time.time()
            # issue #461: release the concurrency gate slot (cancel while queued never acquired the gate, nothing to release)
            if gate_acquired:
                try:
                    self._gate.release(gate_key)
                except Exception as e:
                    logger.warning(f"concurrency gate release failed (ignored) {run_id}: {e}")
            # issue #191: terminal state leaves the store — a finished run no longer needs resume, persistence keeps only active runs
            # issue #388: terminal state first writes a summary row (source/labels/per-step durations), the active row is deleted as usual —
            # GET /api/runs/{id} still works after daemon restart; downstream reconciles by run duration
            if self.run_store is not None:
                try:
                    if hasattr(self.run_store, "save_summary"):
                        self.run_store.save_summary(record)
                except Exception as e:
                    logger.warning(f"terminal summary write failed (ignored) {run_id}: {e}")
                try:
                    self.run_store.delete(run_id)
                except Exception as e:
                    logger.warning(f"run state cleanup failed (ignored) {run_id}: {e}")
            # issue #228: capability usage recording (call count/last used/audit), only installed capabilities are recorded
            if self.capability_recorder is not None:
                try:
                    self.capability_recorder(record)
                except Exception as e:
                    logger.warning(f"capability usage recording failed (ignored) {run_id}: {e}")
            # issue #516: successful runs are recorded as golden regression cases (per-node output summary)
            if self.asset_recorder is not None and record.get("status") == "success" and record.get("workflow_id"):
                try:
                    self.asset_recorder(record)
                except Exception as e:
                    logger.warning(f"golden run recording failed (ignored) {run_id}: {e}")
            # W9: successfully completed runs enter the delivery review queue; failed/cancelled do not
            if record["status"] == "success" and record.get("review") is None:
                record["review"] = {"status": "pending_review", "note": "", "reviewed_at": None}
                logger.info(f"run awaiting delivery review: {run_id}")
            self._broadcast(run_id, {"event": "run_end", "at": time.time(), "run": self._public(record)})
            self._tasks.pop(run_id, None)
            self._last_event.pop(run_id, None)
            # issue #154: run terminal webhook egress point (not injected = zero behavior change)
            if self.reliable_push is not None:
                # issue #579: at-least-once — registration is persisted before delivery; flush failures are left for retry
                reliable = self.reliable_push
                record_ref = record
                reliable.enqueue(
                    "run_finished",
                    {
                        "run_id": run_id,
                        "workflow_name": record_ref.get("workflow_name", ""),
                        "status": record_ref.get("status", ""),
                        "error": record_ref.get("error"),
                        "source": record_ref.get("source", ""),
                        "labels": dict(record_ref.get("labels") or {}),
                    },
                )
                self._schedule_push_flush(reliable)
            elif self.push is not None:
                push = self.push
                record_ref = record
                asyncio.create_task(
                    push.push_event(
                        "run_finished",
                        {
                            "run_id": run_id,
                            "workflow_name": record_ref.get("workflow_name", ""),
                            "status": record_ref.get("status", ""),
                            "error": record_ref.get("error"),
                            # issue #207: attribution fields ride with the event so webhook receivers can route by source
                            "source": record_ref.get("source", ""),
                            "labels": dict(record_ref.get("labels") or {}),
                        },
                    )
                )

    def review(self, run_id: str, approved: bool, note: str = "") -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        """W9: delivery review — pending_review → approved/rejected (with note)."""
        record = self._runs.get(run_id)
        if not record:
            return "not_found", None
        review = record.get("review")
        if not review or review.get("status") != "pending_review":
            return "not_pending", None
        review["status"] = "approved" if approved else "rejected"
        review["note"] = note
        review["reviewed_at"] = time.time()
        record["review"] = review
        self._broadcast(run_id, {"event": "run_reviewed", "at": time.time(), "run": self._public(record)})
        logger.info(f"run delivery review: {run_id} status={review['status']} note_len={len(note)}")
        result: Dict[str, Any] = {"run_id": run_id, "review": review}
        if not approved:
            # B1: rejection attaches the workflow definition so the client can edit and iterate directly
            result["workflow"] = record.get("workflow_def")
        return None, result

    def cancel(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if not record or record["status"] not in ("running", "paused"):
            return False
        self._engine.cancel(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        logger.info(f"workflow run cancelled: {run_id}")
        return True

    def pause(self, run_id: str) -> bool:
        # W8: pause — the engine suspends at the next node checkpoint, record status paused (cancel is also allowed from paused)
        record = self._runs.get(run_id)
        if not record or record["status"] != "running":
            return False
        if not self._engine.pause(run_id):
            return False
        record["status"] = "paused"
        record["paused_reason"] = "manual"
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_paused", "at": time.time(), "run": self._public(record)})
        logger.info(f"workflow run paused: {run_id}")
        return True

    def resume(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if not record or record["status"] != "paused":
            return False
        if not self._engine.resume(run_id):
            # issue #372: engine has no pause event = the execution task is dead (task lost after crash / events cleaned up) —
            # must not fake running again (zombie run, nodes never execute); genuinely restart execution from the snapshot
            return self._relaunch(run_id, record)
        record["status"] = "running"
        record["paused_reason"] = ""
        self._last_event[run_id] = time.time()
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_resumed", "at": time.time(), "run": self._public(record)})
        logger.info(f"workflow run resumed: {run_id}")
        return True

    def _relaunch(self, run_id: str, record: Dict[str, Any]) -> bool:
        # issue #372: the real revival path for resume-after-crash — completed steps continue from snapshot,
        # remaining nodes (including dispatched nodes executing at crash time) re-execute from scratch; revival failure reports loud and stays paused
        try:
            wf_def = record.get("workflow_def") or {}
            if not wf_def:
                raise ValueError("workflow_def 缺失, 无法重启执行")  # i18n: user-facing
            from ..engine import Workflow

            wf = Workflow.from_dict(wf_def)
            if not wf.nodes:
                raise ValueError("workflow_def 无有效节点")  # i18n: user-facing
            snapshot = [
                dict(s)
                for s in (record.get("steps") or [])
                if isinstance(s, dict) and str(s.get("status", "")).lower() in ("success", "completed")
            ]
            old = self._tasks.get(run_id)
            if old and not old.done():
                old.cancel()
            record["status"] = "running"
            record["paused_reason"] = ""
            self._last_event[run_id] = time.time()
            task = asyncio.create_task(self._execute(run_id, wf, resume_steps=snapshot))
            self._tasks[run_id] = task
            self._persist(run_id)
            self._broadcast(run_id, {"event": "run_resumed", "at": time.time(), "run": self._public(record)})
            logger.warning(
                f"[{run_id}] engine has no pause event (execution task dead), restarted execution from snapshot ({len(snapshot)} steps resumed, remaining nodes re-executed)"
            )
            return True
        except Exception as e:
            logger.error(f"[{run_id}] resume restart failed, run stays paused: {e}")
            record["status"] = "paused"
            record["paused_reason"] = "crash"
            record["error"] = f"resume 重启执行失败: {e}"  # i18n: user-facing
            self._persist(run_id)
            self._broadcast(run_id, {"event": "run_resume_failed", "at": time.time(), "run": self._public(record)})
            return False

    def _ensure_stall_sweeper(self) -> None:
        # issue #372: stall watchdog — a running run with no events for a long time (including #257 node heartbeats)
        # is treated as a dead execution task / stuck event loop; crash-pause with evidence; disabled when no emitter or engine lacks heartbeat
        if self._sweeper_started or self._emitter is None:
            return
        if float(getattr(self._engine, "_node_heartbeat_s", 0) or 0) <= 0:
            return
        self._sweeper_started = True
        self._sweeper_task = asyncio.create_task(self._stall_sweeper())

    async def _stall_sweeper(self) -> None:
        threshold = float(os.environ.get("FUSION_COWORK_RUN_STALL_SECONDS") or 300)
        interval = min(max(threshold / 6.0, 5.0), 60.0)
        while True:
            await asyncio.sleep(interval)
            self._stall_pass(time.time())

    def _stall_pass(self, now: float) -> int:
        threshold = float(os.environ.get("FUSION_COWORK_RUN_STALL_SECONDS") or 300)
        tripped = 0
        for run_id, record in list(self._runs.items()):
            if record.get("status") != "running":
                continue
            last = self._last_event.get(run_id) or float(record.get("created_at") or now)
            if now - last <= threshold:
                continue
            try:
                if self._engine.hitl_pending(run_id):
                    continue
            except Exception:
                pass
            self._stall_trip(run_id, record, now - last)
            tripped += 1
        return tripped

    def _stall_trip(self, run_id: str, record: Dict[str, Any], silent_s: float) -> None:
        # issue #372: zombie run verdict — node claims to be running but its execution task has had no output/heartbeat past the threshold;
        # same path as daemon crash recovery: paused_reason=crash + evidence log, task cancelled instead of faking running
        steps = record.get("steps") or []
        last_step = steps[-1] if steps and isinstance(steps[-1], dict) else {}
        logger.warning(
            f"[{run_id}] stall watchdog: running with no events for {silent_s:.0f}s (> threshold), judged execution task dead, "
            f"crash-pause; timeline end: {last_step.get('node_id') or '?'} ({last_step.get('status') or '?'})"
        )
        record["status"] = "paused"
        record["paused_reason"] = "crash"
        record["error"] = (
            f"停滞看门狗: {silent_s:.0f}s 无事件 (节点 {last_step.get('node_id') or '?'})"  # i18n: user-facing
        )
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        self._last_event.pop(run_id, None)
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_paused", "at": time.time(), "run": self._public(record)})

    # issue #227: inject = mid-run directive injection (merged into the target node's input at the next node boundary; the timeline lands a correction marker)
    # issue #465: remove_node/retarget_edge — dynamic changes at wave boundaries (structural validation lives on the engine side)
    DIRECTIVE_ACTIONS = (
        "set_params",
        "insert_node",
        "skip_remaining",
        "inject",
        "override_output",
        "remove_node",
        "retarget_edge",
    )

    def resume_from(self, run_id: str, node_id: str) -> Dict[str, Any]:
        """issue #226: re-run from a given step of a historical run — the new run chains back to the original, completed steps continue from snapshot."""
        record = self._runs.get(run_id)
        if not record:
            raise KeyError(run_id)
        if record["status"] in ("running", "pending", "paused"):
            raise ValueError("not_terminal")
        wf_def = record.get("workflow_def") or {}
        if not wf_def:
            raise ValueError("no_workflow_def")
        raw_nodes = wf_def.get("nodes", [])
        # issue #410: template-sourced runs have workflow_def.nodes as a dict (node_id -> node);
        # the list shape is a canvas-saved run — both accepted, from_dict downstream is already dual-shape compatible
        if isinstance(raw_nodes, dict):
            node_ids = set(raw_nodes.keys())
        else:
            node_ids = {n.get("id") for n in raw_nodes if isinstance(n, dict)}
        if node_id not in node_ids:
            raise ValueError("unknown_node")
        # Position of the target step in the timeline — steps completed before it (success + skipped duplicates of breakpoint resumes) form the resume snapshot;
        # failed/rejected steps are excluded → downstream genuinely re-runs from the target node (re-validation handled by Workflow.from_dict)
        steps = record.get("steps") or []
        idx = next(
            (i for i, s in enumerate(steps) if s.get("node_id") == node_id and not s.get("user_correction")), None
        )
        # issue #249: budgeted snapshot — the last N steps stay full, earlier steps shrink to summary + step:// references;
        # ancestor summaries roll forward (context grows sub-linearly as chain depth increases)
        from ..engine.run_context import build_resume_snapshot, compose_ancestor_summary

        prior_ancestor = list(record.get("ancestor_summary") or [])
        if idx is None:
            # target node never executed → full re-run, empty snapshot, ancestor summary continues as-is
            resume_steps: List[Dict[str, Any]] = []
            ancestor_new: List[Dict[str, Any]] = []
            stats: Dict[str, int] = {"eligible": 0, "snapshot_steps": 0, "estimated_tokens": 0}
        else:
            resume_steps, ancestor_new, stats = build_resume_snapshot(run_id, steps, idx)
        ancestor_summary, truncated = compose_ancestor_summary(prior_ancestor, ancestor_new)
        stats["ancestor_total"] = len(ancestor_summary)
        if truncated:
            stats["ancestor_truncated"] = truncated
        labels = dict(record.get("labels") or {})
        labels["resume_of"] = run_id
        result = self.start(
            wf_def,
            workflow_id=record.get("workflow_id", ""),
            workflow_name=record.get("workflow_name", ""),
            memory=bool(record.get("memory", True)),
            source="resume",
            labels=labels,
            space_id=record.get("space_id", ""),
            resume_steps=resume_steps,
            ancestor_summary=ancestor_summary,
        )
        new_id = result["run_id"]
        logger.info(
            f"resume-from: {run_id} → {new_id} node={node_id} snapshot={len(resume_steps)} steps "
            f"(chained back to original run) context={stats.get('estimated_tokens', 0)}tok "
            f"ancestor={stats.get('ancestor_total', 0)}"
        )
        return {
            "run_id": new_id,
            "resumed_from": node_id,
            "resume_of": run_id,
            "snapshot_steps": len(resume_steps),
            "context": stats,
        }

    # issue #489: rewind re-entry key — idempotent return when (run_id, node_id) match and the produced run is still active
    _ACTIVE_RUN_STATUSES = ("running", "pending", "paused")

    def rewind(self, run_id: str, node_id: str, rule: str = "", actor: str = "") -> Dict[str, Any]:
        """issue #489: gate-denial rewind — re-run downstream from the rewind point; steps already consumed before it replay from snapshot (no double-paying model work).

        Difference from resume_from: idempotency key (run_id, node_id) — repeated calls with the
        same key return the same run while the produced run is active; each rewind records history
        (who/when/which rule) into the original run's rewinds field.
        """
        record = self._runs.get(run_id)
        if not record:
            raise KeyError(run_id)
        for rw in reversed(record.get("rewinds") or []):
            if rw.get("node_id") != node_id:
                continue
            prev = self._runs.get(str(rw.get("new_run_id") or ""))
            if prev and prev.get("status") in self._ACTIVE_RUN_STATUSES:
                logger.info(f"rewind idempotent hit: {run_id} node={node_id} → already active run {prev.get('run_id')}")
                return {
                    "run_id": prev.get("run_id"),
                    "resumed_from": node_id,
                    "rewind_of": run_id,
                    "idempotent": True,
                    "snapshot_steps": len(prev.get("steps") or []),
                    "context": {},
                }
        result = self.resume_from(run_id, node_id)
        new_id = str(result.get("run_id") or "")
        entry = {
            "at": time.time(),
            "node_id": node_id,
            "rule": str(rule or ""),
            "actor": str(actor or ""),
            "new_run_id": new_id,
        }
        record.setdefault("rewinds", []).append(entry)
        # Rewind metadata rides the chain: the new run's labels carry the rewind rule/actor (audit-traceable)
        new_record = self._runs.get(new_id)
        if new_record is not None:
            labels = new_record.setdefault("labels", {})
            labels["rewind_of"] = run_id
            if rule:
                labels["rewind_rule"] = str(rule)
            if actor:
                labels["rewind_actor"] = str(actor)
        self._persist(run_id)
        self._broadcast(
            run_id, {"event": "run_rewound", "at": time.time(), "rewind": entry, "run": self._public(record)}
        )
        logger.info(
            f"rewind: {run_id} → {new_id} node={node_id} rule={rule!r} actor={actor!r} "
            f"snapshot={result.get('snapshot_steps', 0)} steps"
        )
        result["rewind_of"] = run_id
        result["idempotent"] = False
        return result

    def directive(self, run_id: str, directive: Dict[str, Any]) -> Optional[str]:
        """W8/B1: queue a run redirection directive; returns None when queued, otherwise an error code."""
        record = self._runs.get(run_id)
        if not record or record["status"] not in ("running", "paused"):
            return "not_running"
        action = str((directive or {}).get("action", ""))
        if action not in self.DIRECTIVE_ACTIONS:
            return "bad_action"
        if not self._engine.add_directive(run_id, directive):
            # issue #465: structured rejection — take the structured error code from engine-side validation failure and pass it through to the route
            structured = getattr(self._engine, "pop_directive_error", None)
            err = structured(run_id) if callable(structured) else None
            return err or "rejected"
        # issue #191: the full payload lands in the record — replay on recovery needs the whole directive (action alone is not enough)
        record.setdefault("directives", []).append({"action": action, "at": time.time(), "payload": dict(directive)})
        self._persist(run_id)
        self._broadcast(run_id, {"event": "run_directive", "at": time.time(), "run": self._public(record)})
        logger.info(f"run redirection directive queued: {run_id} action={action}")
        return None

    async def recover_persisted(self) -> int:
        # issue #191: daemon restart recovery — SQLite active rows → rebuild record → continue with start_paused.
        # The single recovery path: step progress (resume_steps) and control state (paused/directives) are restored through the same route.
        if self.run_store is None:
            return 0
        try:
            rows = self.run_store.load_active()
        except Exception as e:
            logger.error(f"failed to load persisted run state (skipping recovery): {e}")
            return 0
        recovered = 0
        discarded: list = []
        from ..engine.outbox import get_outbox_bridge

        for row in rows:
            run_id = str(row.get("run_id", ""))
            if not run_id or run_id in self._runs:
                continue
            try:
                wf_def = json.loads(row.get("workflow_def") or "{}")
                steps = json.loads(row.get("steps") or "[]")
                directives = json.loads(row.get("directives") or "[]")
                from ..engine import Workflow

                wf = Workflow.from_dict(wf_def)
                if not wf.nodes:
                    raise ValueError("workflow_def 无有效节点")  # i18n: user-facing
            except Exception as e:
                logger.error(f"failed to recover run {run_id}: persisted data unparseable, row discarded: {e}")
                self.run_store.delete(run_id)
                discarded.append(run_id)
                continue
            # Persisted status running = previous session exited uncleanly (kill -9/crash) — recover loud as paused
            crashed = str(row.get("status", "")) != "paused"
            record: Dict[str, Any] = {
                "run_id": run_id,
                "workflow_id": row.get("workflow_id", ""),
                "workflow_name": row.get("workflow_name", ""),
                "status": "paused",
                "paused_reason": "crash" if crashed else str(row.get("paused_reason") or "manual"),
                "created_at": float(row.get("created_at") or time.time()),
                "finished_at": None,
                "steps": steps,
                "result": None,
                "error": None,
                "review": None,
                "workflow_def": wf_def,
                "artifacts": [],
                "memory": bool(row.get("memory", 1)),
                "recovered": True,
                "directives": [],
                # issue #223: recovery keeps workspace ownership (v1 rows lack the column → empty string, meaning unbound)
                "space_id": str(row.get("space_id") or ""),
            }
            self._runs[run_id] = record
            # issue #463: durable HITL — journal pending rows are rebuilt into the approval queue (recovered runs only)
            journal = getattr(self._engine, "_approval_journal", None)
            pm = getattr(self._engine, "_permission_manager", None)
            if journal is not None and pm is not None:
                try:
                    n_restored = pm.restore_pending_details(journal.pending_for_run(run_id))
                    if n_restored:
                        logger.info(f"[{run_id}] approval journal pending rebuilt: {n_restored} entries")
                except Exception as e:
                    logger.warning(f"[{run_id}] approval journal pending rebuild failed (ignored): {e}")
            task = asyncio.create_task(self._execute(run_id, wf, start_paused=True, resume_steps=steps))
            self._tasks[run_id] = task
            # Wait for the engine to register the pause event before replaying directives (add_directive validates _pause_events exists)
            for _ in range(200):
                if run_id in getattr(self._engine, "_pause_events", {}):
                    break
                await asyncio.sleep(0.02)
            # Replay persisted redirection directives during recovery — effective at the first node boundary after resume
            replayed = 0
            for d in directives:
                if not isinstance(d, dict):
                    continue
                payload = d.get("payload") or d
                if self._engine.add_directive(run_id, payload):
                    record.setdefault("directives", []).append(d)
                    replayed += 1
            if replayed:
                logger.info(f"[{run_id}] replayed {replayed} redirection directives during recovery")
            if crashed:
                # issue #372: crash recovery must provide diagnosable evidence — the node executing at crash time
                # (non-terminal steps in the snapshot) + timeline length; ops never needs to compare transcripts across processes
                running_steps = [
                    f"{s.get('node_id')}({s.get('node_name')})"
                    for s in steps
                    if isinstance(s, dict)
                    and str(s.get("status", "")) not in ("success", "completed", "failed", "skipped")
                ]
                last_step = steps[-1] if steps and isinstance(steps[-1], dict) else {}
                suspect = (
                    ", ".join(running_steps)
                    if running_steps
                    else f"none (timeline end: {last_step.get('node_id') or '?'})"
                )
                logger.warning(
                    f"[{run_id}] previous session exited uncleanly (persisted status running), recovered as paused "
                    f"({len(steps)} step snapshot), waiting for manual resume/approval; node suspected executing at crash: {suspect}"
                )
            else:
                logger.info(f"[{run_id}] recovered paused run: paused_reason={record['paused_reason']}")
            self._persist(run_id)
            recovered += 1
        # issue #382: boot reconcile — expired leases re-queue; effects of discarded runs retire honestly
        _ob = get_outbox_bridge()
        if _ob is not None and _ob.enabled:
            _ob.reconcile(discarded or None)
        # issue #388: terminal summary retention pruning (default 90 days, FUSION_COWORK_RUN_SUMMARY_RETENTION_DAYS overrides)
        if self.run_store is not None and hasattr(self.run_store, "prune_summaries"):
            try:
                retention = int(os.environ.get("FUSION_COWORK_RUN_SUMMARY_RETENTION_DAYS", "90"))
            except ValueError:
                retention = 90
            try:
                self.run_store.prune_summaries(retention)
            except Exception as e:
                logger.warning(f"terminal summary retention prune failed (ignored): {e}")
        return recovered

    def _gc(self) -> None:
        now = time.time()
        for rid in [
            r for r, v in self._runs.items() if v.get("finished_at") and now - v["finished_at"] > _RUN_TTL_SECONDS
        ]:
            self._runs.pop(rid, None)


def _build_space_stack(data_dir: Optional[Path] = None):
    """Assemble the space domain (store + 4 services + FastAPI sub-app) — shared by web serve and tests.

    Returns (space_app, artifact_svc, space_store) — the store lets serve close() on shutdown.
    """
    from ..ai.mlx_client import FusionMLXClient, KBClient
    from ..space import (
        NotificationService,
        SpaceChatService,
        SpaceKBService,
        SpaceMemberService,
        SpaceService,
        SpaceStore,
    )
    from ..space.api import create_space_api
    from ..space.artifact import SpaceArtifactService
    from ..space.permission import SpacePermission

    store = SpaceStore(data_dir=str(data_dir) if data_dir else _DEFAULT_SPACE_DATA_DIR)
    perm = SpacePermission(store)
    mlx = FusionMLXClient()
    # W13/B2: chat service and the SSE endpoint share the same event bus (previously chat built its own emitter,
    # so /stream's _event_emitter was None and SSE only ever emitted ping)
    from ..engine.events import EventEmitter

    emitter = EventEmitter()
    chat_svc = SpaceChatService(store, mlx, perm, event_emitter=emitter)
    kb_svc = SpaceKBService(store, KBClient(), perm)
    artifact_svc = SpaceArtifactService(store, perm)
    notification_svc = NotificationService(store)
    space_svc = SpaceService(store)
    member_svc = SpaceMemberService(store, perm)
    app = create_space_api(
        space_svc,
        member_svc,
        chat_svc,
        kb_svc,
        artifact_svc=artifact_svc,
        notification_svc=notification_svc,
        event_emitter=emitter,
        space_store=store,
    )
    return app, artifact_svc, store


def _serialize_session(s: Any) -> Dict[str, Any]:
    return {
        "id": s.id,
        "workflow_id": s.workflow_id,
        "workflow_name": s.workflow_name,
        "status": s.status,
        "initial_input": s.initial_input,
        "execution_id": s.execution_id,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "completed_at": s.completed_at,
        "metadata": s.metadata,
    }


def _make_pending_push_callback(push_egress, runs: Any = None):
    """issue #154: approval enqueue → webhook push callback factory (best-effort, never raises).

    Called inside the event loop (engine permission-check await chain), so create_task directly;
    skipped when no event loop — the webhook is best-effort, never spin up a loop for it.
    issue #207: when a RunRegistry is injected, attach run attribution by execution_id (run_source/run_labels).
    """

    def _cb(info: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("push callback has no event loop, skipped (best-effort)")
            return
        payload = dict(info)
        if runs is not None:
            try:
                record = runs.get(str(info.get("execution_id") or ""))
                if record:
                    payload["run_source"] = record.get("source", "")
                    payload["run_labels"] = dict(record.get("labels") or {})
            except Exception as e:
                logger.debug(f"approval event run attribution failed (ignored): {e}")
        loop.create_task(push_egress.push_event("approval_requested", payload))

    return _cb


def _otlp_logs(records: List[Dict[str, Any]], scope: str = "fusion-cowork.audit") -> Dict[str, Any]:
    """issue #156: audit records → OTLP/JSON LogsData (consumable directly by a SIEM collector)."""
    log_records: List[Dict[str, Any]] = []
    for rec in records:
        attrs = [{"key": k, "value": {"stringValue": str(v)}} for k, v in (rec.get("attributes") or {}).items()]
        log_records.append(
            {
                "timeUnixNano": str(int(float(rec.get("time_unix_nano") or 0))),
                "severityText": str(rec.get("severity") or "INFO"),
                "body": {"stringValue": str(rec.get("body") or "")},
                "attributes": attrs,
            }
        )
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "fusion-cowork"}},
                    ]
                },
                "scopeLogs": [{"scope": {"name": scope}, "logRecords": log_records}],
            }
        ]
    }


async def _takeover_session(ws, run_id: str, runs: Any, permission_manager: Any) -> None:
    """issue #154: takeover session main loop — subscribe to run events + handle steering frames.

    Frames: {"method": "directive"|"pause"|"resume"|"cancel"|"confirm", "params": {...}}
    """
    queue = runs.subscribe(run_id)
    record = runs.get(run_id)
    snapshot = RunRegistry._public(record) if record is not None else {}
    await ws.send_json({"event": "snapshot", "run": snapshot})
    pump = asyncio.create_task(_pump_run_events(ws, queue))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"event": "error", "message": "invalid json"})
                continue
            method = str(frame.get("method", ""))
            params = frame.get("params") or {}
            reply = await _takeover_apply(method, params, run_id, runs, permission_manager)
            await ws.send_json(reply)
    finally:
        runs.unsubscribe(run_id, queue)
        pump.cancel()


def _schedule_hitl_audit(app: Any, run_id: str, action_id: str, approved: bool, decided_by: str) -> None:
    """issue #463: HITL decisions enter the tamper-evident audit chain (security/audit.py) — fire-and-forget, failures only WARN."""
    audit = getattr(app.state, "audit_log", None)
    if audit is None:
        return

    async def _run():
        try:
            from ..tenant import get_current_tenant

            tenant = get_current_tenant()
            tenant_id = str(tenant) if tenant else "default"
            await audit.log(
                tenant_id=tenant_id,
                actor=decided_by,
                action="hitl.resolve",
                resource=f"run/{run_id}/approval/{action_id}",
                detail={"approved": approved, "decided_by": decided_by},
            )
        except Exception as e:
            logger.warning(f"HITL audit log write failed run={run_id}: {e}")

    try:
        # strong ref: fire-and-forget tasks are weakly held by the loop and can be GC'd mid-run
        _PENDING_AUDIT_TASKS.add(asyncio.get_running_loop().create_task(_run()))
    except RuntimeError:
        logger.debug("HITL audit log skipped: no running loop")


async def _takeover_apply(method: str, params: dict, run_id: str, runs: Any, permission_manager: Any) -> dict:
    """issue #154: steering frame dispatch — reuses RunRegistry + permission manager, returns a reply frame."""
    if method == "directive":
        action = str(params.get("action", ""))
        if action not in RunRegistry.DIRECTIVE_ACTIONS:
            return {"event": "error", "message": f"directive rejected: unknown action {action}"}
        err = runs.directive(run_id, params)
        if err:
            return {"event": "error", "message": f"directive rejected: {err}"}
        return {"event": "directive_queued", "action": str(params.get("action", ""))}
    if method == "pause":
        ok = runs.pause(run_id)
        return {"event": "paused"} if ok else {"event": "error", "message": "not running"}
    if method == "resume":
        ok = runs.resume(run_id)
        return {"event": "resumed"} if ok else {"event": "error", "message": "not paused"}
    if method == "cancel":
        ok = runs.cancel(run_id)
        return {"event": "cancelled"} if ok else {"event": "error", "message": "not cancelable"}
    if method == "confirm":
        if permission_manager is None:
            return {"event": "error", "message": "权限管理器不可用"}  # i18n: user-facing
        action_id = str(params.get("action_id", ""))
        approved = bool(params.get("approved"))
        confirmed = await permission_manager.confirm_guard(
            action_id, approved, approved_by="takeover", scope=str(params.get("scope") or "*")
        )
        if confirmed:
            try:
                saver = getattr(permission_manager, "save", None)
                if callable(saver):
                    saver()
            except Exception as e:
                logger.warning(f"takeover confirm rule save failed: {e}")
        return {"event": "confirm_result", "action_id": action_id, "approved": approved, "confirmed": confirmed}
    return {"event": "error", "message": f"unknown method: {method}"}


async def _pump_run_events(ws, queue) -> None:
    """issue #154: run event pump — RunRegistry subscription queue → WS frames. Exits on disconnect/cancel."""
    while True:
        item = await queue.get()
        try:
            await ws.send_json(item)
        except Exception:
            return


def _seed_hash(workflow: Dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(workflow, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _seed_template_workflows(store: WorkflowDefStore, templates_dir: Optional[Path] = None) -> List[str]:
    # issue #485: register templates as workflow definitions so external callers
    # (e.g. fusion-site factory) can kick off runs by workflow_id right after a
    # fresh deploy — no manual install step. Templates with required params that
    # lack defaults are seeded raw (residue kept) so the per-run params path on
    # /api/runs can assemble them with caller-provided values.
    from ..templates import TemplateManager

    try:
        tm = TemplateManager(custom_dir=str(templates_dir)) if templates_dir else TemplateManager()
    except Exception as e:
        logger.warning(f"issue #485 template seeding skipped (TemplateManager unavailable): {e}")
        return []
    seeded: List[str] = []
    for tpl in tm.list_templates():
        tid = str(tpl.get("id") or "")
        wf_data = tpl.get("workflow") or {}
        if not tid or not isinstance(wf_data, dict) or not (wf_data.get("nodes") or []):
            continue
        try:
            wf = tm.template_to_workflow(tid)
            assembled = wf.to_dict() if wf is not None else wf_data
            if "${param." in json.dumps(assembled, ensure_ascii=False):
                assembled = wf_data
        except Exception as e:
            logger.warning(f"issue #485 template assembly failed for {tid}: {e}")
            assembled = wf_data
        # digest over the RAW template workflow (deterministic — assembled dicts
        # embed created_at/updated_at timestamps, so hashing those never converges)
        digest = _seed_hash(wf_data)
        for rid in [tid] + [a for a in tpl.get("web_aliases", []) if isinstance(a, str) and a]:
            existing = store.get(rid)
            if existing and (
                existing.get("template_id") != tid
                or existing.get("source") != "template_seed"
                or existing.get("_seed_hash") == digest
            ):
                # never clobber operator-created or install-source definitions;
                # refresh only our own seeded records when the template changed
                continue
            record = {
                "name": tpl.get("name", tid),
                "description": tpl.get("description", ""),
                "workflow": assembled,
                "template_id": tid,
                "source": "template_seed",
                "_seed_hash": digest,
            }
            if rid != tid:
                record["alias_of"] = tid
            try:
                store.save(rid, record)
                seeded.append(rid)
            except ValueError as e:
                logger.warning(f"issue #485 seed skipped for {rid}: {e}")
    if seeded:
        logger.info(f"issue #485 template seeds registered: {len(seeded)} workflow definition(s)")
    return seeded


def create_web_app(
    engine: Any = None,
    dist_dir: Optional[Path] = None,
    store_root: Optional[Path] = None,
    scheduler: Any = None,
    nl_generator: Any = None,
    session_store: Any = None,
    space_api: Any = None,
    space_data_dir: Optional[Path] = None,
    permission_manager: Any = None,
    cross_sync: Any = None,
    skills_dir: Optional[Path] = None,
    rag_base_url: Optional[str] = None,
    rag_wired_path: Optional[Path] = None,
    run_store: Any = None,
    team_db: Optional[str] = None,
    team_roster: Any = None,
    team_mailbox: Any = None,
    runner_pool: Any = None,
    capabilities_store: Any = None,
    capabilities_path: Optional[Path] = None,
    connector_dir: Optional[Path] = None,
    studio_base_url: Optional[str] = None,
    studio_api_key: Optional[str] = None,
    capabilities_transport: Any = None,
    templates_dir: Optional[Path] = None,
    demand_db_path: Optional[str] = None,
    demand_catalog_path: Optional[str] = None,
    delivery_db_path: Optional[str] = None,
):
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket
        from fastapi.responses import JSONResponse, Response, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise ImportError("web API 需要 [web] 依赖: pip install fusion-cowork[web]") from e  # i18n: user-facing

    from ..nodes import import_all_nodes

    import_all_nodes()
    # ai-hr-automation: load trusted plugins so plugin nodes (hr_http) join the
    # serve process registry. deny-closed: only plugins.trusted whitelist passes
    # the loader's CR-21 check; load failures are logged, not fatal.
    try:
        from ..plugins import get_plugin_loader

        for _name, _plugin_nodes in get_plugin_loader().load_all().items():
            logger.info(f"plugin nodes loaded: {_name} -> {[n.name for n in _plugin_nodes]}")
    except Exception as e:
        logger.warning(f"plugin load failed (ignored): {e}")
    store = WorkflowDefStore(root=store_root)
    # issue #516: workflow asset discipline — versioned saves + machine-checkable
    # acceptance gates + audit trail + golden regression suite per workflow id
    from .workflow_assets import WorkflowAssetStore, canonical_digest

    asset_store = WorkflowAssetStore(root=Path(store_root) / "assets" if store_root else None)
    # issue #485: seed templates as workflow definitions so fresh deploys can kick
    # off runs by workflow_id (e.g. product_lifecycle_agents) immediately
    try:
        _seed_template_workflows(store, templates_dir)
    except Exception as e:
        logger.warning(f"issue #485 template seeding failed (ignored): {e}")
    # issue #459: inbound trigger registry — token-authenticated webhooks create new runs, persisted alongside workflow definitions
    from ..engine.triggers import TriggerRegistry

    triggers = TriggerRegistry(Path(store_root) if store_root else None)
    if permission_manager is None:
        from ..engine import PermissionManager

        permission_manager = PermissionManager()
        # issue #138: approval rule persistence — serve startup loads previously approved/denied rules
        loader = getattr(permission_manager, "load", None)
        if callable(loader):
            try:
                loader()
            except Exception as e:
                logger.warning(f"web API permission rule load failed (continuing with empty rules): {e}")
    if engine is None:
        from ..engine import WorkflowEngine
        from ..engine.events import EventEmitter

        emitter = EventEmitter()
        # issue #545: ops telemetry — opt-in via FUSION_OPS_DSN (unset = None = zero behavior change)
        from ..observability.ops_telemetry import OpsTelemetry

        engine = WorkflowEngine(
            permission_manager=permission_manager,
            event_emitter=emitter,
            ops_telemetry=OpsTelemetry.from_env(),
        )
    else:
        permission_manager = getattr(engine, "_permission_manager", None) or permission_manager
        emitter = getattr(engine, "_event_emitter", None)
    # issue #545: injected engines may carry their own telemetry (test seam);
    # otherwise build from env here (mirrors the #463 approval-journal injection)
    if getattr(engine, "_ops_telemetry", None) is None:
        from ..observability.ops_telemetry import OpsTelemetry

        engine._ops_telemetry = OpsTelemetry.from_env()
    # issue #463: durable HITL — inject the approval journal into the engine (never overrides an engine-provided one; tests can inject a temp store)
    approval_journal = getattr(engine, "_approval_journal", None)
    if approval_journal is None:
        from ..engine.approval_journal import ApprovalJournal

        approval_journal = ApprovalJournal(Path(store_root) / "approval_journal.db" if store_root else None)
        engine._approval_journal = approval_journal
        logger.info("issue #463 approval journal attached (durable HITL)")
    # issue #404: HITL email notification — enabled when SMTP_HOST is set;
    # fire-and-forget, failure logs a warning only. Never overrides an injected notifier.
    if getattr(engine, "hitl_notifier", None) is None:
        from .email_notify import HitlEmailNotifier

        notifier = HitlEmailNotifier()
        if notifier.enabled:
            engine.hitl_notifier = notifier
            logger.info("issue #404 HITL email notifier enabled (smtp host=%s)", (notifier._config or {}).get("host"))
    # issue #191: run control-state persistence layer — SQLite-backed, recoverable across daemon restarts
    from ..engine.run_persistence import RunStateStore

    if run_store is None:
        run_store = RunStateStore(Path(store_root) / "run_states.db" if store_root else None)
    runs = RunRegistry(engine, event_emitter=emitter, run_store=run_store)
    if scheduler is None:
        from ..engine.scheduler import TaskScheduler

        scheduler = TaskScheduler()
    if nl_generator is None:
        from ..ai.mlx_client import FusionMLXClient
        from ..ai.nl_parser import NLWorkflowGenerator

        nl_generator = NLWorkflowGenerator(FusionMLXClient())
        logger.info("web API NL generator not injected, using default FusionMLXClient")
    if session_store is None:
        from ..engine.session import SessionStore

        session_store = SessionStore()
    if space_api is None:
        space_api, artifact_svc, space_store = _build_space_stack(space_data_dir)
        logger.info("web API space stack built (default paths)")
    else:
        artifact_svc, space_store = None, None
    app = FastAPI(title="Fusion-Cowork Web API", version="0.1.0")
    # issue #545: telemetry surfaced on app.state for startup/shutdown lifecycle
    app.state.ops_telemetry = getattr(engine, "_ops_telemetry", None)
    app.state.asset_store = asset_store
    app.state.scheduler = scheduler
    app.state.nl_generator = nl_generator
    app.state.session_store = session_store
    app.state.space_api = space_api
    app.state.artifact_svc = artifact_svc
    app.state.space_store = space_store
    # issue #463: HITL decision audit chain — reuses the space store's audit_log table (no space stack = no audit)
    if space_store is not None:
        from ..security.audit import AuditLog

        app.state.audit_log = AuditLog(space_store)
    app.state.permission_manager = permission_manager
    app.state.run_registry = runs
    # W22: cross-device sync engine seam — /api/deliver can deliver once injected; default None (503, honestly unavailable)
    app.state.cross_sync = cross_sync

    # issue #288: identity JWT capture — the shell forwards the caller's
    # fusion-identity JWT to modules (sdk.auth.identityToken()); capture it per
    # request so in-request LLM calls (space chat, NL delegate) carry it as
    # Authorization: Bearer for gateway quota metering. Background runs capture
    # separately in RunRegistry.start/_execute.
    @app.middleware("http")
    async def _capture_identity_token(request: Request, call_next):
        from ..auth.identity_propagation import extract_identity_token, identity_token_context

        token = extract_identity_token({k.lower(): v for k, v in request.headers.items()})
        with identity_token_context(token):
            return await call_next(request)

    # ── issue #228: capability marketplace — workspace capability pool + authorization gate + usage recording ──
    from ..capabilities import OAuthFlow, build_catalog, make_capability_gate, record_run_usage
    from ..capabilities.store import CapabilityStore

    capability_store = capabilities_store or CapabilityStore(capabilities_path)
    app.state.capability_store = capability_store
    _gate_kwargs = {
        "connector_dir": connector_dir,
        "rag_wired_path": rag_wired_path,
    }
    if getattr(permission_manager, "capability_gate", None) is None:
        permission_manager.capability_gate = make_capability_gate(capability_store, **_gate_kwargs)

    def _capability_recorder(record: Dict[str, Any]) -> None:
        record_run_usage(capability_store, record, **_gate_kwargs)

    runs.capability_recorder = _capability_recorder

    def _asset_recorder(record: Dict[str, Any]) -> None:
        # issue #516: successful run becomes a golden regression case
        # (per-node output digests) for the workflow it executed
        workflow_id = str(record.get("workflow_id") or "")
        steps = record.get("steps") or []
        digests = {
            str(s.get("node_id")): canonical_digest(s.get("output_data") or {})
            for s in steps
            if isinstance(s, dict) and s.get("node_id")
        }
        asset_store.record_golden_case(workflow_id, record.get("message_input") or {}, digests)

    runs.asset_recorder = _asset_recorder

    def _asset_actor(request: Request) -> str:
        # issue #516: audit actor = masked caller key (never persist the raw key)
        try:
            key = extract_caller_key(request.headers)
        except Exception:
            key = ""
        return (key[:6] + "…") if key else "anonymous"

    def _make_replay_fn(engine_factory: Any = None):
        # issue #516: regression replay runs the candidate definition in an
        # isolated thread + fresh event loop (route handlers sit in a running
        # loop); per-node output digests are compared against golden cases
        def replay(workflow_dict: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, str]:
            from ..engine import PermissionLevel, PermissionManager, Workflow, WorkflowEngine

            outcome: Dict[str, Any] = {}

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    pm = PermissionManager(level=getattr(PermissionLevel, "AUTO", PermissionLevel.CONFIRM))
                    eng = WorkflowEngine(permission_manager=pm, hitl_pause=False)
                    wf = Workflow.from_dict(workflow_dict)
                    exec_id = f"replay_{uuid.uuid4().hex[:8]}"
                    execution = loop.run_until_complete(eng.execute(wf, execution_id=exec_id))
                    outcome["digests"] = {
                        str(s.node_id): canonical_digest(s.output_data or {}) for s in execution.steps if s.node_id
                    }
                    outcome["status"] = execution.status.value
                except Exception as e:
                    outcome["error"] = str(e)
                finally:
                    loop.close()

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            t.join(timeout=60)
            if t.is_alive():
                raise RuntimeError("regression replay timed out after 60s")
            if "error" in outcome:
                raise RuntimeError(f"regression replay failed: {outcome['error']}")
            if outcome.get("status") != "success":
                raise RuntimeError(f"regression replay did not succeed: {outcome.get('status')}")
            return outcome["digests"]

        return replay

    oauth_flow = OAuthFlow(transport=capabilities_transport)

    # issue #190: team message consumption stack — roster + durable mailbox + resident consumer pool
    from ..orchestrator.runner_pool import RunnerPool
    from ..orchestrator.team_mailbox import CoworkRoster, TeamMailbox

    if team_roster is None:
        team_roster = CoworkRoster(team_db)
    if team_mailbox is None:
        team_mailbox = TeamMailbox(team_db)

    async def _team_dispatch(agent: Dict[str, Any], message: Dict[str, Any]) -> str:
        workflow_id = str(agent.get("workflow_id") or "")
        record = store.get(workflow_id)
        if not record:
            raise RuntimeError(f"工作流定义不存在: {workflow_id}")  # i18n: user-facing
        body = message.get("body") or ""
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = {"input": body}
        if not isinstance(payload, dict):
            payload = {"input": body}
        payload.setdefault("input", body)
        payload["sender"] = message.get("sender", "")
        payload["message_id"] = message.get("message_id", "")
        payload["team"] = message.get("team", "")
        started = runs.start(
            record.get("workflow") or {},
            workflow_id=workflow_id,
            workflow_name=record.get("name", workflow_id),
            memory=False,
            message_input=payload,
        )
        logger.info(f"team dispatch: agent={agent.get('agent_id')} workflow={workflow_id} run={started['run_id']}")
        return started["run_id"]

    async def _team_get_run(run_id: str) -> Optional[Dict[str, Any]]:
        record = runs.get(run_id)
        if not record:
            return None
        return {
            "status": record.get("status"),
            "last_result": (record.get("result") or {}).get("summary"),
            "last_error": record.get("error"),
        }

    if runner_pool is None:
        runner_pool = RunnerPool(
            roster=team_roster,
            mailbox=team_mailbox,
            dispatch=_team_dispatch,
            get_run=_team_get_run,
            poll_interval=_team_poll_interval(),
        )
    app.state.team_roster = team_roster
    app.state.team_mailbox = team_mailbox
    app.state.runner_pool = runner_pool

    # issue #154: webhook push egress (FUSION_COWORK_PUSH_WEBHOOK_URL unset = no-op)
    from .push import PushEgress, ReliableEgress

    push_egress = PushEgress.from_env()
    app.state.push_egress = push_egress
    runs.push = push_egress
    # issue #579: at-least-once completion notification — when FUSION_COWORK_PUSH_AT_LEAST_ONCE=1
    # run_finished/node_end go through the persistent outbox (off = original best-effort path, zero behavior change)
    reliable_push = ReliableEgress.from_env()
    app.state.reliable_push = reliable_push
    if reliable_push is not None:
        runs.reliable_push = reliable_push
    # issue #154: approval enqueue → webhook push (permission manager callback, best-effort)
    if permission_manager is not None:
        permission_manager.on_pending_created = _make_pending_push_callback(push_egress, runs)

    # issue #191: recover at startup — active runs in SQLite (paused / crash-left running) are rebuilt as paused, waiting for manual resume/approval
    @app.on_event("startup")
    async def _recover_runs():
        recovered = await runs.recover_persisted()
        if recovered:
            logger.warning(
                f"startup recovery: rebuilt {recovered} active runs from the persistence layer (all start paused, waiting for manual resume/approval)"
            )
        # issue #579: flush leftover outbox at startup — re-deliver last session's undelivered completion notifications (may duplicate; receivers dedupe by id)
        if reliable_push is not None and reliable_push.pending_count():
            try:
                delivered = await reliable_push.flush_pending()
                logger.info(f"push outbox startup flush: delivered={delivered} pending={reliable_push.pending_count()}")
            except Exception as e:
                logger.warning(f"push outbox startup flush failed (left for event trigger): {e}")

    @app.on_event("startup")
    async def _start_team_pool():
        # issue #190: start the consumer pool when the roster has enabled agents; FUSION_COWORK_RUNNER_POOL=1 forces resident
        try:
            enabled = len(app.state.team_roster.list(enabled_only=True))
        except Exception as e:
            logger.error(f"team consumer pool failed to start (roster unreadable): {e}")
            return
        forced = os.environ.get("FUSION_COWORK_RUNNER_POOL", "").strip().lower() in ("1", "true", "yes", "on")
        if enabled or forced:
            await app.state.runner_pool.start()
            logger.info(
                f"team consumer pool started: {enabled} consumers, polling every {app.state.runner_pool.poll_interval}s"
            )
        else:
            logger.info(
                "team consumer pool not started: roster has no enabled agents (register via fusion-cowork team register)"
            )

    @app.on_event("shutdown")
    async def _stop_team_pool():
        await app.state.runner_pool.stop()

    # issue #545: ops telemetry lifecycle — start the batched OpsEmitter worker on
    # serve, drain + flush on shutdown; absent (no FUSION_OPS_DSN) = no-op
    @app.on_event("startup")
    async def _start_ops_telemetry():
        telemetry = getattr(app.state, "ops_telemetry", None)
        if telemetry is not None:
            await telemetry.start()

    @app.on_event("shutdown")
    async def _stop_ops_telemetry():
        telemetry = getattr(app.state, "ops_telemetry", None)
        if telemetry is not None:
            await telemetry.stop()

    # issue #546: support mailbox agent — P0 observe-only, opt-in via
    # FUSION_MAILBOX_ENABLED=1 + IMAP env (fusion-sv T2 injection); unset = no-op
    @app.on_event("startup")
    async def _start_mailbox_agent():
        from ..mailbox.service import MailboxAgent

        agent = MailboxAgent.from_env()
        app.state.mailbox_agent = agent
        if agent is not None:
            await agent.start()

    @app.on_event("shutdown")
    async def _stop_mailbox_agent():
        agent = getattr(app.state, "mailbox_agent", None)
        if agent is not None:
            await agent.stop()

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        if request.url.path in ("/health",) or request.url.path.startswith("/ws/"):
            return await call_next(request)
        # issue #262: webhook callbacks authenticate via HMAC signature (compare_digest inside the route), never the static key
        if request.url.path.startswith("/api/hooks/"):
            return await call_next(request)
        # #146: no key configured = allow (aligns with the cli.py/README "unset = allow" contract) —
        # the old unconditional 401 caused a shell login bounce loop.
        if not auth_configured():
            return await call_next(request)
        key = extract_caller_key(request.headers)
        key_info = check_static_key(key)
        if not key_info:
            logger.warning(f"web API auth denied: {request.url.path} (key {'missing' if not key else 'invalid'})")
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "missing or invalid api key"}},
            )
        # issue #565: service credential scope — the storefront bff key may only
        # drive the run lifecycle endpoints (create/poll/retry) plus the invite
        # token surface (#609, site /invite activation); everything else
        # (workflows CRUD, spaces, approvals, admin) stays user-key territory
        _svc_ok = request.url.path.startswith("/api/runs") or request.url.path.startswith("/api/delivery/invites")
        if key_info.get("key_id") == "service" and not _svc_ok:
            logger.warning(f"web API service key out of scope: {request.url.path}")
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "forbidden", "message": "service key is scoped to /api/runs endpoints"}},
            )
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "module": "fusion-cowork",
            "version": _version(),
            "shell_registration": _shell_registration_state(),
        }

    @app.get("/api/nodes")
    async def nodes_list():
        from ..engine.node import NodeRegistry

        nodes = NodeRegistry.list()
        return {"nodes": nodes, "count": len(nodes)}

    @app.get("/api/workflows")
    async def workflows_list():
        items = store.list()
        return {"workflows": items, "count": len(items)}

    @app.post("/api/workflows", status_code=201)
    async def workflows_create(request: Request, body: Dict[str, Any]):
        workflow_id = body.get("id") or f"wf_{uuid.uuid4().hex[:10]}"
        # issue #516: every save is a versioned asset — gates evaluate before
        # activation; a rejected candidate never becomes the current def
        try:
            asset_record = asset_store.save(workflow_id, body, actor=_asset_actor(request), replay_fn=_make_replay_fn())
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
        if asset_record.get("status") == "rejected":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "asset_gate_rejected",
                    "message": "工作流资产门禁未通过, 未激活",  # i18n: user-facing
                    "version": asset_record.get("version"),
                    "gate_results": asset_record.get("gate_results", []),
                },
            )
        try:
            record = store.save(workflow_id, body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
        record["workflow_version"] = asset_record.get("version")
        record["workflow_digest"] = asset_record.get("digest")
        return record

    @app.get("/api/workflows/{workflow_id}")
    async def workflows_get(workflow_id: str):
        record = store.get(workflow_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"},  # i18n: user-facing
            )
        return record

    @app.put("/api/workflows/{workflow_id}")
    async def workflows_put(request: Request, workflow_id: str, body: Dict[str, Any]):
        if not store.get(workflow_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"},  # i18n: user-facing
            )
        # issue #516: update also flows through the asset gate
        try:
            asset_record = asset_store.save(workflow_id, body, actor=_asset_actor(request), replay_fn=_make_replay_fn())
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
        if asset_record.get("status") == "rejected":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "asset_gate_rejected",
                    "message": "工作流资产门禁未通过, 未激活",  # i18n: user-facing
                    "version": asset_record.get("version"),
                    "gate_results": asset_record.get("gate_results", []),
                },
            )
        record = store.save(workflow_id, body)
        record["workflow_version"] = asset_record.get("version")
        record["workflow_digest"] = asset_record.get("digest")
        return record

    @app.delete("/api/workflows/{workflow_id}")
    async def workflows_delete(workflow_id: str):
        if not store.delete(workflow_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"},  # i18n: user-facing
            )
        return {"deleted": True}

    # ── issue #516: asset version / audit surface (queryable independently of execution logs) ──

    @app.get("/api/workflows/{workflow_id}/versions")
    async def workflow_versions(workflow_id: str):
        versions = asset_store.list_versions(workflow_id)
        return {"versions": versions, "count": len(versions)}

    @app.get("/api/workflows/{workflow_id}/versions/diff")
    async def workflow_version_diff(workflow_id: str, v1: int, v2: int):
        result = asset_store.diff(workflow_id, v1, v2)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"版本不存在: v{v1} 或 v{v2}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"v1": v1, "v2": v2, "diff": result}

    @app.get("/api/workflows/{workflow_id}/versions/{version}")
    async def workflow_version_detail(workflow_id: str, version: int):
        rec = asset_store.get_version(workflow_id, version)
        if rec is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"版本不存在: v{version}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return rec

    @app.get("/api/workflows/{workflow_id}/audit")
    async def workflow_asset_audit(workflow_id: str, limit: int = 100):
        entries = asset_store.audit(workflow_id, limit=limit)
        return {"audit": entries, "count": len(entries)}

    # ── issue #459: inbound triggers — webhooks/events can create new runs (start of the event-driven chain) ──

    @app.get("/api/triggers")
    async def triggers_list():
        items = triggers.list()
        return {"triggers": items, "count": len(items)}

    @app.post("/api/triggers", status_code=201)
    async def triggers_create(body: Dict[str, Any]):
        workflow_id = str(body.get("workflow_id") or "").strip()
        record = store.get(workflow_id)
        if not record:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"workflow_id 不存在: {workflow_id}"},  # i18n: user-facing
            )
        try:
            created = triggers.create(
                body.get("name"),
                workflow_id,
                params=body.get("params") if isinstance(body.get("params"), dict) else None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
        return {**created, "token_masked": True}

    @app.delete("/api/triggers/{trigger_id}")
    async def triggers_delete(trigger_id: str):
        if not triggers.delete(trigger_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"触发器不存在: {trigger_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"deleted": True}

    @app.post("/api/hooks/trigger/{token}", status_code=202)
    async def trigger_hook(token: str, request: Request):
        # The token itself is the auth (/api/hooks/ prefix bypasses the static key); compare_digest prevents timing attacks
        trigger = triggers.find_by_token(token)
        if not trigger:
            logger.warning(f"trigger hook token invalid: {token[:12]}…")
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "trigger token 无效"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            payload = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from exc  # i18n: user-facing
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON 对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        workflow_id = str(trigger.get("workflow_id") or "")
        record = store.get(workflow_id)
        if not record:
            raise HTTPException(
                status_code=410,
                detail={"code": "gone", "message": f"触发器绑定的 workflow 已删除: {workflow_id}"},  # i18n: user-facing
            )
        workflow_def = record.get("workflow") or {}
        if not workflow_def:
            raise HTTPException(
                status_code=410,
                detail={"code": "gone", "message": f"workflow 定义为空: {workflow_id}"},  # i18n: user-facing
            )
        params = dict(trigger.get("params") or {})
        params.update({k: v for k, v in payload.items() if k not in ("payload",)})
        try:
            started = runs.start(
                workflow_def,
                workflow_id=workflow_id,
                workflow_name=record.get("name", workflow_id),
                source="trigger",
                labels={"trigger": str(trigger.get("name") or "")},
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"},  # i18n: user-facing
            ) from exc
        run_id = started.get("run_id") if isinstance(started, dict) else ""
        # Trigger events also broadcast onto the event bus — wait_for_event can chain-respond to inbound triggers
        from ..engine.event_bus import get_event_bus

        get_event_bus().emit(f"trigger.{trigger.get('name')}", {"run_id": run_id, **payload})
        logger.info(f"trigger hook started run: trigger={trigger.get('name')} run={run_id}")
        return {"run_id": run_id, "trigger": trigger.get("name"), "started": bool(run_id)}

    @app.post("/api/events/{name}")
    async def events_emit(name: str, request: Request):
        try:
            payload = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from exc  # i18n: user-facing
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON 对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        from ..engine.event_bus import get_event_bus

        woken = get_event_bus().emit(name, payload)
        return {"event": name, "woken": woken}

    @app.post("/api/runs", status_code=202)
    async def runs_create(request: Request, body: Dict[str, Any]):
        workflow_def = body.get("workflow") or {}
        workflow_id = body.get("workflow_id", "")
        workflow_version = 0
        workflow_digest = ""
        if not workflow_def and workflow_id:
            record = store.get(workflow_id)
            if not record:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"},  # i18n: user-facing
                )
            workflow_def = record.get("workflow") or {}
            workflow_name = record.get("name", workflow_id)
            # issue #516: run↔asset version binding — every run records which
            # asset version + digest actually executed
            _asset = asset_store.get_active(workflow_id)
            if _asset:
                workflow_version = int(_asset.get("version") or 0)
                workflow_digest = str(_asset.get("digest") or "")
        else:
            workflow_name = body.get("name", "")
            record = None
        if not workflow_def:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "workflow 或 workflow_id 必填"},  # i18n: user-facing
            )
        try:
            # issue #157: per-run memory switch, default on (bridge auto-disables when FUSION_MEMORY_API_KEY is unset)
            memory = body.get("memory", True)
            if not isinstance(memory, bool):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "memory 须为 boolean"},  # # i18n: user-facing
                )  # i18n: user-facing
            # issue #209: per-run input — same semantics as mail dispatch (message_input injected into the first node),
            # inputs takes precedence, initial_input kept as a compat alias; non-dict always 400, no longer silently dropped
            inputs_obj = body.get("inputs", body.get("initial_input"))
            if inputs_obj is not None and not isinstance(inputs_obj, dict):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "inputs 须为 JSON 对象"},  # # i18n: user-facing
                )  # i18n: user-facing
            # issue #207: per-run source/labels — all optional, type-checked then stored on the run record
            source = body.get("source", "")
            if not isinstance(source, str):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "source 须为字符串"},  # # i18n: user-facing
                )  # i18n: user-facing
            labels = body.get("labels")
            if labels is None:
                labels = {}
            if not isinstance(labels, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "labels 须为字符串到字符串的对象"},  # i18n: user-facing
                )
            # issue #223: run's owning workspace — runs started in the active directory context carry the directory id
            space_id = body.get("space_id", "")
            if not isinstance(space_id, str):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "space_id 须为字符串"},  # # i18n: user-facing
                )  # i18n: user-facing
            # issue #260: per-run HITL gate override — unattended acceptance runs can fail fast
            # (true/false explicit override; default follows the engine-global FUSION_COWORK_HITL_PAUSE)
            hitl_pause = body.get("hitl_pause")
            if hitl_pause is not None and not isinstance(hitl_pause, bool):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "hitl_pause 须为 boolean"},  # i18n: user-facing
                )
            # issue #461: cross-run concurrency gate key + same-run wave parallelism
            concurrency_key = body.get("concurrency_key", "")
            if not isinstance(concurrency_key, str):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "concurrency_key 须为字符串"},  # i18n: user-facing
                )
            concurrency = body.get("concurrency", 0)
            if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 0:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "concurrency 须为非负整数"},  # i18n: user-facing
                )
            # issue #485: per-run template params — template-backed records re-assemble
            # the source template with param_values so ${param.*} resolve before execution
            params_obj = body.get("params")
            if params_obj is not None and not isinstance(params_obj, dict):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "params 须为 JSON 对象"},  # # i18n: user-facing
                )  # i18n: user-facing
            template_id = str((record or {}).get("template_id") or "")
            if params_obj and not template_id:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "bad_request",
                        "message": "params 仅支持模板工作流 (记录缺 template_id)",  # # i18n: user-facing
                    },  # i18n: user-facing
                )
            if params_obj and template_id:
                _wf = _tpl_manager().template_to_workflow(template_id, param_values=params_obj)
                if _wf is None:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # i18n: user-facing
                    )
                _errors = _wf.validate()
                if _errors:
                    raise HTTPException(
                        status_code=400, detail={"code": "invalid_workflow", "message": "; ".join(_errors)}
                    )
                _assembled = _wf.to_dict()
                if "${param." in json.dumps(_assembled, ensure_ascii=False):
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "params_missing",
                            "message": "必填模板参数缺失 (${param.*} 未替换)",  # # i18n: user-facing
                        },  # i18n: user-facing
                    )
                workflow_def = _assembled
            # issue #288: caller identity JWT → background run LLM calls meter
            # under the user's `sub` at the gateway (never persisted to disk).
            from ..auth.identity_propagation import extract_identity_token

            identity_token = extract_identity_token({k.lower(): v for k, v in request.headers.items()})
            return runs.start(
                workflow_def,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                memory=memory,
                message_input=inputs_obj,
                source=source,
                labels=labels,
                space_id=space_id,
                hitl_pause=hitl_pause,
                identity_token=identity_token,
                concurrency_key=concurrency_key,
                concurrency=concurrency,
                workflow_version=workflow_version,
                workflow_digest=workflow_digest,
            )
        except ValueError as exc:
            # issue #135 F-2: bad edge shapes/cycles and other workflow definition errors → 400 with a readable message, not 500
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"},  # i18n: user-facing
            ) from exc

    @app.get("/api/runs")
    async def runs_list(space_id: str = ""):
        # issue #223: ?space_id= workspace filter — default returns all (zero change for old callers)
        items = runs.list(space_id=space_id)
        return {"runs": items, "count": len(items)}

    # ── issue #223: home page directory-selection aggregation — Space cards + member count + task count + recent activity ──
    @app.get("/api/workspaces")
    async def workspaces_list():
        if space_store is None:
            # space stack not assembled (no [web] data dir) — honestly report unavailable; frontend keeps its state visible
            return {"workspaces": [], "available": False}
        try:
            spaces = await space_store.list_spaces(limit=100)
        except Exception as e:
            logger.error(f"workspaces aggregation failed: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_error", "message": f"工作区列表失败: {e}"},  # i18n: user-facing
            ) from e
        run_items = runs.list()
        out: List[Dict[str, Any]] = []
        for sp in spaces:
            d = sp.to_dict() if hasattr(sp, "to_dict") else dict(sp)
            sid = str(d.get("id") or "")
            try:
                member_count = await space_store.count_members(sid)
            except Exception as e:
                logger.warning(f"workspaces member count failed space={sid}: {e}")
                member_count = 0
            ws_runs = [r for r in run_items if str(r.get("space_id") or "") == sid]
            out.append(
                {
                    "id": sid,
                    "name": d.get("name", ""),
                    "description": d.get("description", ""),
                    "owner_id": d.get("owner_id", ""),
                    "status": d.get("status", ""),
                    "created_at": d.get("created_at"),
                    "updated_at": d.get("updated_at"),
                    "member_count": int(member_count),
                    "task_count": len(ws_runs),
                    "last_run_at": max((r.get("created_at") or 0 for r in ws_runs), default=None),
                }
            )
        return {"workspaces": out, "available": True}

    # ── issue #251: system workspace directory enumeration — a new directory name must come from a real directory pick, no free-typed input;
    # ?path= relative browsing supports subdirectory levels (dialog drills down level by level); out-of-bounds paths get 400 ──
    @app.get("/api/workspaces/system-dirs")
    async def workspaces_system_dirs(path: str = ""):
        root = Path(os.environ.get("FUSION_COWORK_WORKSPACE_ROOT") or Path.home()).expanduser()
        if not root.is_dir():
            # Root missing → honestly report unavailable; frontend picker collapses with a hint
            logger.warning(f"system-dirs root directory missing: {root}")
            return {"root": str(root), "root_name": root.name, "relative": "", "directories": [], "available": False}
        rel = (path or "").strip().strip("/")
        current = root
        if rel and rel != ".":
            resolved_root = root.resolve()
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_path", "message": f"路径越界: {path}"},  # i18n: user-facing
                ) from None
            if not candidate.is_dir():
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"目录不存在: {path}"},  # # i18n: user-facing
                )  # i18n: user-facing
            current = candidate
            rel = candidate.relative_to(resolved_root).as_posix()
        try:
            entries = sorted(
                (e for e in current.iterdir() if e.is_dir() and not e.name.startswith(".")),
                key=lambda e: e.name.lower(),
            )
        except OSError as e:
            logger.error(f"system-dirs enumeration failed root={root} rel={rel}: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_error", "message": f"目录枚举失败: {e}"},  # i18n: user-facing
            ) from e
        child_rel = (rel + "/") if rel else ""
        directories = [{"name": e.name, "path": str(e), "relative": child_rel + e.name} for e in entries[:200]]
        return {
            "root": str(root),
            "root_name": root.name,
            "relative": rel,
            "directories": directories,
            "available": True,
        }

    @app.get("/api/runs/{run_id}")
    async def runs_get(run_id: str):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        # issue #461: live token/spend attribution — per-step usage rollup, computed on the fly even for old records without the field
        if record.get("steps"):
            record["tokens"] = _token_rollup(record["steps"])
            record["spend_usd"] = _spend_usd(record["tokens"]["total_tokens"])
        # issue #558 (#132): terminal failed runs carry edge-level handoff candidates — kernel
        # FailureAnalyzer deterministic heuristics (no model call); purely additive field, failure never breaks the API
        if record.get("status") == "failed":
            attribution = _failure_attribution(run_id, record)
            if attribution is not None:
                record["failure_attribution"] = attribution
            else:
                record.pop("failure_attribution", None)
        # issue #181: HITL pending marker — the frontend shows "waiting for human approval" instead of a frozen state
        # issue #187: deadline attached — approval card countdown; state is server-persistent and survives page refresh
        if record.get("status") == "running":
            record["hitl_pending"] = engine.hitl_pending(run_id)
            if not record["hitl_pending"]:
                # issue #563: restart re-delivery surface — engine memory is empty
                # after a crash, but the kernel gate queue may still hold the run's
                # pending question; expose it read-only (marker verified against
                # the checkpoint store when wired)
                recover_getter = getattr(engine, "hitl_recover", None)
                recovered = recover_getter(run_id) if callable(recover_getter) else None
                if recovered is not None:
                    record["hitl_recover"] = recovered
                else:
                    record.pop("hitl_recover", None)
            else:
                record.pop("hitl_recover", None)
            if record["hitl_pending"]:
                deadline_getter = getattr(engine, "hitl_deadline", None)
                deadline = deadline_getter(run_id) if callable(deadline_getter) else None
                if deadline is not None:
                    record["hitl_deadline"] = deadline
                # issue #508: awaiting_human branch state — who is waiting on the human decision
                record["awaiting_human"] = True
                waiting_node = getattr(engine, "hitl_waiting_node", None)
                awaiting_node = waiting_node(run_id) if callable(waiting_node) else None
                if awaiting_node:
                    record["awaiting_node"] = awaiting_node
        else:
            # issue #549: terminal runs clear HITL markers written by mid-run GETs — the record is a
            # registry-shared dict; keys written while running would leak into the terminal response if not cleaned
            record.pop("hitl_pending", None)
            record.pop("hitl_deadline", None)
            record.pop("hitl_recover", None)
            record.pop("awaiting_human", None)
            record.pop("awaiting_node", None)
        return record

    # ── W10/B5 run artifact preview (right column, refreshed live with step outputs) ──
    @app.get("/api/runs/{run_id}/artifacts")
    async def runs_artifacts(run_id: str):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        artifacts = record.get("artifacts") or []
        return {"artifacts": artifacts, "count": len(artifacts), "review": record.get("review")}

    # ── B2 run step-log SSE — events.py bus → frontend real-time steps (polling is the fallback) ──
    @app.get("/api/runs/{run_id}/events")
    async def runs_events(run_id: str, request: Request):
        record = runs.get(run_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing

        async def event_stream():
            queue = runs.subscribe(run_id)
            try:
                snapshot = json.dumps({"run": runs._public(record)}, ensure_ascii=False)
                yield f"event: snapshot\ndata: {snapshot}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=20)
                    except TimeoutError:
                        yield "event: ping\ndata: \n\n"
                        continue
                    yield f"event: {item.get('event', 'run_update')}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                runs.unsubscribe(run_id, queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def runs_cancel(run_id: str):
        if not runs.cancel(run_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不可取消: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"status": "cancelling", "run_id": run_id}

    # ── W8 run redirection: pause/resume (appended directives need the orchestrator interaction protocol, see migration doc I2) ──
    @app.post("/api/runs/{run_id}/pause")
    async def runs_pause(run_id: str):
        if not runs.pause(run_id):
            raise HTTPException(
                status_code=409,
                detail={"code": "not_running", "message": f"运行不可暂停: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"status": "paused", "run_id": run_id}

    @app.post("/api/runs/{run_id}/resume")
    async def runs_resume(run_id: str):
        if not runs.resume(run_id):
            raise HTTPException(
                status_code=409,
                detail={"code": "not_paused", "message": f"运行不可恢复: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"status": "running", "run_id": run_id}

    # ── W9 delivery review: successful runs pending_review → approved/rejected (node-level guard approvals belong to W19 separately) ──
    @app.post("/api/runs/{run_id}/review")
    async def runs_review(run_id: str, body: Dict[str, Any]):
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "approved 必须是布尔值"},  # # i18n: user-facing
            )  # i18n: user-facing
        err, result = runs.review(run_id, approved, str(body.get("note") or ""))
        if err == "not_found":
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        if err == "not_pending":
            raise HTTPException(
                status_code=409,
                detail={"code": "not_pending", "message": f"运行不在待审查状态: {run_id}"},  # i18n: user-facing
            )
        return result

    # ── W8/B1 run redirection: append directives while paused (swap params / insert node / skip remaining) ──
    @app.post("/api/runs/{run_id}/directive", status_code=202)
    async def runs_directive(run_id: str, body: Dict[str, Any]):
        err = runs.directive(run_id, body)
        if err == "bad_action":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "bad_action",
                    "message": f"action 必须是 {', '.join(RunRegistry.DIRECTIVE_ACTIONS)}",  # i18n: user-facing
                },
            )
        if err == "not_running":
            raise HTTPException(
                status_code=409,
                detail={"code": "not_running", "message": f"运行不在运行/暂停态: {run_id}"},  # i18n: user-facing
            )
        if isinstance(err, dict):
            # issue #465: structured rejection (mid_wave/already_executed/unknown_target/bad_directive)
            status = 400 if err.get("code") == "bad_directive" else 409
            raise HTTPException(status_code=status, detail={"code": err.get("code"), "message": err.get("message")})
        if err:
            raise HTTPException(
                status_code=409,
                detail={"code": err, "message": f"运行改向被拒绝: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"status": "queued", "run_id": run_id, "action": body.get("action")}

    # ── issue #226: re-run terminal runs from a given step (new run chains back to the original) ──
    @app.post("/api/runs/{run_id}/resume-from")
    async def runs_resume_from(run_id: str, body: Dict[str, Any]):
        node_id = str(body.get("node_id") or "").strip()
        if not node_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "node_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            return runs.resume_from(run_id, node_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # i18n: user-facing
            ) from None
        except ValueError as exc:
            msg = str(exc)
            if msg == "not_terminal":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "not_terminal", "message": f"运行未到终态: {run_id}"},  # i18n: user-facing
                ) from exc
            if msg == "unknown_node":
                raise HTTPException(
                    status_code=400,
                    detail={"code": "unknown_node", "message": f"步骤不存在于工作流: {node_id}"},  # i18n: user-facing
                ) from exc
            # no_workflow_def / from_dict validation failure → 400
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"无法重跑: {msg}"},  # # i18n: user-facing
            ) from exc  # i18n: user-facing

    # ── issue #489: gate-denial rewind — idempotent rewind + history (who/when/which rule) ──
    @app.post("/api/runs/{run_id}/rewind")
    async def runs_rewind(run_id: str, body: Dict[str, Any]):
        node_id = str(body.get("node_id") or "").strip()
        if not node_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "node_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        rule = str(body.get("rule") or "").strip()
        actor = str(body.get("actor") or "").strip()
        try:
            return runs.rewind(run_id, node_id, rule=rule, actor=actor)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # i18n: user-facing
            ) from None
        except ValueError as exc:
            msg = str(exc)
            if msg == "not_terminal":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "not_terminal", "message": f"运行未到终态: {run_id}"},  # i18n: user-facing
                ) from exc
            if msg == "unknown_node":
                raise HTTPException(
                    status_code=400,
                    detail={"code": "unknown_node", "message": f"步骤不存在于工作流: {node_id}"},  # i18n: user-facing
                ) from exc
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"无法回退: {msg}"},  # # i18n: user-facing
            ) from exc  # i18n: user-facing

    # ── issue #233: artifact card proxy (fusion-artifacts-engine v0.6.0 contract) ──
    @app.get("/api/artifacts/refs/{artifact_id}")
    async def artifacts_ref_card(artifact_id: str):
        # issue #237: distinguish three failure classes — "invalid id / artifact deleted or no permission / engine unreachable";
        # the frontend then gives a clear message instead of silently injecting a fake ref.
        card, upstream_status = await _fetch_artifact_card_with_status(artifact_id)
        if card is None:
            if not artifact_id or not _ARTIFACT_ID_PATTERN.match(artifact_id):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "bad_id", "message": f"非法 artifact id: {artifact_id}"},  # i18n: user-facing
                )
            if upstream_status in (401, 403, 404):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "artifact_not_found",
                        "message": f"产物不存在或无权访问: {artifact_id}",  # # i18n: user-facing
                    },  # i18n: user-facing
                )
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "upstream_unavailable",
                    "message": "fusion-artifacts-engine 不可用",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        card["ref_text"] = _artifact_ref_text(card)
        return {"artifact": card}

    # ── W5 template catalog (migration doc §6 P0) ──
    # issue #261: template publish/import/export — canvas workflows ↔ template assets (custom_dir injectable for isolation)
    def _tpl_manager():
        from ..templates import TemplateManager

        return TemplateManager(custom_dir=str(templates_dir) if templates_dir else "")

    def _tpl_slug(name: str) -> str:
        import uuid as _uuid

        s = re.sub(r"[^a-z0-9_-]+", "-", str(name).strip().lower()).strip("-")
        return s[:48] or f"custom_{_uuid.uuid4().hex[:8]}"

    @app.get("/api/templates")
    async def templates_list(category: str = ""):
        items = _tpl_manager().list_templates(category=category or "")
        return {"templates": items, "count": len(items)}

    @app.get("/api/templates/{template_id}")
    async def templates_get(template_id: str):
        tpl = _tpl_manager().get_template(template_id)
        if not tpl:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return tpl

    @app.post("/api/templates")
    async def templates_publish(request: Request):
        # issue #261: publish a canvas workflow as a named template (custom, idempotent slug — same name overwrites)
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        name = str(body.get("name") or "").strip()
        wf = body.get("workflow")
        if not name or not isinstance(wf, dict) or not (wf.get("nodes") or wf.get("workflow", {}).get("nodes")):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "name 与 workflow.nodes 必填"},  # i18n: user-facing
            )
        wf = wf.get("workflow") if isinstance(wf.get("workflow"), dict) else wf
        tpl_id = _tpl_slug(name)
        tpl = {
            "id": tpl_id,
            "name": name,
            "description": str(body.get("description") or ""),
            "category": _tpl_slug(str(body.get("category") or "custom")),
            "workflow": wf,
        }
        # issue #294: params schema persists with the template — GUI launch form is generated from the schema
        if isinstance(body.get("params"), dict):
            tpl["params"] = body["params"]
        if not _tpl_manager().save_custom_template(tpl):
            raise HTTPException(
                status_code=500,
                detail={"code": "save_failed", "message": "模板保存失败"},  # # i18n: user-facing
            )  # i18n: user-facing
        logger.info(f"template published: {tpl_id} ({name})")
        return {"id": tpl_id, "name": name}

    @app.post("/api/templates/import")
    async def templates_import(request: Request):
        # issue #261: import template JSON (full template or bare workflow definition); same id overwrites
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON 对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        wf = body.get("workflow") if isinstance(body.get("workflow"), dict) else body
        if not isinstance(wf, dict) or not wf.get("nodes"):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "workflow.nodes 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        name = str(body.get("name") or wf.get("name") or "").strip() or "imported-template"
        tpl_id = _tpl_slug(str(body.get("id") or name))
        tpl = {
            "id": tpl_id,
            "name": name,
            "description": str(body.get("description") or ""),
            "category": _tpl_slug(str(body.get("category") or "custom")),
            "workflow": wf,
        }
        # issue #294: params schema persists with the template — GUI launch form is generated from the schema
        if isinstance(body.get("params"), dict):
            tpl["params"] = body["params"]
        if not _tpl_manager().save_custom_template(tpl):
            raise HTTPException(
                status_code=500,
                detail={"code": "save_failed", "message": "模板保存失败"},  # # i18n: user-facing
            )  # i18n: user-facing
        logger.info(f"template imported: {tpl_id} ({name})")
        return {"id": tpl_id, "name": name}

    @app.get("/api/templates/{template_id}/export")
    async def templates_export(template_id: str):
        # issue #261: export the template's raw JSON (Content-Disposition attachment)
        tpl = _tpl_manager().get_template(template_id)
        if not tpl:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        import json as _json

        return Response(
            content=_json.dumps(tpl, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{template_id}.json"'},
        )

    @app.delete("/api/templates/{template_id}")
    async def templates_delete(template_id: str):
        # issue #261: delete a custom template — builtin templates are refused
        if not _tpl_manager().delete_custom_template(template_id):
            raise HTTPException(
                status_code=400,
                detail={"code": "not_custom", "message": f"仅自定义模板可删除: {template_id}"},  # i18n: user-facing
            )
        return {"deleted": template_id}

    @app.post("/api/templates/{template_id}/install", status_code=201)
    async def templates_install(template_id: str, body: Optional[Dict[str, Any]] = None):
        # issue #485: register a template as a workflow definition on demand —
        # builtin templates are auto-seeded at serve startup; this covers custom
        # templates published after startup and explicit operator installs
        tm = _tpl_manager()
        tpl = tm.get_template(template_id)
        if not tpl:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        body = body if isinstance(body, dict) else {}
        param_values = body.get("params") if isinstance(body.get("params"), dict) else {}
        wf = tm.template_to_workflow(template_id, param_values=param_values)
        if wf is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        errors = wf.validate()
        if errors:
            raise HTTPException(status_code=400, detail={"code": "invalid_workflow", "message": "; ".join(errors)})
        wf_dict = wf.to_dict()
        if "${param." in json.dumps(wf_dict, ensure_ascii=False):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "params_missing",
                    "message": "必填模板参数缺失 (${param.*} 未替换)",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        primary = str(body.get("id") or template_id)
        ids = [primary] + [a for a in tpl.get("web_aliases", []) if isinstance(a, str) and a and a != primary]
        try:
            installed = []
            for rid in ids:
                payload = {
                    "name": tpl.get("name", template_id),
                    "description": tpl.get("description", ""),
                    "workflow": wf_dict,
                    "template_id": template_id,
                    "source": "template_install",
                    # raw-template digest: seed refresh skips install-source records anyway
                    "_seed_hash": _seed_hash(tpl.get("workflow") or {}),
                }
                if rid != primary:
                    payload["alias_of"] = primary
                installed.append(store.save(rid, payload))
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
        logger.info(f"issue #485 template installed as workflow definition: {template_id} -> {ids}")
        return {"installed": ids, "records": installed}

    @app.post("/api/templates/{template_id}/launch", status_code=202)
    async def templates_launch(template_id: str, request: Request):
        # issue #294: GUI one-click template launch — params form values are substituted via template_to_workflow and the run starts directly
        tpl = _tpl_manager().get_template(template_id)
        if not tpl:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        param_values = body.get("params") if isinstance(body.get("params"), dict) else {}
        wf = _tpl_manager().template_to_workflow(template_id, param_values=param_values)
        if wf is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        errors = wf.validate()
        if errors:
            raise HTTPException(status_code=400, detail={"code": "invalid_workflow", "message": "; ".join(errors)})
        wf_dict = wf.to_dict()
        # Leftover ${param.*} residue = missing required params — refusing at assembly is more honest than shipping org/repo literals
        blob = json.dumps(wf_dict, ensure_ascii=False)
        if "${param." in blob:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "params_missing",
                    "message": "必填模板参数缺失 (${param.*} 未替换)",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        # issue #288: caller identity JWT → background run LLM calls meter under the user
        from ..auth.identity_propagation import extract_identity_token

        identity_token = extract_identity_token({k.lower(): v for k, v in request.headers.items()})
        try:
            return runs.start(
                wf_dict,
                workflow_name=str(tpl.get("name") or template_id),
                source="template",
                labels={"template": template_id},
                identity_token=identity_token,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"},  # i18n: user-facing
            ) from exc

    # ── issue #400: requirement intake mode — requirement in; slug/private repo/registry registration all derived automatically ──
    def _intake_module():
        from . import intake as _intake

        if _intake.INTAKE_FACTORY is not None:
            return _intake.INTAKE_FACTORY()
        return _intake

    @app.post("/api/templates/{template_id}/launch-intake", status_code=202)
    async def templates_launch_intake(template_id: str, request: Request, response: Response):
        # The client provides exactly one thing: requirement text (md/txt doc, v1). slug/private repo/registry
        # registration/launch params are all derived; explicit-params mode remains /launch (operator expert channel)
        intake = _intake_module()
        tpl = _tpl_manager().get_template(template_id)
        if not tpl:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        requirement = str(body.get("requirement") or "")
        slug_hint = str(body.get("slug_hint") or "")
        dry_run = bool(body.get("dry_run"))
        # issue #413: architecture review (gate B) intake is opt-in — default false = gate passes automatically
        review_arch = bool(body.get("review_arch"))
        try:
            plan = await intake.prepare_intake(
                requirement, slug_hint=slug_hint, dry_run=dry_run, path=intake.registry_path()
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail={"code": "registry_missing", "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(exc)}) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail={"code": "upstream_failure", "message": str(exc)}) from exc
        if dry_run:
            response.status_code = 200  # preview is not an accepted task
            return {"status": "dry_run", "plan": plan}

        # registry entry was created with run_id "pending" — start the run, then backfill
        # issue #414: the template's slug param is a bare slug (repo_scaffold adds the ai- prefix itself) — registry/repo/labels
        # keep the ai- prefixed form, the value passed to the template is stripped, otherwise the scaffold dir becomes ai-ai-<name>
        template_slug = plan["slug"].removeprefix("ai-")
        try:
            wf = _tpl_manager().template_to_workflow(
                template_id,
                param_values={
                    "slug": template_slug,
                    "repo": plan["repo"],
                    "task_title": intake.task_title(requirement),
                    "task_prompt": "Implement the assigned task per PRD/architecture docs; deliver via PR with self-test evidence.",
                    "review_arch": review_arch,
                },
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"模板装配失败: {exc}"},  # i18n: user-facing
            ) from exc
        if wf is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"模板不存在: {template_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        errors = wf.validate()
        if errors:
            raise HTTPException(status_code=400, detail={"code": "invalid_workflow", "message": "; ".join(errors)})
        wf_dict = wf.to_dict()
        if "${param." in json.dumps(wf_dict, ensure_ascii=False):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "params_missing",
                    "message": "必填模板参数缺失 (${param.*} 未替换)",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        from ..auth.identity_propagation import extract_identity_token

        identity_token = extract_identity_token({k.lower(): v for k, v in request.headers.items()})
        try:
            run = runs.start(
                wf_dict,
                workflow_name=str(tpl.get("name") or template_id),
                source="intake",
                labels={"template": template_id, "intake": "1", "slug": plan["slug"]},
                identity_token=identity_token,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"},  # i18n: user-facing
            ) from exc
        run_id = str(run.get("run_id", ""))
        try:
            intake.update_registry_run_id(plan["slug"], run_id, path=intake.registry_path())
        except Exception as e:
            logger.warning(f"registry run_id backfill failed (ignored) {plan['slug']}: {e}")
        return {"status": "started", "run_id": run_id, "plan": plan}

    # ── issue #263 repository onboarding wizard ──
    def _wizard():
        from . import github_wizard as _gw

        if _gw.WIZARD_FACTORY is not None:
            return _gw.WIZARD_FACTORY()
        if not os.environ.get("GITHUB_TOKEN"):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "token_missing",
                    "message": "GITHUB_TOKEN 未设置 — 向导只走环境变量, 不入库",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        return _gw.GithubWizard()

    @app.get("/api/wizard/repos")
    async def wizard_repos(org: str):
        org = (org or "").strip()
        if not org:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "org 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            repos = await _wizard().list_repos(org)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"wizard list repos failed org={org}: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "github_error", "message": f"GitHub 列仓失败: {e}"},  # i18n: user-facing
            ) from e
        return {"repos": repos, "count": len(repos)}

    @app.post("/api/wizard/preview")
    async def wizard_preview(body: Dict[str, Any]):
        org = str(body.get("org") or "").strip()
        repos = [r for r in (body.get("repos") or []) if str(r).strip()]
        if not org or not repos:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "org 与 repos 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            plans = await _wizard().preview(
                org, repos, codeowners=body.get("codeowners"), protected_checks=body.get("protected_checks")
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"wizard preview failed org={org}: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "github_error", "message": f"GitHub 预览失败: {e}"},  # i18n: user-facing
            ) from e
        return {"plans": plans}

    @app.post("/api/wizard/apply")
    async def wizard_apply(body: Dict[str, Any]):
        org = str(body.get("org") or "").strip()
        repos = [r for r in (body.get("repos") or []) if str(r).strip()]
        if not org or not repos:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "org 与 repos 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            results = await _wizard().apply(
                org, repos, codeowners=body.get("codeowners"), protected_checks=body.get("protected_checks")
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"wizard apply failed org={org}: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "github_error", "message": f"GitHub 应用失败: {e}"},  # i18n: user-facing
            ) from e
        return {"results": results}

    @app.post("/api/wizard/teardown")
    async def wizard_teardown(body: Dict[str, Any]):
        org = str(body.get("org") or "").strip()
        repos = [r for r in (body.get("repos") or []) if str(r).strip()]
        if not org or not repos:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "org 与 repos 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            results = await _wizard().teardown(org, repos)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"wizard teardown failed org={org}: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "github_error", "message": f"GitHub 卸载失败: {e}"},  # i18n: user-facing
            ) from e
        return {"results": results}

    # ── W6 scheduled tasks (migration doc §6 P0) ──
    def _make_task_executor(workflow_id: str):
        # issue #218: TaskScheduler._run_task invokes the executor with zero args (await executor());
        # the old _exec(task_id) signature raised TypeError on every fire, and after 5 consecutive failures the task was marked FAILED.
        async def _exec() -> None:
            record = store.get(workflow_id)
            if not record:
                logger.error(f"scheduled task executor cannot find workflow: {workflow_id}")
                return
            runs.start(
                record.get("workflow") or {}, workflow_id=workflow_id, workflow_name=record.get("name", workflow_id)
            )
            logger.info(f"scheduled task triggered workflow {workflow_id}")

        return _exec

    @app.get("/api/scheduler/tasks")
    async def scheduler_tasks_list():
        tasks = scheduler.list_tasks()
        return {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "workflow_id": t.workflow_id,
                    "trigger_type": t.trigger_type,
                    "trigger_config": t.trigger_config,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "created_at": t.created_at,
                    "last_run": t.last_run,
                    "next_run": t.next_run,
                    "run_count": t.run_count,
                    "fail_count": t.fail_count,
                    "description": t.description,
                    "tags": list(t.tags),
                }
                for t in tasks
            ],
            "count": len(tasks),
        }

    @app.post("/api/scheduler/tasks", status_code=201)
    async def scheduler_tasks_create(body: Dict[str, Any]):
        name = str(body.get("name") or "").strip()
        workflow_id = str(body.get("workflow_id") or "").strip()
        if not name or not workflow_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "name 与 workflow_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        if not store.get(workflow_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"工作流不存在: {workflow_id}"},  # i18n: user-facing
            )
        cron = str(body.get("cron") or "").strip()
        interval_minutes = body.get("interval_minutes")
        if cron and isinstance(interval_minutes, (int, float)) and interval_minutes > 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "cron 与 interval_minutes 只能二选一"},  # i18n: user-facing
            )
        try:
            if cron:
                task_id = scheduler.add_cron_task(
                    name,
                    workflow_id,
                    cron,
                    _make_task_executor(workflow_id),
                    description=str(body.get("description") or ""),
                )
            elif isinstance(interval_minutes, (int, float)) and interval_minutes > 0:
                task_id = scheduler.add_interval_task(
                    name,
                    workflow_id,
                    minutes=int(interval_minutes),
                    executor=_make_task_executor(workflow_id),
                    description=str(body.get("description") or ""),
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "cron 或 interval_minutes 必填其一"},  # i18n: user-facing
                )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": f"触发器无效: {e}"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        t = scheduler.get_task(task_id)
        return {"id": task_id, "name": name, "workflow_id": workflow_id, "status": t.status.value if t else "active"}

    @app.post("/api/scheduler/tasks/{task_id}/pause")
    async def scheduler_tasks_pause(task_id: str):
        if not scheduler.pause_task(task_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"任务不存在: {task_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"id": task_id, "status": "paused"}

    @app.post("/api/scheduler/tasks/{task_id}/resume")
    async def scheduler_tasks_resume(task_id: str):
        if not scheduler.resume_task(task_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"任务不存在: {task_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"id": task_id, "status": "active"}

    @app.post("/api/scheduler/tasks/{task_id}/run-now")
    async def scheduler_tasks_run_now(task_id: str):
        # issue #608: operator run-now from the demand pipeline tab — same
        # executor + bookkeeping as a cron fire; paused tasks 409 (GUI shows
        # the reason instead of a silent skip)
        try:
            triggered = await scheduler.run_task_now(task_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail={"code": "task_not_active", "message": str(e)}) from e
        if not triggered:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"任务不存在: {task_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        t = scheduler.get_task(task_id)
        return {
            "id": task_id,
            "triggered": True,
            "workflow_id": t.workflow_id if t else "",
            "run_count": t.run_count if t else 0,
        }

    # ── issue #608: demand-mail-rail seed — def at app build, cron on startup ──
    if store.get("demand-mail-rail") is None:
        store.save("demand-mail-rail", _DEMAND_MAIL_RAIL_DEF)
        logger.info("[demand-mail-rail] workflow definition seeded")

    @app.on_event("startup")
    async def _seed_demand_mail_rail_task():
        try:
            # cli `web serve` starts the scheduler before uvicorn fires startup
            # events, so persisted tasks are already restored; a bare TestClient
            # never calls start() — load once here so the workflow_id check sees
            # the persisted task either way (re-load is an idempotent overwrite)
            scheduler.load_tasks()
            existing = [t for t in scheduler.list_tasks() if t.workflow_id == "demand-mail-rail"]
            for t in existing:
                # restart path: restored task has no executor (P1-12) — re-bind
                # and lift the auto-pause; user pause keeps its own marker
                scheduler.register_executor(t.id, _make_task_executor("demand-mail-rail"))
            if not existing:
                task_id = scheduler.add_cron_task(
                    "demand-mail-rail-hourly",
                    "demand-mail-rail",
                    "0 * * * *",
                    _make_task_executor("demand-mail-rail"),
                    description="Demand mail rail: hourly poll -> normalize -> match -> dual draft",
                    tags=["demand"],
                )
                logger.info(f"[demand-mail-rail] hourly cron task created: {task_id}")
        except Exception as e:
            logger.warning(f"[demand-mail-rail] task seeding failed (ignored): {e}")

    @app.delete("/api/scheduler/tasks/{task_id}")
    async def scheduler_tasks_delete(task_id: str):
        if not scheduler.remove_task(task_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"任务不存在: {task_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"deleted": True}

    # ── W7 NL delegate (migration doc §6 P1) ──
    @app.post("/api/ai/generate-workflow")
    async def ai_generate_workflow(body: Dict[str, Any]):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "prompt 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        context = body.get("context") if isinstance(body.get("context"), dict) else None
        # W17: optional model override — build a temporary generator with the request's own mlx client (never mutates the shared instance)
        model = str(body.get("model") or "").strip()
        generator = nl_generator
        if model:
            from ..ai.nl_parser import NLWorkflowGenerator

            generator = NLWorkflowGenerator(getattr(nl_generator, "mlx", None), model=model)
        logger.info(f"web API NL generation request: {prompt[:80]} model={model or '(auto)'}")
        try:
            workflow = await generator.generate(prompt, context)
        except Exception as e:
            logger.error(f"web API NL generation failed: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_error", "message": f"生成失败: {e}"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        if not isinstance(workflow, dict) or not workflow.get("nodes"):
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_error", "message": "生成结果缺少 nodes"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"workflow": workflow}

    # ── flywheel compile (issue #625 follow-up): trajectory → compiled workflow ──
    # POST /api/trajectory/compile — session_id (trajectory JSONL file) + optional run_id,
    # or an inline snapshot list. One LLM condition-extraction call, returns an executable
    # Workflow definition (skip_if conditions folded into node config, runnable via runs_create).
    @app.post("/api/trajectory/compile")
    async def trajectory_compile(body: Dict[str, Any]):
        from ..engine.node import NodeRegistry
        from ..engine.workflow import Workflow
        from ..trajectory.compile import compile_workflow, steps_from_jsonl, steps_from_snapshot
        from ..trajectory.recorder import DEFAULT_TRAJECTORY_DIR

        session_id = str(body.get("session_id") or "").strip()
        run_id = str(body.get("run_id") or "").strip()
        model = str(body.get("model") or "").strip()
        workflow_name = str(body.get("workflow_name") or "").strip()
        snapshot = body.get("snapshot") if isinstance(body.get("snapshot"), list) else None
        if not session_id and not snapshot:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "session_id 或 snapshot 必填"},  # i18n: user-facing
            )
        steps: List[Dict[str, Any]] = []
        if session_id:
            # session_id is the filename stem under the trajectory dir; whitelist chars prevent path traversal
            if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": "非法 session_id"},  # i18n: user-facing
                )
            path = Path(DEFAULT_TRAJECTORY_DIR) / f"{session_id}.jsonl"
            if not path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"轨迹文件不存在: {session_id}"},  # i18n: user-facing
                )
            steps = steps_from_jsonl(path, run_id=run_id)
        else:
            steps = steps_from_snapshot(snapshot or [])
        if not steps:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "empty_trajectory",
                    "message": "轨迹中无可编译步骤 (无 post_node_execute 记录)",  # i18n: user-facing
                },
            )
        mlx = getattr(nl_generator, "mlx", None)
        if mlx is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "no_model_client", "message": "模型客户端不可用"},  # i18n: user-facing
            )

        def create_node(name: str):
            node = NodeRegistry.create(name)
            if node is None:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "unknown_node", "message": f"轨迹引用了未注册节点: {name}"},  # i18n: user-facing
                )
            return node

        wf = Workflow(name=workflow_name or f"compiled-{session_id or 'snapshot'}")
        logger.info(
            f"web API trajectory compile request: session={session_id or '-'} run={run_id or '-'} steps={len(steps)} model={model or '(auto)'}"
        )
        try:
            plan, node_ids = await compile_workflow(steps, mlx, create_node, wf, model=model)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"web API trajectory compile failed: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "compile_failed", "message": f"编译失败: {e}"},  # i18n: user-facing
            ) from e
        if not node_ids:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "empty_plan",
                    "message": "条件抽取产出空计划 (轨迹无成功步骤或回复不可解析)",  # i18n: user-facing
                },
            )
        return {"plan": plan, "node_ids": node_ids, "steps_used": len(steps), "workflow": wf.to_dict()}

    # ── issue #148: chat-first delegate — one sentence straight to a deliverable, no forced canvas entry ──
    # Deterministic heuristic (Rule: decisions go through code, not the model): a plan with ≤2 nodes and no
    # high-risk nodes → mode=auto executes directly and returns run_id (frontend streams progress/deliverables);
    # otherwise mode=plan returns the internal execution plan (still saved as a workflow) for one-click approval;
    # DAG editing always remains optional (workflow_id can enter the canvas).
    @app.post("/api/ai/chat-delegate")
    async def ai_chat_delegate(body: Dict[str, Any]):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "prompt 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        context = body.get("context") if isinstance(body.get("context"), dict) else None
        model = str(body.get("model") or "").strip()
        memory = body.get("memory", True)
        generator = nl_generator
        if model:
            from ..ai.nl_parser import NLWorkflowGenerator

            generator = NLWorkflowGenerator(getattr(nl_generator, "mlx", None), model=model)
        logger.info(f"web API chat-delegate: {prompt[:80]} model={model or '(auto)'}")
        try:
            workflow = await generator.generate(prompt, context)
        except Exception as e:
            logger.error(f"web API chat-delegate generation failed: {e}")
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_error", "message": f"生成失败: {e}"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        if not isinstance(workflow, dict) or not workflow.get("nodes"):
            raise HTTPException(
                status_code=502,
                detail={"code": "upstream_error", "message": "生成结果缺少 nodes"},  # # i18n: user-facing
            )  # i18n: user-facing

        from ..engine.node import NodeRegistry
        from ..engine.permission import HIGH_RISK_NODES

        nodes = workflow.get("nodes") or []
        node_names = [str(n.get("name") or "") for n in nodes if isinstance(n, dict)]
        plan_steps = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            reg = NodeRegistry.get(str(n.get("name") or ""))
            display = getattr(reg, "display_name", "") or str(n.get("name") or "")
            plan_steps.append(
                {"node": n.get("name"), "display": display, "label": (n.get("config") or {}).get("label", "")}
            )
        simple = len(nodes) <= 2 and not any(name in HIGH_RISK_NODES for name in node_names)

        workflow_id = f"wf_{uuid.uuid4().hex[:10]}"
        record = store.save(
            workflow_id,
            {
                "name": workflow.get("name") or prompt[:40],
                "description": workflow.get("description") or prompt[:120],
                "workflow": {"name": workflow.get("name") or "", "nodes": nodes, "edges": workflow.get("edges") or []},
            },
        )
        plan = {
            "name": record.get("name", workflow_id),
            "description": record.get("description", ""),
            "steps": plan_steps,
        }
        # issue #509: hallucinated-path hint — unverified paths stripped by the generator pass through to the preview card
        gen_warnings = workflow.get("warnings")
        if isinstance(gen_warnings, list) and gen_warnings:
            plan["warnings"] = [str(w) for w in gen_warnings]

        if simple:
            try:
                run = runs.start(
                    {"name": workflow.get("name") or "", "nodes": nodes, "edges": workflow.get("edges") or []},
                    workflow_id=workflow_id,
                    workflow_name=record.get("name", workflow_id),
                    memory=bool(memory),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request", "message": f"工作流定义无效: {exc}"},  # i18n: user-facing
                ) from exc
            logger.info(f"chat-delegate auto-executed: workflow={workflow_id} run={run['run_id']} ({len(nodes)} nodes)")
            return {"mode": "auto", "workflow_id": workflow_id, "run_id": run["run_id"], "plan": plan}
        logger.info(
            f"chat-delegate plan awaiting approval: workflow={workflow_id} ({len(nodes)} nodes, high-risk={any(n in HIGH_RISK_NODES for n in node_names)})"
        )
        return {"mode": "plan", "workflow_id": workflow_id, "plan": plan}

    # ── issue #150: citation provenance — read a text file by path (within the sandbox, shown in the citation card) ──
    @app.get("/api/files/read")
    async def files_read(path: str = "", line_start: int = 0, line_end: int = 0):
        if not path:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "path 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        from ..security import get_scoped_folder_manager

        p = Path(path).expanduser()
        if not get_scoped_folder_manager().ensure_allowed(p):
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": f"路径不在授权范围内: {path}"},  # # i18n: user-facing
            )  # i18n: user-facing
        if not p.is_file():
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"文件不存在: {path}"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "read_error", "message": f"读取失败: {e}"},  # # i18n: user-facing
            ) from e  # i18n: user-facing
        lines = raw.splitlines()
        total_lines = len(lines)
        lo = max(int(line_start or 1), 1)
        hi = min(int(line_end or total_lines), total_lines) if line_end else total_lines
        selected = "\n".join(lines[lo - 1 : hi])[:_MAX_ARTIFACT_BYTES]
        logger.info(f"web API file read: {p} [{lo}-{hi}/{total_lines}]")
        return {
            "path": str(p),
            "name": p.name,
            "content": selected,
            "line_start": lo,
            "line_end": hi,
            "total_lines": total_lines,
            "truncated": len(selected) >= _MAX_ARTIFACT_BYTES,
        }

    # ── W17 model catalog (migration doc §6 P3; since #126 the default returns chat models only) ──
    # #147: last-successful model list cache — return the stale result instead of 503 when upstream is down,
    # eliminating the frontend retry storm; the next success refreshes automatically ("auto-backfill after service recovery").
    # #172: cold cache (upstream never came online) also no longer 503s — returns a 200 empty list + unavailable=true
    # envelope, so the frontend poller gets a success and stops retrying, rendering an empty state instead of error spam.
    _models_cache: Dict[str, Any] = {"models": None, "ts": 0.0}

    @app.get("/api/models")
    async def models_list(request: Request):
        mlx = getattr(nl_generator, "mlx", None)
        if mlx is None:
            logger.warning("web API model list: model client unavailable, returning empty list envelope")
            return {"models": [], "count": 0, "total": 0, "stale": False, "unavailable": True}
        try:
            models = await mlx.list_models()
            _models_cache["models"] = models
            _models_cache["ts"] = time.time()
        except Exception as e:
            cached = _models_cache["models"]
            if cached is not None:
                age = int(time.time() - _models_cache["ts"])
                logger.warning(
                    f"web API model list fetch failed, returning {age}s-old cache ({len(cached)} entries): {e}"
                )
                models = cached
                stale = True
            else:
                logger.warning(f"web API model list fetch failed (fusion-mlx offline, no cache): {e}")
                return {"models": [], "count": 0, "total": 0, "stale": False, "unavailable": True}
        else:
            stale = False
        # #126: the top-level model picker (W17 picker / DelegatePage) can only select chat models —
        # video/embedding etc. would only fail later during generation. Filtered by default; the raw list is at ?all=1.
        from ..ai.nl_parser import _is_chat_model

        if request.query_params.get("all") != "1":
            chat_models = [m for m in models if _is_chat_model(m)]
            logger.info(f"web API model list: chat {len(chat_models)}/{len(models)} (non-chat filtered by default)")
            return {"models": chat_models, "count": len(chat_models), "total": len(models), "stale": stale}
        return {"models": models, "count": len(models), "total": len(models), "stale": stale}

    # ── issue #155: markdown skill packages — discovery / param form schema / execution ──
    @app.get("/api/skills")
    async def skills_list():
        from ..skills import get_package_registry
        from ..skills.packages import USER_PACKAGES_DIR

        user_dir = str(USER_PACKAGES_DIR)
        packages = get_package_registry().list_packages()
        out = []
        for p in packages:
            d = p.to_dict()
            # issue #255: user-directory packages can be edited/deleted via web; builtin packages are read-only
            d["editable"] = bool(p.source) and p.source.startswith(user_dir)
            out.append(d)
        return {"skills": out, "count": len(out)}

    @app.post("/api/skills/{name:path}/run")
    async def skills_run(name: str, request: Request):
        from ..skills import get_package_registry

        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"/api/skills/{name}/run JSON parse failed: {e}")
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        values = body.get("params") if isinstance(body, dict) and "params" in body else body
        if not isinstance(values, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_params", "message": "params 必须是对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        registry = get_package_registry()
        if registry.get(name) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知 skill 包: {name}"},  # # i18n: user-facing
            )  # i18n: user-facing
        result = await registry.run(name, values)
        if "error" in result:
            status = 400 if result.get("errors") else 500
            return JSONResponse(result, status_code=status)
        return result

    # ── issue #255: markdown skill package CRUD (user directory ~/.fusion-cowork/skills) ──
    def _skill_slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lstrip("/").lower()).strip("-")
        if not slug:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_name", "message": f"技能名无法转为文件名: {name}"},  # i18n: user-facing
            )
        return slug

    def _reload_skill_packages() -> None:
        from ..skills.packages import reset_package_registry

        reset_package_registry()

    @app.get("/api/skills/{name:path}/source")
    async def skills_source(name: str):
        from ..skills import get_package_registry
        from ..skills.packages import USER_PACKAGES_DIR

        pkg = get_package_registry().get(name)
        if pkg is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知 skill 包: {name}"},  # # i18n: user-facing
            )  # i18n: user-facing
        editable = bool(pkg.source) and pkg.source.startswith(str(USER_PACKAGES_DIR))
        markdown = ""
        if editable:
            try:
                markdown = Path(pkg.source).read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(f"[skills] failed to read package source {pkg.source}: {e}")
                raise HTTPException(
                    status_code=500,
                    detail={"code": "read_failed", "message": "读取技能包失败"},  # # i18n: user-facing
                ) from e  # i18n: user-facing
        return {"name": pkg.name, "editable": editable, "markdown": markdown}

    @app.put("/api/skills")
    async def skills_save(request: Request):
        from ..skills import get_package_registry
        from ..skills.packages import USER_PACKAGES_DIR, parse_markdown

        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        markdown = body.get("markdown") if isinstance(body, dict) else None
        if not isinstance(markdown, str) or not markdown.strip():
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_body", "message": "markdown 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            pkg = parse_markdown(markdown)
        except ValueError as e:
            return JSONResponse({"saved": False, "problems": [str(e)]}, status_code=400)
        slug = _skill_slug(pkg.name)
        target = USER_PACKAGES_DIR / f"{slug}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        _reload_skill_packages()
        logger.info(f"[skills] markdown package saved: {pkg.name} -> {target}")
        saved = get_package_registry().get(pkg.name)
        d = (saved or pkg).to_dict()
        d["saved"] = True
        d["editable"] = True
        return d

    @app.delete("/api/skills/{name:path}")
    async def skills_delete(name: str):
        from ..skills import get_package_registry
        from ..skills.packages import USER_PACKAGES_DIR

        pkg = get_package_registry().get(name)
        if pkg is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知 skill 包: {name}"},  # # i18n: user-facing
            )  # i18n: user-facing
        source = Path(pkg.source or "")
        if not (str(source).startswith(str(USER_PACKAGES_DIR)) and source.exists()):
            logger.warning(f"[skills] refused to delete non-user-directory package: {pkg.name} ({pkg.source})")
            raise HTTPException(
                status_code=400,
                detail={"code": "builtin", "message": "内置技能不可删除"},  # # i18n: user-facing
            )  # i18n: user-facing
        source.unlink()
        _reload_skill_packages()
        logger.info(f"[skills] markdown package deleted: {pkg.name} ({source})")
        return {"deleted": True, "name": pkg.name}

    @app.get("/api/skills/{name:path}")
    async def skills_get(name: str):
        from ..skills import get_package_registry

        pkg = get_package_registry().get(name)
        if pkg is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知 skill 包: {name}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return pkg.to_dict()

    # ── issue #170: Extensions console — Skills + Knowledge (f-ecommerce E2 migration) ──
    # Skills: user JSON skill packs (~/.fusion-cowork/skills/<name>/skill.json,
    #   handler = workflow|node|script); installing registers into the SkillRegistry (callable via
    #   MCP skill_run); script packs install only under a static env key (no local auth configured = local operator).
    # Knowledge: fusion-rag (:11436) KB console — reuses the SpaceKB-style KBClient binding;
    #   wire state lands in ~/.fusion-cowork/web/rag_wired.json; capability id knowledge.<kb>.search.
    def _extensions_rag():
        from ..ai.mlx_client import KBClient

        return KBClient(base_url=(rag_base_url or os.environ.get("FUSION_RAG_URL", "http://127.0.0.1:11436")))

    def _load_wired() -> List[str]:
        path = rag_wired_path or (Path.home() / ".fusion-cowork" / "web" / "rag_wired.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            wired = data.get("wired") if isinstance(data, dict) else None
            return [str(k) for k in wired] if isinstance(wired, list) else []
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"rag wired state read failed {path}: {e}")
            return []

    def _save_wired(wired: List[str]) -> None:
        path = rag_wired_path or (Path.home() / ".fusion-cowork" / "web" / "rag_wired.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"wired": sorted(set(wired))}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _script_install_allowed(request: Request) -> bool:
        # script packs = arbitrary code — installable only with a static env key (or when local auth is unconfigured);
        # gateway-issued fm- module keys go through the browser surface and can never install script packs.
        if not auth_configured():
            return True
        return extract_caller_key(request.headers) == _api_key() and bool(_api_key())

    def _validate_skill_doc(doc: Dict[str, Any]) -> tuple:
        from ..skills.persistence import VALID_TYPES

        problems: List[str] = []
        name = str(doc.get("name") or "").strip().strip("/")
        if not name or "/" in name or name.startswith("."):
            problems.append("missing/invalid name")
        stype = str(doc.get("type") or "node")
        if stype not in VALID_TYPES:
            problems.append(f"type must be one of {VALID_TYPES}")
        if stype == "script" and not str(doc.get("script") or "").strip():
            problems.append("script skills require the script field")
        if stype == "workflow" and not str(doc.get("template_id") or "").strip():
            problems.append("workflow skills require template_id")
        if stype == "node" and not str(doc.get("node") or "").strip():
            problems.append("node skills require the node field")
        return name, stype, problems

    @app.get("/api/extensions/skills")
    async def extensions_skills_list():
        from ..skills.persistence import list_skill_packs
        from ..skills.registry import SkillRegistry

        registered = {s.name for s in SkillRegistry().list_skills()}
        packs = list_skill_packs(skills_dir=skills_dir)
        out = [
            {
                "name": p.name,
                "description": p.description,
                "type": p.type,
                "category": p.category,
                "capability": f"skill.{p.name}",
                "cap_status": "active" if p.name in registered else "missing",
            }
            for p in packs
        ]
        return {"skills": out, "count": len(out)}

    @app.post("/api/extensions/skills")
    async def extensions_skills_install(request: Request):
        from ..skills.persistence import SkillPack, make_pack_handler, save_skill_pack
        from ..skills.registry import Skill, SkillRegistry

        try:
            doc = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        if not isinstance(doc, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_body", "message": "body 必须是 JSON 对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        name, stype, problems = _validate_skill_doc(doc)
        if problems:
            return JSONResponse({"installed": False, "problems": problems}, status_code=400)
        if stype == "script" and not _script_install_allowed(request):
            logger.warning("[extensions] script pack install refused (not a static admin key): %s", name)
            return JSONResponse(
                {"installed": False, "problems": ["script skills are admin-only (arbitrary code)"]},
                status_code=403,
            )
        pack = SkillPack.from_manifest(doc)
        pack.name = name
        try:
            save_skill_pack(pack, skills_dir=skills_dir)
        except ValueError as e:
            return JSONResponse({"installed": False, "problems": [str(e)]}, status_code=400)
        registry = SkillRegistry()
        registry.register(
            Skill(
                name=pack.name,
                description=pack.description,
                handler=make_pack_handler(pack, skills_dir),
                category=pack.category,
                aliases=pack.aliases,
            )
        )
        logger.info("[extensions] skill %s installed (type=%s, capability=skill.%s)", name, stype, name)
        return {"installed": True, "name": name, "capability": f"skill.{name}"}

    @app.post("/api/extensions/skills/{name}/test")
    async def extensions_skills_test(name: str, request: Request):
        import time as _time

        from ..skills.persistence import list_skill_packs, make_pack_handler

        try:
            body = await request.json()
        except Exception:
            body = {}
        args = str(body.get("args", "")) if isinstance(body, dict) else ""
        pack = next((p for p in list_skill_packs(skills_dir=skills_dir) if p.name == name), None)
        if pack is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知 skill 包: {name}"},  # # i18n: user-facing
            )  # i18n: user-facing
        started = _time.monotonic()
        result = await make_pack_handler(pack, skills_dir)(args)
        failed = isinstance(result, dict) and "error" in result
        duration_ms = int((_time.monotonic() - started) * 1000)
        logger.info("[extensions] skill %s test run: ok=%s (%dms)", name, not failed, duration_ms)
        return {"ok": not failed, "result": result, "duration_ms": duration_ms}

    @app.delete("/api/extensions/skills/{name}")
    async def extensions_skills_delete(name: str):
        from ..skills.persistence import delete_skill_pack
        from ..skills.registry import SkillRegistry

        deleted = delete_skill_pack(name, skills_dir=skills_dir)
        removed = SkillRegistry().unregister(name)
        logger.info("[extensions] skill %s removed (deleted=%s, capability_removed=%s)", name, deleted, removed)
        return {"deleted": deleted, "capability_removed": removed}

    def _norm_kb_id(b: Dict[str, Any]) -> str:
        return str(b.get("id") or b.get("kb_id") or b.get("name") or "")

    @app.get("/api/extensions/rag/health")
    async def extensions_rag_health():
        client = _extensions_rag()
        try:
            resp = await client.client.get("/health")
            data = resp.json() if resp.status_code < 400 else {}
            if resp.status_code >= 400:
                return {"ok": False, "error": f"fusion-rag {resp.status_code}"}
            return {"ok": True, "version": data.get("version", ""), "status": data.get("status", "")}
        except Exception as e:
            logger.warning("[extensions] fusion-rag unreachable: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            await client.close()

    @app.get("/api/extensions/rag/kb")
    async def extensions_rag_kb_list(request: Request):
        client = _extensions_rag()
        try:
            resp = await client.client.get("/kb/bases")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("[extensions] fusion-rag KB list failed: %s", e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        bases = data if isinstance(data, list) else (data.get("bases") or data.get("items") or [])
        q = (request.query_params.get("q") or "").strip().lower()
        if q:
            bases = [
                b for b in bases if q in str(b.get("name", "")).lower() or q in str(b.get("description", "")).lower()
            ]
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            page_size = max(1, min(int(request.query_params.get("page_size", "10")), 50))
        except ValueError:
            page, page_size = 1, 10
        total = len(bases)
        window = bases[(page - 1) * page_size : page * page_size]
        wired = set(_load_wired())

        async def _doc_count(b: Dict[str, Any]) -> Any:
            kb_id = _norm_kb_id(b)
            c = _extensions_rag()
            try:
                r = await c.client.get(f"/kb/bases/{kb_id}/documents")
                if r.status_code >= 400:
                    return None
                docs = r.json()
                docs = docs if isinstance(docs, list) else (docs.get("documents") or docs.get("items") or [])
                return len(docs)
            except Exception:
                return None
            finally:
                await c.close()

        import asyncio as _asyncio

        counts = await _asyncio.gather(*(_doc_count(b) for b in window))
        rows = []
        for b, count in zip(window, counts):
            kb_id = _norm_kb_id(b)
            is_wired = kb_id in wired
            rows.append(
                {
                    "kb_id": kb_id,
                    "name": b.get("name", kb_id),
                    "description": b.get("description", ""),
                    "document_count": count,
                    "capability": f"knowledge.{kb_id}.search" if is_wired else None,
                    "cap_status": "active" if is_wired else None,
                }
            )
        return {"bases": rows, "total": total, "page": page, "page_size": page_size}

    @app.post("/api/extensions/rag/kb")
    async def extensions_rag_kb_create(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
        description = str(body.get("description") or "").strip() if isinstance(body, dict) else ""
        wire = bool(body.get("wire")) if isinstance(body, dict) else False
        if not name:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_name", "message": "name 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        client = _extensions_rag()
        try:
            kb_id = await client.create_kb(name, description)
            if wire:
                _save_wired(_load_wired() + [kb_id])
        except Exception as e:
            logger.error("[extensions] fusion-rag KB create failed: %s", e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        logger.info("[extensions] KB %s created (wired=%s)", kb_id, wire)
        return {"kb_id": kb_id, "capability": f"knowledge.{kb_id}.search" if wire else None}

    @app.post("/api/extensions/rag/kb/{kb_id}/wire")
    async def extensions_rag_kb_wire(kb_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        wired_flag = bool(body.get("wired")) if isinstance(body, dict) else False
        wired = set(_load_wired())
        capabilities_disabled: List[str] = []
        if wired_flag:
            wired.add(kb_id)
        else:
            wired.discard(kb_id)
            capabilities_disabled = [f"knowledge.{kb_id}.search"]
        _save_wired(sorted(wired))
        logger.info("[extensions] KB %s wired=%s", kb_id, wired_flag)
        return {
            "wired": wired_flag,
            "capability": f"knowledge.{kb_id}.search" if wired_flag else None,
            "capabilities_disabled": capabilities_disabled,
        }

    @app.get("/api/extensions/rag/kb/{kb_id}/documents")
    async def extensions_rag_kb_documents(kb_id: str):
        client = _extensions_rag()
        try:
            resp = await client.list_documents(kb_id)
        except Exception as e:
            logger.warning("[extensions] fusion-rag document list failed %s: %s", kb_id, e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        docs = resp if isinstance(resp, list) else (resp.get("documents") or resp.get("items") or [])
        return {"documents": docs}

    @app.post("/api/extensions/rag/kb/{kb_id}/documents")
    async def extensions_rag_kb_upload(kb_id: str, request: Request):
        # multipart upload: browser files land in ~/.fusion-cowork/web/rag_uploads/<kb>/ first,
        # then KBClient.upload_file (same binding as SpaceKB) ingests them into fusion-rag.
        try:
            form = await request.form()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_form", "message": "multipart form 解析失败"},  # i18n: user-facing
            ) from e
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_file", "message": "file 字段必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        filename = Path(upload.filename or "").name
        if not filename or Path(filename).suffix.lower() not in (".md", ".txt"):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_type", "message": "仅接受 .md/.txt"},  # # i18n: user-facing
            )  # i18n: user-facing
        if not kb_id or any(c in kb_id for c in "/\\") or kb_id.startswith("."):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_kb", "message": "非法 kb_id"},  # # i18n: user-facing
            )  # i18n: user-facing
        upload_dir = Path.home() / ".fusion-cowork" / "web" / "rag_uploads" / kb_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / filename
        size = 0
        try:
            with dest.open("wb") as f:
                while chunk := await upload.read(1 << 20):
                    size += len(chunk)
                    if size > 10 << 20:
                        raise HTTPException(
                            status_code=400,
                            detail={"code": "too_large", "message": "文件超过 10MB 上限"},  # i18n: user-facing
                        )
                    f.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        except OSError as e:
            logger.error("[extensions] upload write failed %s: %s", dest, e)
            raise HTTPException(status_code=500, detail={"code": "write_failed", "message": str(e)}) from e
        client = _extensions_rag()
        try:
            result = await client.upload_file(kb_id, str(dest), file_name=filename)
        except Exception as e:
            logger.error("[extensions] fusion-rag ingest failed %s/%s: %s", kb_id, filename, e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        result = result if isinstance(result, dict) else {}
        logger.info("[extensions] KB %s ingested %s (%d bytes): %s", kb_id, filename, size, result.get("chunks", "?"))
        return {"chunks": result.get("chunks"), **{k: v for k, v in result.items() if k != "chunks"}}

    @app.post("/api/extensions/rag/kb/{kb_id}/search")
    async def extensions_rag_kb_search(kb_id: str, request: Request):
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_json", "message": "invalid json"}) from e
        query = str(body.get("query") or "").strip() if isinstance(body, dict) else ""
        if not query:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_query", "message": "query 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            top_k = int(body.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(top_k, 20))
        client = _extensions_rag()
        try:
            hits = await client.search(kb_id, query, top_k=top_k)
        except Exception as e:
            logger.warning("[extensions] fusion-rag search failed %s: %s", kb_id, e)
            raise HTTPException(status_code=503, detail={"code": "rag_unavailable", "message": str(e)}) from e
        finally:
            await client.close()
        hits = hits if isinstance(hits, list) else []
        return [
            {"text": h.get("text", ""), "doc_path": h.get("doc_path", ""), "score": h.get("score")}
            for h in hits
            if isinstance(h, dict)
        ]

    # ── issue #228: capability marketplace — unified Skills / Connectors / Sub-agents lifecycle ──
    from ..capabilities import load_connector_config, normalize_workspace, search_items

    _CAP_KIND = {"skill": "skills", "connector": "connectors", "subagent": "subagents"}

    async def _cap_workspace(raw: Any) -> str:
        ws = normalize_workspace(raw)
        if ws == normalize_workspace("") or space_store is None:
            return ws
        try:
            space = await space_store.get_space(ws)
        except Exception as e:
            logger.warning(f"capability marketplace: workspace validation failed {ws}: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": str(e)}) from e
        if space is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"工作区不存在: {ws}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return ws

    async def _cap_catalog(ws: str, types: Optional[List[str]] = None) -> Dict[str, Any]:
        return await build_catalog(
            ws,
            store=capability_store,
            skills_dir=skills_dir,
            connector_dir=connector_dir,
            rag_wired_path=rag_wired_path,
            studio_base_url=studio_base_url,
            studio_api_key=studio_api_key,
            transport=capabilities_transport,
            types=types,
        )

    async def _cap_item(ws: str, cap_type: str, cap_id: str) -> Optional[Dict[str, Any]]:
        catalog = await _cap_catalog(ws, [cap_type])
        return next((i for i in catalog[_CAP_KIND[cap_type]] if i["id"] == cap_id), None)

    def _cap_type_or_400(cap_type: str) -> str:
        if cap_type not in _CAP_KIND:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_type", "message": f"未知能力类型: {cap_type}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return cap_type

    async def _cap_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"capability marketplace request body parse failed: {e}")
            return {}
        return body if isinstance(body, dict) else {}

    @app.get("/api/capabilities")
    async def capabilities_list(workspace_id: str = "", q: str = "", type: str = ""):
        ws = await _cap_workspace(workspace_id)
        types = [type] if type in _CAP_KIND else None
        catalog = await _cap_catalog(ws, types)
        query = q.strip().lower()
        if query:
            for key in _CAP_KIND.values():
                catalog[key] = [
                    i
                    for i in catalog[key]
                    if query
                    in " ".join(
                        str(i.get(k) or "") for k in ("name", "title", "title_en", "description", "description_en")
                    ).lower()
                ]
            catalog["count"] = sum(len(catalog[k]) for k in _CAP_KIND.values())
        return catalog

    @app.post("/api/capabilities/{cap_type}/{cap_id}/install")
    async def capabilities_install(cap_type: str, cap_id: str, request: Request):
        _cap_type_or_400(cap_type)
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        item = await _cap_item(ws, cap_type, cap_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知能力: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        entry = capability_store.install(
            ws,
            cap_type,
            cap_id,
            name=str(item.get("title") or item.get("name") or cap_id),
            source=str(item.get("source") or ""),
            spawn_policy=item.get("spawn_policy") if cap_type == "subagent" else None,
            actor=str(body.get("actor") or ""),
        )
        logger.info(f"capability marketplace install: ws={ws} {cap_id} source={item.get('source')}")
        return {"installed": True, "workspace_id": ws, "capability": entry.to_public()}

    @app.delete("/api/capabilities/{cap_type}/{cap_id}")
    async def capabilities_uninstall(cap_type: str, cap_id: str, workspace_id: str = ""):
        _cap_type_or_400(cap_type)
        ws = normalize_workspace(workspace_id)
        removed = capability_store.uninstall(ws, cap_type, cap_id)
        return {"uninstalled": removed, "workspace_id": ws, "id": cap_id}

    @app.patch("/api/capabilities/{cap_type}/{cap_id}/enabled")
    async def capabilities_enabled(cap_type: str, cap_id: str, request: Request):
        _cap_type_or_400(cap_type)
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        enabled = bool(body.get("enabled", True))
        entry = capability_store.set_enabled(ws, cap_type, cap_id, enabled, actor=str(body.get("actor") or ""))
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_installed", "message": f"未安装: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"workspace_id": ws, "capability": entry.to_public()}

    @app.post("/api/capabilities/connectors/{cap_id}/scopes")
    async def capabilities_scopes(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        item = await _cap_item(ws, "connector", cap_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知连接器: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        known = {str(s.get("id")): bool(s.get("sensitive")) for s in item.get("scopes") or []}
        requested = [str(s) for s in body.get("scopes") or [] if str(s)]
        unknown = [s for s in requested if s not in known]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_scope", "message": f"未知 scope: {unknown}"},  # # i18n: user-facing
            )  # i18n: user-facing
        sensitive = [s for s in requested if known.get(s)]
        if sensitive and body.get("approve") is not True:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "sensitive_scope_requires_approval",
                    "message": f"敏感 scope 需显式批准: {sensitive}",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        entry = capability_store.grant_scopes(ws, cap_id, requested, actor=str(body.get("actor") or ""))
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_installed", "message": f"未安装: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"workspace_id": ws, "capability": entry.to_public(), "sensitive_approved": sensitive}

    @app.post("/api/capabilities/connectors/{cap_id}/oauth/start")
    async def capabilities_oauth_start(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        item = await _cap_item(ws, "connector", cap_id)
        if item is None or (item.get("auth") or {}).get("kind") != "oauth":
            raise HTTPException(
                status_code=404,
                detail={"code": "oauth_not_configured", "message": f"无 OAuth 配置: {cap_id}"},  # i18n: user-facing
            )
        raw_id = str(item.get("name") or "")
        cfg = load_connector_config(connector_dir, raw_id) or {}
        oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else None
        if not oauth_cfg:
            raise HTTPException(
                status_code=404,
                detail={"code": "oauth_not_configured", "message": f"无 OAuth 配置: {cap_id}"},  # i18n: user-facing
            )
        try:
            started = oauth_flow.start(cap_id, oauth_cfg, workspace_id=ws)
        except Exception as e:
            logger.warning(f"OAuth flow start failed {cap_id}: {e}")
            raise HTTPException(status_code=400, detail={"code": "oauth_not_configured", "message": str(e)}) from e
        return started

    @app.get("/api/capabilities/oauth/callback")
    async def capabilities_oauth_callback(code: str = "", state: str = "", error: str = ""):
        from ..security.encryption import encrypt_at_rest

        if error:
            logger.warning(f"OAuth callback returned error: {error}")
            return JSONResponse({"connected": False, "error": error}, status_code=400)
        pending = oauth_flow.pop_state(state)
        if pending is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "oauth_state_invalid", "message": "state 未知或已过期"},  # i18n: user-facing
            )
        if not code:
            raise HTTPException(
                status_code=400,
                detail={"code": "oauth_code_missing", "message": "缺少 code"},  # # i18n: user-facing
            )  # i18n: user-facing
        cap_id = str(pending.get("connector_id") or "")
        ws = str(pending.get("workspace_id") or "")
        item = await _cap_item(ws, "connector", cap_id)
        raw_id = str((item or {}).get("name") or "")
        cfg = load_connector_config(connector_dir, raw_id) or {}
        oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
        try:
            tokens = await oauth_flow.exchange(pending, code, oauth_cfg)
        except Exception as e:
            logger.warning(f"OAuth token exchange failed {cap_id}: {e}")
            raise HTTPException(status_code=502, detail={"code": "oauth_exchange_failed", "message": str(e)}) from e
        encrypted = {k: encrypt_at_rest(str(v)) for k, v in tokens.items()}
        capability_store.set_credentials(ws, cap_id, encrypted)
        scopes = [str(s) for s in pending.get("scopes") or []]
        if scopes:
            capability_store.grant_scopes(ws, cap_id, scopes)
        logger.info(f"OAuth connect completed: ws={ws} {cap_id} scopes={scopes}")
        return {"connected": True, "workspace_id": ws, "connector_id": cap_id, "scopes": scopes}

    @app.post("/api/capabilities/connectors/{cap_id}/revoke")
    async def capabilities_oauth_revoke(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        revoked = capability_store.clear_credentials(ws, cap_id)
        return {"revoked": revoked, "workspace_id": ws, "id": cap_id}

    @app.get("/api/capabilities/{cap_type}/{cap_id}/usage")
    async def capabilities_usage(cap_type: str, cap_id: str, workspace_id: str = ""):
        _cap_type_or_400(cap_type)
        ws = normalize_workspace(workspace_id)
        usage = capability_store.usage(ws, cap_type, cap_id)
        if usage is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_installed", "message": f"未安装: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        usage["workspace_id"] = ws
        return usage

    @app.get("/api/capabilities/subagents/{cap_id}/definition")
    async def capabilities_definition(cap_id: str, workspace_id: str = ""):
        ws = await _cap_workspace(workspace_id)
        item = await _cap_item(ws, "subagent", cap_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知子代理: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        kind = str((item.get("detail") or {}).get("kind") or "")
        if kind == "template":
            from ..templates import TemplateManager

            tpl = TemplateManager().get_template(str(item["detail"]["template_id"]))
            if tpl is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": "模板不存在"},  # # i18n: user-facing
                )  # i18n: user-facing
            return {"kind": "template", "definition": tpl}
        import httpx

        # product-level call (roadmap §6): capability pitch instantiation starts
        # a graph run on the agent-studio daemon — the graph lives there.
        base = (studio_base_url or os.environ.get("FUSION_AGENT_STUDIO_URL", "http://127.0.0.1:11442")).rstrip("/")
        key = (studio_api_key or os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "")).strip()
        headers = {"x-api-key": key} if key else {}
        detail = item.get("detail") or {}
        path = (
            f"/v1/graphs/{detail.get('graph_id')}"
            if kind == "graph"
            else f"/v1/agents/{detail.get('agent_id')}/definition"
        )
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=headers, timeout=5.0, transport=capabilities_transport
            ) as client:
                resp = await client.get(path)
                resp.raise_for_status()
                return {"kind": kind, "definition": resp.json()}
        except Exception as e:
            logger.warning(f"subagent definition fetch failed {cap_id}: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_unavailable", "message": str(e)}) from e

    @app.post("/api/capabilities/subagents/{cap_id}/spawn")
    async def capabilities_spawn(cap_id: str, request: Request):
        body = await _cap_body(request)
        ws = await _cap_workspace(body.get("workspace_id"))
        entry = capability_store.get(ws, "subagent", cap_id)
        if entry is None or not entry.enabled:
            raise HTTPException(
                status_code=409,
                detail={"code": "not_enabled", "message": f"子代理未启用: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        item = await _cap_item(ws, "subagent", cap_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"未知子代理: {cap_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        detail = item.get("detail") or {}
        policy = entry.spawn_policy or item.get("spawn_policy") or {}
        if policy.get("requires_approval") and body.get("approve") is not True:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "spawn_approval_required",
                    "message": f"子代理派生需显式批准: {cap_id}",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        text = str(body.get("input") or body.get("goal") or "")
        kind = str(detail.get("kind") or "")
        if kind == "template":
            from ..templates import TemplateManager

            wf = TemplateManager().template_to_workflow(str(detail.get("template_id") or ""))
            if wf is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": "模板不存在"},  # # i18n: user-facing
                )  # i18n: user-facing
            started = runs.start(
                wf.to_dict(),
                workflow_id=f"capability:{cap_id}",
                workflow_name=item.get("title") or wf.name,
                source="capability",
                labels={"capability": cap_id},
                space_id=ws,
            )
            logger.info(f"subagent spawn (template): ws={ws} {cap_id} run={started['run_id']}")
            return {"kind": "template", "run_id": started["run_id"], "route": f"/runs/{started['run_id']}"}
        import httpx

        # product-level call (roadmap §6): subagent spawn (capability pitch)
        # submits an agent run to the agent-studio daemon — the agent runtime
        # lives there.
        base = (studio_base_url or os.environ.get("FUSION_AGENT_STUDIO_URL", "http://127.0.0.1:11442")).rstrip("/")
        key = (studio_api_key or os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "")).strip()
        headers = {"x-api-key": key} if key else {}
        timeout = float(policy.get("timeout_s") or 120)
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=headers, timeout=timeout, transport=capabilities_transport
            ) as client:
                if kind == "graph":
                    graph_id = str(detail.get("graph_id") or "")
                    resp = await client.post(
                        f"/v1/graphs/{graph_id}/execute",
                        json={"graph_id": graph_id, "input_text": text},
                    )
                else:
                    resp = await client.post(
                        "/v1/runs",
                        json={"goal": text, "agent_id": str(detail.get("agent_id") or "")},
                    )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            logger.warning(f"subagent spawn failed {cap_id}: {e}")
            raise HTTPException(status_code=502, detail={"code": "upstream_unavailable", "message": str(e)}) from e
        capability_store.record_usage(
            ws, "subagent", cap_id, workflow=str(item.get("title") or ""), detail=text[:80], verdict="spawned"
        )
        logger.info(f"subagent spawn ({kind}): ws={ws} {cap_id}")
        return {"kind": kind, "result": payload, "spawn_policy": policy}

    # ── issue #153: managed browser — launch / close / status / live screenshot sidebar ──
    @app.post("/api/browser/launch")
    async def browser_launch(request: Request):
        from ..managed_browser import ManagedBrowserError, get_managed_browser

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_body", "message": "body 必须是对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            info = await get_managed_browser().launch(
                url=str(body.get("url") or "about:blank"),
                port=int(body.get("port") or 0),
                headless=bool(body.get("headless")),
                import_cookies=str(body.get("import_cookies") or ""),
            )
        except ManagedBrowserError as e:
            logger.error(f"managed browser launch failed: {e}")
            raise HTTPException(status_code=503, detail={"code": "browser_unavailable", "message": str(e)}) from e
        except Exception as e:
            logger.error(f"managed browser launch error: {e}")
            raise HTTPException(status_code=500, detail={"code": "browser_error", "message": str(e)}) from e
        return info

    @app.post("/api/browser/close")
    async def browser_close():
        from ..managed_browser import get_managed_browser

        closed = await get_managed_browser().close()
        return {"closed": closed}

    @app.get("/api/browser/status")
    async def browser_status():
        from ..managed_browser import get_managed_browser

        return get_managed_browser().status()

    @app.get("/api/browser/screenshot")
    async def browser_screenshot():
        from ..managed_browser import get_managed_browser
        from ..nodes.browser.cdp_client import CDPClient

        info = get_managed_browser().status()
        if not info.get("running"):
            raise HTTPException(
                status_code=409,
                detail={"code": "not_running", "message": "托管浏览器未运行"},  # # i18n: user-facing
            )  # i18n: user-facing
        client = CDPClient(port=int(info["port"]))
        try:
            await client.connect()
            data = await client.screenshot()
        except Exception as e:
            logger.warning(f"managed browser screenshot failed: {e}")
            raise HTTPException(status_code=502, detail={"code": "screenshot_failed", "message": str(e)}) from e
        finally:
            await client.disconnect()
        return Response(content=data, media_type="image/png")

    # ── issue #162: fusion-browser engine — per-run profile / login-state import / panel screenshot stream ──
    def _fusion_browser():
        from ..fusion_browser import get_fusion_browser

        return get_fusion_browser()

    async def _fusion_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_body", "message": "body 必须是对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        return body

    @app.post("/api/fusion-browser/launch")
    async def fusion_browser_launch(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        browser = _fusion_browser()
        origins = body.get("allowed_origins")
        if isinstance(origins, list):
            browser.allowed_origins = [str(o) for o in origins if str(o).strip()]
        try:
            return await asyncio.to_thread(browser.launch, str(body.get("url") or "") or None)
        except FusionBrowserError as e:
            logger.error(f"fusion-browser launch failed: {e}")
            raise HTTPException(status_code=503, detail={"code": "browser_unavailable", "message": str(e)}) from e

    @app.get("/api/fusion-browser/status")
    async def fusion_browser_status():
        return _fusion_browser().status()

    @app.post("/api/fusion-browser/session")
    async def fusion_browser_session(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        browser = _fusion_browser()
        try:
            return await asyncio.to_thread(
                browser.create_session,
                run_id=str(body.get("run_id") or "") or None,
                profile_id=str(body.get("profile_id") or "") or None,
                initial_url=str(body.get("initial_url") or body.get("url") or "") or None,
                mode=str(body.get("mode") or "") or None,
            )
        except FusionBrowserError as e:
            status = 409 if "未运行" in str(e) else 502  # i18n: user-facing
            raise HTTPException(status_code=status, detail={"code": "session_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/import-cookies")
    async def fusion_browser_import_cookies(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        session_id = str(body.get("session_id") or "")
        if not session_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_session", "message": "session_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            return await asyncio.to_thread(
                _fusion_browser().import_cookies,
                session_id,
                str(body.get("browser") or "firefox"),
                str(body.get("profile") or "") or None,
            )
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "import_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/execute")
    async def fusion_browser_execute(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        session_id = str(body.get("session_id") or "")
        action = str(body.get("action") or "")
        if not session_id or not action:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_params", "message": "session_id 与 action 必填"},  # i18n: user-facing
            )
        try:
            return await asyncio.to_thread(
                _fusion_browser().execute,
                session_id,
                action,
                str(body.get("target_node_id") or "") or None,
                str(body.get("payload_text") or "") or None,
            )
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "execute_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/finish")
    async def fusion_browser_finish(request: Request):
        from ..fusion_browser import FusionBrowserError

        body = await _fusion_body(request)
        try:
            return await asyncio.to_thread(
                _fusion_browser().finish,
                str(body.get("run_id") or "") or None,
                not bool(body.get("keep_profile")),
            )
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "finish_failed", "message": str(e)}) from e

    @app.post("/api/fusion-browser/close")
    async def fusion_browser_close():
        return await asyncio.to_thread(_fusion_browser().close)

    @app.get("/api/fusion-browser/screenshot")
    async def fusion_browser_screenshot(session_id: str = ""):
        from ..fusion_browser import FusionBrowserError

        if not session_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_session", "message": "session_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            data = await asyncio.to_thread(_fusion_browser().screenshot_png, session_id)
        except FusionBrowserError as e:
            status = 409 if "未运行" in str(e) else 502  # i18n: user-facing
            raise HTTPException(status_code=status, detail={"code": "screenshot_failed", "message": str(e)}) from e
        return Response(content=data, media_type="image/png")

    @app.get("/api/fusion-browser/stream")
    async def fusion_browser_stream(request: Request, session_id: str = ""):
        from ..fusion_browser import FusionBrowserError

        if not session_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_session", "message": "session_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        browser = _fusion_browser()
        if not browser.socket_path:
            raise HTTPException(
                status_code=409,
                detail={"code": "not_running", "message": "fusion-browser 未运行"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            frames = browser.stream_frames(session_id)
        except FusionBrowserError as e:
            raise HTTPException(status_code=502, detail={"code": "stream_failed", "message": str(e)}) from e
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)

        def _pump() -> None:
            try:
                for frame in frames:
                    while True:
                        try:
                            loop.call_soon_threadsafe(queue.put_nowait, frame)
                            break
                        except asyncio.QueueFull:
                            time.sleep(0.05)
            except Exception as e:
                logger.warning(f"fusion-browser panel stream interrupted: {e}")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        pump = asyncio.create_task(asyncio.to_thread(_pump))

        async def _events():
            try:
                while True:
                    frame = await queue.get()
                    if frame is None:
                        yield "event: stream_end\ndata: {}\n\n"
                        return
                    payload = {
                        "seq": frame.get("seq"),
                        "url": frame.get("url", ""),
                        "title": frame.get("title", ""),
                        "ts_ms": frame.get("ts_ms"),
                        "png": frame.get("png", ""),
                    }
                    yield f"event: screenshot_frame\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if await request.is_disconnected():
                        return
            finally:
                pump.cancel()

        return StreamingResponse(_events(), media_type="text/event-stream")

    # ── issue #139: manifest ext backend — ⌘K cross-module search + notification center poll sources ──
    @app.get("/api/search")
    async def search_all(q: str = ""):
        query = (q or "").strip().lower()
        if not query:
            return {"items": []}
        items: List[Dict[str, Any]] = []
        for wf in store.list():
            name = str(wf.get("name") or wf.get("id") or "")
            desc = str(wf.get("description") or "")
            if query in name.lower() or query in desc.lower():
                items.append({"title": name, "route": f"/canvas/{wf['id']}", "kind": "workflow"})
        for r in runs.list():
            name = str(r.get("workflow_name") or r.get("run_id") or "")
            if query in name.lower():
                items.append({"title": f"运行: {name}", "route": "/runs", "kind": "run"})  # i18n: user-facing
        if space_store is not None:
            try:
                spaces = await space_store.list_spaces(limit=100)
            except Exception as e:
                logger.warning(f"space search failed (skipped): {e}")
                spaces = []
            for s in spaces:
                name = str(getattr(s, "name", "") or getattr(s, "id", ""))
                if query in name.lower():
                    items.append({"title": name, "route": "/spaces", "kind": "space"})
        # issue #228: capability marketplace items merged into ⌘K search (skills/connectors/subagents)
        try:
            for hit in search_items(
                q,
                normalize_workspace(""),
                store=capability_store,
                skills_dir=skills_dir,
                connector_dir=connector_dir,
                rag_wired_path=rag_wired_path,
            ):
                items.append(
                    {
                        "title": hit.get("title") or hit.get("name") or hit["id"],
                        "route": f"/capabilities/{hit['type']}",
                        "kind": "capability",
                        "capability_id": hit["id"],
                    }
                )
        except Exception as e:
            logger.warning(f"capability marketplace search failed (skipped): {e}")
        items = items[:32]
        logger.info(f"web API search q={q!r} hits {len(items)}")
        return {"items": items}

    @app.get("/api/notifications/approvals")
    async def notifications_approvals():
        if permission_manager is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "unavailable", "message": "权限管理器不可用"},  # # i18n: user-facing
            )  # i18n: user-facing
        list_merged = getattr(permission_manager, "list_pending", None)
        approvals = list_merged() if callable(list_merged) else permission_manager.list_pending_guard()
        items = [
            {
                "id": a.get("action_id", ""),
                "title": f"审批: {a.get('tool_name', '')}",  # i18n: user-facing
                "route": "/approvals",
                "level": "warning",
            }
            for a in approvals
        ][:32]
        logger.info(f"web API notification source approvals poll: {len(items)} pending")
        return {"items": items}

    @app.get("/api/notifications/runs")
    async def notifications_runs():
        terminal = {"completed", "failed", "canceled"}
        rows = [r for r in runs.list() if r.get("status") in terminal]
        rows.sort(key=lambda x: x.get("finished_at") or x.get("created_at") or 0, reverse=True)
        items = [
            {
                "id": r.get("run_id", ""),
                "title": f"运行结束: {r.get('workflow_name') or r.get('run_id')} ({r.get('status')})",  # i18n: user-facing
                "route": "/runs",
                "level": "error" if r.get("status") == "failed" else "info",
            }
            for r in rows[:32]
        ]
        logger.info(f"web API notification source runs poll: {len(items)} terminal runs")
        return {"items": items}

    # ── issue #138 G10: space knowledge layer — space-level instructions (system-prompt injection) ──
    @app.get("/api/space/spaces/{space_id}/instructions")
    async def instructions_get(space_id: str):
        if space_store is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "unavailable", "message": "空间存储不可用"},  # # i18n: user-facing
            )  # i18n: user-facing
        sp = await space_store.get_space(space_id)
        if not sp:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "空间不存在"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"space_id": space_id, "instructions": getattr(sp.config, "instructions", "") or ""}

    @app.put("/api/space/spaces/{space_id}/instructions")
    async def instructions_put(space_id: str, body: Dict[str, Any]):
        if space_store is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "unavailable", "message": "空间存储不可用"},  # # i18n: user-facing
            )  # i18n: user-facing
        sp = await space_store.get_space(space_id)
        if not sp:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "空间不存在"},  # # i18n: user-facing
            )  # i18n: user-facing
        existing = sp.config.to_dict()
        existing["instructions"] = str(body.get("instructions") or "")
        await space_store.update_space(space_id, config=existing)
        logger.info(f"web API space instructions updated: {space_id} ({len(existing['instructions'])} chars)")
        return {"space_id": space_id, "instructions": existing["instructions"]}

    # ── issue #262: signed webhook callback — external systems approve/resume runs waiting on approval gates ──

    async def _apply_rejected_item_states(repo: str, rejected: List[Dict[str, Any]], findings: str) -> None:
        # issue #309: per-item rejection writeback to GitHub — state:blocked label + reason comment
        # (best-effort, any failure only WARNs and never blocks the resume); reuses the
        # GITHUB_TOKEN env var, same source as the github_api node
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            logger.warning("batch rejection writeback skipped: GITHUB_TOKEN not configured")
            return
        import httpx

        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        ) as client:
            for item in rejected:
                issue_number = item.get("task_issue")
                reason = str(item.get("reason") or "被验收闸门拒绝")  # i18n: user-facing
                body_text = f"G4 验收拒绝: {reason}" + (
                    f"\n\n审查意见:\n{findings}" if findings else ""  # # i18n: user-facing
                )  # i18n: user-facing
                try:
                    await client.request(
                        "PUT", f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": ["state:blocked"]}
                    )
                    await client.request(
                        "POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body_text}
                    )
                    logger.info(f"batch rejection written back: {repo}#{issue_number} state:blocked")
                except Exception as e:
                    logger.warning(f"batch rejection writeback failed (ignored): {repo}#{issue_number}: {e}")

    @app.post("/api/hooks/webhook/{run_id}")
    async def hooks_webhook(run_id: str, request: Request):
        secret = os.environ.get("FUSION_COWORK_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "unavailable",
                    "message": "FUSION_COWORK_WEBHOOK_SECRET 未配置, webhook 回调不可用",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        raw = await request.body()
        provided = request.headers.get("X-Fusion-Signature", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not provided or not hmac.compare_digest(provided, expected):
            logger.warning(f"webhook callback signature check failed: run={run_id}")
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "HMAC 签名无效"},  # # i18n: user-facing
            )  # i18n: user-facing
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON"},  # # i18n: user-facing
            ) from exc  # i18n: user-facing
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "请求体须为 JSON 对象"},  # # i18n: user-facing
            )  # i18n: user-facing
        if permission_manager is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "unavailable", "message": "权限管理器不可用"},  # # i18n: user-facing
            )  # i18n: user-facing
        finder = getattr(permission_manager, "find_pending_by_execution", None)
        pending = finder(run_id) if callable(finder) else None
        if not pending:
            # issue #279: the sender may register ONE static webhook URL with a
            # placeholder run id — fall back to the run id carried in the decision
            # body (params.run_id or top-level run_id) and route by that.
            body_params = payload.get("params")
            body_run = ""
            if isinstance(body_params, dict):
                body_run = str(body_params.get("run_id") or "")
            body_run = body_run or str(payload.get("run_id") or "")
            if body_run and body_run != run_id:
                pending = finder(body_run) if callable(finder) else None
                if pending:
                    logger.info(f"webhook run id resolved from body: path={run_id} body={body_run}")
                    run_id = body_run
        if not pending:
            return {
                "run_id": run_id,
                "resumed": False,
                "reason": "该 run 无待审批项 (已处理或不在等待)",  # # i18n: user-facing
            }  # i18n: user-facing
        approved = bool(payload.get("approved"))
        action_id = pending.get("action_id", "")
        edited = payload.get("params")
        edited = edited if isinstance(edited, dict) and edited else None
        # issue #309: per-item decision items[] — full-approve/mixed both resume (partial
        # completion is trackable), all-reject follows the reject path;
        # rejected items write back GitHub state:blocked (best-effort, when payload.repo provided)
        batch: List[Dict[str, Any]] = []
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            batch = [dict(x) for x in raw_items if isinstance(x, dict) and "task_issue" in x]
        if batch:
            any_approved = any(bool(x.get("approved")) for x in batch)
            record = runs.get(run_id) or {}
            verdicts = list(record.get("batch_verdicts") or [])
            verdicts.append(
                {
                    "approval_id": str(payload.get("approval_id") or action_id),
                    "decided_by": str(payload.get("decided_by") or "webhook"),
                    "decided_at": str(payload.get("decided_at") or ""),
                    "items": batch,
                }
            )
            record["batch_verdicts"] = verdicts
            approved = any_approved
            rejected = [x for x in batch if not x.get("approved")]
            logger.info(
                f"webhook per-item decision: run={run_id} items={len(batch)} approved={len(batch) - len(rejected)} rejected={len(rejected)}"
            )
            if rejected and str(payload.get("repo") or ""):
                await _apply_rejected_item_states(
                    str(payload.get("repo")),
                    rejected,
                    str(payload.get("findings") or ""),
                )
        # issue #303: decision attribution lands on the run timeline via the webhook (who approved)
        attribution = {
            "approval_id": str(payload.get("approval_id") or action_id),
            "decided_by": str(payload.get("decided_by") or "webhook"),
            "decided_at": str(payload.get("decided_at") or ""),
        }
        releaser = getattr(permission_manager, "release_pending_once", None)
        released = releaser(action_id) if callable(releaser) else False
        resolver = getattr(engine, "resolve_hitl", None)
        resumed = False
        if released and callable(resolver):
            try:
                resumed = bool(
                    resolver(run_id, approved, once=True, params=edited, attribution=attribution, batch=batch or None)
                )
            except TypeError:
                try:
                    resumed = bool(resolver(run_id, approved, once=True, params=edited, attribution=attribution))
                except TypeError:
                    try:
                        resumed = bool(resolver(run_id, approved, once=True, params=edited))
                    except TypeError:
                        resumed = bool(resolver(run_id, approved, once=True))
        logger.info(f"webhook callback resume: run={run_id} approved={approved} resumed={resumed}")
        _schedule_hitl_audit(app, run_id, action_id, approved, "webhook")
        return {
            "run_id": run_id,
            "resumed": resumed,
            "approved": approved,
            **({"items": len(batch)} if batch else {}),
        }

    # ── issue #278: voice panel — fusion-speech WS ingress config + health proxy ──
    @app.get("/api/speech/config")
    async def speech_config():
        port = os.environ.get("FUSION_SPEECH_PORT", "11465")
        url = os.environ.get("FUSION_SPEECH_WS_URL", f"ws://127.0.0.1:{port}/stream/audio")
        token = os.environ.get("FUSION_SPEECH_INGRESS_TOKEN", "")
        return {"url": url, "token": token, "ingress_enabled": True}

    @app.get("/api/speech/health")
    async def speech_health():
        import httpx

        port = os.environ.get("FUSION_SPEECH_PORT", "11465")
        base = os.environ.get("FUSION_SPEECH_URL", f"http://127.0.0.1:{port}")
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{base}/health")
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning(f"speech daemon health probe failed: {e}")
            return {"status": "down", "error": str(e)}

    # ── Web capability completion: permission rules / benchmark / system / sessions / db ops ──
    @app.get("/api/permissions/rules")
    async def permissions_rules():
        if permission_manager is None:
            raise HTTPException(
                status_code=503, detail={"code": "unavailable", "message": "Permission manager unavailable"}
            )
        snapshot = permission_manager.to_dict()
        return {"level": snapshot.get("level", ""), "rules": snapshot.get("rules", [])}

    @app.post("/api/permissions/rules", status_code=201)
    async def permissions_rules_create(body: Dict[str, Any]):
        if permission_manager is None:
            raise HTTPException(
                status_code=503, detail={"code": "unavailable", "message": "Permission manager unavailable"}
            )
        tool_name = str(body.get("tool_name") or "").strip()
        if not tool_name:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "tool_name is required"})
        allowed = bool(body.get("allowed", True))
        scope = str(body.get("scope") or "*")
        if allowed:
            permission_manager.approve(tool_name, scope=scope)
        else:
            permission_manager.deny(tool_name, scope=scope)
        permission_manager.save()
        logger.info(f"Permission rule set: {tool_name} allowed={allowed} scope={scope}")
        return {"tool_name": tool_name, "allowed": allowed, "scope": scope}

    @app.delete("/api/permissions/rules/{tool_name}")
    async def permissions_rules_delete(tool_name: str):
        if permission_manager is None:
            raise HTTPException(
                status_code=503, detail={"code": "unavailable", "message": "Permission manager unavailable"}
            )
        removed = permission_manager.reset(tool_name)
        permission_manager.save()
        logger.info(f"Permission rules cleared: {tool_name} ({removed})")
        return {"tool_name": tool_name, "removed": removed}

    @app.put("/api/permissions/level")
    async def permissions_level_set(body: Dict[str, Any]):
        if permission_manager is None:
            raise HTTPException(
                status_code=503, detail={"code": "unavailable", "message": "Permission manager unavailable"}
            )
        from ..engine.permission import PermissionLevel

        raw = str(body.get("level") or "").strip().lower()
        try:
            permission_manager.level = PermissionLevel(raw)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "bad_request",
                    "message": f"Unknown permission level: {raw} (valid: {[lv.value for lv in PermissionLevel]})",
                },
            ) from e
        logger.info(f"Permission level switched: {raw}")
        return {"level": raw}

    _bench_state: Dict[str, Any] = {"running": False, "results": [], "summary": {}, "error": ""}

    @app.post("/api/benchmark/run")
    async def benchmark_run(body: Dict[str, Any]):
        from ..benchmark import BenchmarkRunner
        from ..engine.node import NodeRegistry
        from ..nodes import import_all_nodes

        import_all_nodes()
        specs = body.get("specs")
        if not isinstance(specs, list) or not specs:
            raise HTTPException(
                status_code=400, detail={"code": "bad_request", "message": "specs is required (list of node+params)"}
            )
        for spec in specs:
            node_name = str((spec or {}).get("node") or "").strip()
            if not node_name or NodeRegistry.get(node_name) is None:
                raise HTTPException(
                    status_code=400, detail={"code": "bad_request", "message": f"Unknown node: {node_name}"}
                )
        repeats = max(1, min(int(body.get("repeats") or 3), 10))
        if _bench_state.get("running"):
            raise HTTPException(status_code=409, detail={"code": "busy", "message": "Benchmark already running"})

        async def _run() -> None:
            _bench_state.update({"running": True, "results": [], "summary": {}, "error": ""})
            try:
                runner = BenchmarkRunner(repeats=repeats)
                res = await runner.run_nodes([{"node": s["node"], "params": s.get("params") or {}} for s in specs])
                _bench_state.update(
                    {"results": [r.to_dict() for r in res], "summary": runner.summary() or {"total": len(res)}}
                )
                logger.info(f"Benchmark done: {len(specs)} specs x {repeats} repeats")
            except Exception as e:
                _bench_state["error"] = str(e)
                logger.error(f"Benchmark failed: {e}")
            finally:
                _bench_state["running"] = False

        if body.get("wait"):
            await _run()
            return {"started": True, "finished": True, "specs": len(specs), "repeats": repeats}
        asyncio.create_task(_run())
        return {"started": True, "specs": len(specs), "repeats": repeats}

    @app.get("/api/benchmark/status")
    async def benchmark_status():
        return {
            "running": _bench_state.get("running", False),
            "results": _bench_state.get("results", []),
            "summary": _bench_state.get("summary", {}),
            "error": _bench_state.get("error", ""),
        }

    @app.get("/api/system/info")
    async def system_info():
        import platform as _platform
        import shutil as _shutil
        import sys as _sys

        from .. import __version__
        from ..engine.node import NodeRegistry
        from ..nodes import import_all_nodes

        import_all_nodes()
        home = Path.home()
        try:
            disk = _shutil.disk_usage(home)
            disk_free_gb = round(disk.free / 1024**3, 1)
            disk_total_gb = round(disk.total / 1024**3, 1)
        except OSError:
            disk_free_gb = disk_total_gb = None
        return {
            "version": __version__,
            "python": _sys.version.split()[0],
            "platform": _platform.platform(),
            "node_count": len(NodeRegistry.list()),
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "pid": os.getpid(),
        }

    @app.post("/api/system/clean")
    async def system_clean(body: Dict[str, Any]):
        from ..nodes import import_all_nodes
        from ..nodes.macos import DesktopCleanNode, DownloadOrganizerNode

        import_all_nodes()
        dry_run = bool(body.get("dry_run", True))
        from ..engine.node import NodeConfig

        outputs = {}
        for label, cls, extra in [
            ("desktop", DesktopCleanNode, {"organize_by_type": True, "skip_hidden": True}),
            ("downloads", DownloadOrganizerNode, {}),
        ]:
            params = {**extra, "dry_run": dry_run}
            node = cls(config=NodeConfig(params=params))
            result = await node.execute({})
            outputs[label] = getattr(result, "summary", str(result))
        logger.info(f"System clean (dry_run={dry_run}) done: {list(outputs)}")
        return {"dry_run": dry_run, "outputs": outputs}

    @app.post("/api/sessions/{session_id}/fork")
    async def session_fork(session_id: str, body: Dict[str, Any]):
        if session_store is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "Session store unavailable"})
        from_step = int(body.get("from_step") or 0)
        forked = session_store.fork(session_id, from_step=from_step)
        if not forked:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": f"Session not found: {session_id}"}
            )
        logger.info(f"Session forked: {session_id} from_step={from_step}")
        return {"session_id": getattr(forked, "id", "")}

    @app.post("/api/sessions/cleanup")
    async def session_cleanup(body: Dict[str, Any]):
        if session_store is None:
            raise HTTPException(status_code=503, detail={"code": "unavailable", "message": "Session store unavailable"})
        days = int(body.get("days") or 30)
        removed = session_store.cleanup_expired(expire_days=days)
        logger.info(f"Expired sessions cleaned: {removed} (days={days})")
        return {"removed": removed, "days": days}

    @app.post("/api/admin/db/backup")
    async def admin_db_backup(body: Dict[str, Any]):
        from ..db.backup import BackupManager

        dsn = str(body.get("dsn") or os.environ.get("DATABASE_URL", "") or os.environ.get("FUSION_PG_DSN", ""))
        if not dsn:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "DSN not configured (body.dsn or DATABASE_URL)"},
            )
        dest = str(body.get("dest") or "")
        manager = BackupManager(dsn)
        if not dest:
            from datetime import datetime

            dest = f"fusion-cowork-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sql.gz"
        try:
            path = await asyncio.get_event_loop().run_in_executor(None, lambda: manager.backup(dest))
        except Exception as e:
            logger.error(f"DB backup failed: {e}")
            raise HTTPException(status_code=500, detail={"code": "backup_failed", "message": str(e)}) from e
        logger.info(f"DB backup done: {path}")
        return {"path": str(path)}

    # ── W19 guard L3 approval queue (migration doc §6 P3; guard + local CONFIRM merged since #124) ──
    @app.get("/api/permissions/pending")
    async def permissions_pending():
        if permission_manager is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "unavailable", "message": "权限管理器不可用"},  # # i18n: user-facing
            )  # i18n: user-facing
        list_merged = getattr(permission_manager, "list_pending", None)
        approvals = list_merged() if callable(list_merged) else permission_manager.list_pending_guard()
        # issue #463: durable HITL — merge approval-journal pending rows as fallback
        # (still visible if the queue is lost, source=journal)
        # issue #497: journal row hygiene — rows with an empty run_id (never journaled
        # before a crash, can never be woken/resolved) and rows whose run is in a
        # terminal state no longer surface in the pending list (they pollute the
        # approval inbox + cause cross-run filter false positives); rows of active
        # runs keep the normal fallback visibility. issue #572: rows whose run
        # record is gone entirely (restart with lost in-memory store / evicted
        # history) are dropped too — they can neither be woken nor resolved, and
        # lingering forever only pollutes the inbox.
        journal = getattr(engine, "_approval_journal", None)
        if journal is not None:
            try:
                seen = {str(a.get("action_id")) for a in approvals if isinstance(a, dict)}
                active_statuses = set(getattr(runs, "_ACTIVE_RUN_STATUSES", ())) or {"running", "pending", "paused"}
                dropped = 0
                for row in journal.pending():
                    aid = str(row.get("action_id") or "")
                    if not aid or aid in seen:
                        continue
                    rid = str(row.get("run_id") or "")
                    if not rid:
                        dropped += 1
                        continue
                    rec = runs.get(rid)
                    # issue #572: run record gone entirely (restart with lost
                    # store, evicted history) — the approval can never be woken
                    # or resolved, so the row must not surface. Records are
                    # durable since #399; absence means the run is unrecoverable.
                    if rec is None or rec.get("status") not in active_statuses:
                        dropped += 1
                        continue
                    approvals.append(
                        {
                            "action_id": aid,
                            "tool_name": str(row.get("tool_name") or ""),
                            "source": "journal",
                            "params": json.loads(row.get("params") or "{}")
                            if isinstance(row.get("params"), str)
                            else (row.get("params") or {}),
                            "ts": float(row.get("ts") or 0),
                            "workflow": str(row.get("workflow") or ""),
                            "node_id": str(row.get("node_id") or ""),
                            "execution_id": rid,
                        }
                    )
                if dropped:
                    logger.info(
                        f"approval journal pending hygiene: {dropped} stale rows excluded from pending list (issue #497)"
                    )
            except Exception as e:
                logger.warning(f"approval journal pending merge failed (ignored): {e}")
        return {"approvals": approvals}

    @app.post("/api/permissions/pending/{action_id}/confirm")
    async def permissions_confirm(action_id: str, body: Dict[str, Any]):
        if permission_manager is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "unavailable", "message": "权限管理器不可用"},  # # i18n: user-facing
            )  # i18n: user-facing
        # issue #287: confirm semantics — {"once": true} without approved is treated as
        # approve-once ("Approve once" card semantics), no longer defaulting to a
        # deny-resume; an explicit approved=false still takes the once-deny path (issue #216).
        approved = bool(body.get("approved", bool(body.get("once"))))
        # issue #328: rejection review comment — passed to the engine with the decision,
        # merged into produced-node inputs when the revision loop re-runs
        reject_comment = str(body.get("comment") or body.get("comments") or "").strip() or None
        # issue #142: block-level review — accepted_blocks selects accepted change blocks,
        # rebuilding the narrowed edit params
        narrowed = None
        if approved and isinstance(body.get("accepted_blocks"), list):
            narrowed = _narrow_pending_edit(permission_manager, action_id, body["accepted_blocks"])
            if narrowed is _EDIT_ALL_REJECTED:
                logger.info(f"block-level review: {action_id} all change blocks rejected, no approval rule saved")
                return {
                    "action_id": action_id,
                    "approved": True,
                    "confirmed": False,
                    "reason": "所有变更块均被拒绝, 无可批准内容",  # i18n: user-facing
                }
        # issue #181: read pending details before confirm — local pending entries carry an
        # execution_id; after confirm saves the rule, the HITL-paused run is woken and
        # resumes in place (guard-side entries have no execution_id, unaffected)
        hitl_exec = None
        details_getter = getattr(permission_manager, "get_pending_details", None)
        if callable(details_getter):
            hitl_exec = (details_getter(action_id) or {}).get("execution_id") or None
        # issue #187: "Approve once" — dequeue only this pending entry, persist no rule,
        # wake the paused run directly
        # issue #508: reply — structured human reply, merged into the gated node input on
        # approval (downstream receives it via the gate output passthrough)
        reply = body.get("reply") if isinstance(body.get("reply"), dict) else None
        if body.get("once") and hitl_exec:
            releaser = getattr(permission_manager, "release_pending_once", None)
            released = releaser(action_id) if callable(releaser) else False
            if released and approved:
                resolver = getattr(engine, "resolve_hitl", None)
                if callable(resolver):
                    # issue #225: approval-card "edit params" — edited_params merged into the
                    # gated node input on approval
                    edited = body.get("edited_params")
                    edited = edited if isinstance(edited, dict) and edited else None
                    # issue #303: web approval-card decisions record attribution too
                    attribution = {
                        "approval_id": action_id,
                        "decided_by": "web",
                        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    try:
                        resolver(hitl_exec, True, once=True, params=edited, attribution=attribution, reply=reply)
                    except TypeError:
                        try:
                            resolver(hitl_exec, True, once=True, params=edited, attribution=attribution)
                        except TypeError:
                            try:
                                resolver(hitl_exec, True, once=True)
                            except TypeError:
                                # back-compat with engines lacking the once param — no rule
                                # saved, the re-check will deny but the run is woken
                                resolver(hitl_exec, True)
                logger.info(f"HITL once-approve woke run: {hitl_exec} (action_id={action_id}, no rule saved)")
                _schedule_hitl_audit(app, hitl_exec, action_id, True, "web")
                return {"action_id": action_id, "approved": True, "confirmed": True, "once": True}
            if released and not approved:
                # issue #216: once-deny must wake the run too (the deny path has no
                # once-grant and saves no rule), otherwise after the card is dequeued
                # confirm_guard finds no entry and returns False, leaving the run to
                # stall until the HITL timeout auto-denies
                resolver = getattr(engine, "resolve_hitl", None)
                if callable(resolver):
                    try:
                        resolver(
                            hitl_exec,
                            False,
                            attribution={
                                "approval_id": action_id,
                                "decided_by": "web",
                                "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            },
                            comments=reject_comment,
                        )
                    except TypeError:
                        resolver(hitl_exec, False)
                logger.info(f"HITL once-deny woke run: {hitl_exec} (action_id={action_id}, no rule saved)")
                _schedule_hitl_audit(app, hitl_exec, action_id, False, "web")
                return {"action_id": action_id, "approved": False, "confirmed": True, "once": True}
        confirmed = await permission_manager.confirm_guard(
            action_id, approved, approved_by="web", scope=str(body.get("scope") or "*")
        )
        if confirmed and hitl_exec:
            resolver = getattr(engine, "resolve_hitl", None)
            if callable(resolver):
                try:
                    resolver(
                        hitl_exec,
                        approved,
                        attribution={
                            "approval_id": action_id,
                            "decided_by": "web",
                            "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        },
                        comments=None if approved else reject_comment,
                        reply=reply if approved else None,
                    )
                except TypeError:
                    resolver(hitl_exec, approved)
                logger.info(f"HITL confirm woke run: {hitl_exec} approved={approved}")
                _schedule_hitl_audit(app, hitl_exec, action_id, approved, "web")
        # issue #138: persist immediately after confirm saves the rule (survives restart)
        try:
            saver = getattr(permission_manager, "save", None)
            if confirmed and callable(saver):
                saver()
        except Exception as e:
            logger.warning(f"web API permission rule save failed: {e}")
        resp: Dict[str, Any] = {"action_id": action_id, "approved": approved, "confirmed": confirmed}
        if narrowed is not None:
            resp["edited_params"] = narrowed
        return resp

    # ── W21 cluster panel (migration doc §6 P3, FUSION_CLUSTER_ENABLED opt-in) ──
    @app.get("/api/cluster/state")
    async def cluster_state():
        from ..distributed_state import get_cluster_state_store

        cluster_store = get_cluster_state_store()
        if cluster_store is None:
            return {
                "cluster_enabled": False,
                "nodes": [],
                "vram": {},
                "total_allocated_mb": 0,
            }
        nodes = [n.to_dict() for n in cluster_store.list_all_nodes()]
        return {
            "cluster_enabled": True,
            "nodes": nodes,
            "vram": cluster_store.cluster_vram_usage(),
            "total_allocated_mb": cluster_store.total_vram_allocated_mb(),
        }

    # ── W18 usage/quota indicator — space-domain counts + QuotaEnforcer quotas (migration doc §6 P3) ──
    @app.get("/api/usage")
    async def usage_overview():
        quotas_usage = {
            "spaces": 0,
            "messages": 0,
            "artifacts": 0,
            "agents": 0,
        }
        tenant_id = "default"
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            from ..tenant import get_current_tenant

            tenant_id = get_current_tenant() or "default"
            try:
                row = await space_store._fetchone("SELECT COUNT(*) AS c FROM spaces WHERE tenant_id = ?", (tenant_id,))
                quotas_usage["spaces"] = int(row["c"] or 0)
                row = await space_store._fetchone(
                    "SELECT COUNT(*) AS c FROM space_messages WHERE tenant_id = ?", (tenant_id,)
                )
                quotas_usage["messages"] = int(row["c"] or 0)
                row = await space_store._fetchone(
                    "SELECT COUNT(*) AS c FROM space_artifacts WHERE tenant_id = ?", (tenant_id,)
                )
                quotas_usage["artifacts"] = int(row["c"] or 0)
                row = await space_store._fetchone(
                    "SELECT COUNT(*) AS c FROM space_agents WHERE tenant_id = ?", (tenant_id,)
                )
                quotas_usage["agents"] = int(row["c"] or 0)
            except Exception as e:
                logger.warning(f"usage counting failed (returning zeros): {e}")
        from ..security.quotas import get_default_quota_enforcer

        try:
            quotas = get_default_quota_enforcer()._load_quotas(tenant_id)
            quota_fields = {
                "max_spaces": quotas.max_spaces,
                "max_messages_per_space": quotas.max_messages_per_space,
                "max_artifacts_per_space": quotas.max_artifacts_per_space,
                "max_agents_per_space": quotas.max_agents_per_space,
                "max_storage_mb": quotas.max_storage_mb,
            }
        except Exception as e:
            logger.warning(f"quota read failed (returning unlimited): {e}")
            quota_fields = {
                "max_spaces": -1,
                "max_messages_per_space": -1,
                "max_artifacts_per_space": -1,
                "max_agents_per_space": -1,
                "max_storage_mb": -1,
            }
        # issue #461: token/spend attribution — aggregated by run and workflow (in-process registry + restart summary rows)
        token_usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "spend_usd": 0.0, "runs": 0}
        by_workflow: Dict[str, Dict[str, Any]] = {}
        for r in runs.list():
            t = r.get("tokens")
            if not isinstance(t, dict) and r.get("steps"):
                t = _token_rollup(r["steps"])
            if not isinstance(t, dict):
                continue
            total = int(t.get("total_tokens", 0) or 0)
            if total <= 0:
                continue
            token_usage["total_tokens"] += total
            token_usage["prompt_tokens"] += int(t.get("prompt_tokens", 0) or 0)
            token_usage["completion_tokens"] += int(t.get("completion_tokens", 0) or 0)
            token_usage["runs"] += 1
            spend = float(r.get("spend_usd") if r.get("spend_usd") is not None else _spend_usd(total))
            token_usage["spend_usd"] = round(token_usage["spend_usd"] + spend, 6)
            wf_name = str(r.get("workflow_name") or r.get("workflow_id") or "-")
            agg = by_workflow.setdefault(wf_name, {"runs": 0, "total_tokens": 0, "spend_usd": 0.0})
            agg["runs"] += 1
            agg["total_tokens"] += total
            agg["spend_usd"] = round(agg["spend_usd"] + spend, 6)
        return {
            "tenant_id": tenant_id,
            "usage": quotas_usage,
            "quotas": quota_fields,
            "unlimited": all(v < 0 for v in quota_fields.values()),
            "token_usage": token_usage,
            "token_by_workflow": by_workflow,
        }

    # ── W22 mobile read-only overview + deliver to desktop (remote/sync foundation, cross_sync injection seam) ──
    @app.get("/api/mobile/overview")
    async def mobile_overview():
        recent = runs.list()[:5]
        return {
            "runs": [
                {
                    "run_id": r["run_id"],
                    "workflow_name": r["workflow_name"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "finished_at": r.get("finished_at"),
                    "error": r.get("error"),
                }
                for r in recent
            ],
            "pending_approvals": getattr(permission_manager, "list_pending", permission_manager.list_pending_guard)(),
            "generated_at": time.time(),
        }

    @app.post("/api/deliver")
    async def deliver_to_desktop(request: Request):
        cross = getattr(app.state, "cross_sync", None)
        if cross is None:
            return JSONResponse({"error": "跨设备同步未启用 (cross_sync 未注入)"}, status_code=503)  # i18n: user-facing
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"/api/deliver JSON parse failed: {e}")
            return JSONResponse({"error": "invalid json"}, status_code=400)
        run_id = str(body.get("run_id") or "")
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"运行不存在: {run_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        receiver = str(body.get("target_device_id") or "broadcast")
        from .sync import SyncMessage

        msg = SyncMessage(
            msg_id=f"del_{uuid.uuid4().hex[:8]}",
            msg_type="cowork_deliver",
            sender="fusion-cowork-web",
            receiver=receiver,
            payload={
                "run_id": run_id,
                "workflow_name": record.get("workflow_name", ""),
                "status": record.get("status", ""),
                "deep_link": f"fusion-cowork://runs/{run_id}",
            },
        )
        try:
            delivered = await cross.send_message(msg)
        except Exception as e:
            logger.warning(f"/api/deliver send failed (honestly reporting not delivered): {e}")
            delivered = False
        return {"delivered": delivered, "run_id": run_id, "receiver": receiver}

    # ── issue #156: admin console — members / usage / audit export. FUSION_COWORK_ADMIN_KEY gate ──
    def _require_admin(request: Request) -> None:
        import os as _os

        expected = _os.environ.get("FUSION_COWORK_ADMIN_KEY", "")
        if not expected:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "admin_disabled",
                    "message": "管理台未启用 (未配置 FUSION_COWORK_ADMIN_KEY)",  # # i18n: user-facing
                },  # i18n: user-facing
            )
        provided = request.headers.get("x-admin-key", "")
        if provided != expected:
            logger.warning(f"admin API auth denied: {request.url.path}")
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "invalid admin key"})

    @app.get("/api/admin/members")
    async def admin_members(request: Request):
        _require_admin(request)
        members: List[Dict[str, Any]] = []
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            try:
                rows = await space_store._fetchall(
                    "SELECT space_id, user_id, role, display_name, joined_at, tenant_id FROM space_members ORDER BY tenant_id, space_id"
                )
                members = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"admin members query failed: {e}")
        from ..auth import is_identity_enabled

        return {
            "mode": "identity" if is_identity_enabled() else "local",
            "members": members,
            "count": len(members),
        }

    @app.get("/api/admin/usage")
    async def admin_usage(request: Request):
        _require_admin(request)
        totals = {"spaces": 0, "messages": 0, "artifacts": 0, "agents": 0}
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            for key, table in (
                ("spaces", "spaces"),
                ("messages", "space_messages"),
                ("artifacts", "space_artifacts"),
                ("agents", "space_agents"),
            ):
                try:
                    row = await space_store._fetchone(f"SELECT COUNT(*) AS c FROM {table}")
                    totals[key] = int(row["c"] or 0)
                except Exception as e:
                    logger.warning(f"admin usage count failed {table}: {e}")
        run_counts: Dict[str, int] = {}
        total_tokens = 0
        for record in runs._runs.values():
            status = str(record.get("status", ""))
            run_counts[status] = run_counts.get(status, 0) + 1
            for step in record.get("steps") or []:
                tok = step.get("tokens") or {}
                if isinstance(tok, dict):
                    total_tokens += int(tok.get("total_tokens") or 0)
        return {"spaces": totals, "runs": {"by_status": run_counts, "total": len(runs._runs)}, "tokens": total_tokens}

    @app.get("/api/admin/audit/export")
    async def admin_audit_export(request: Request, since: float = 0.0, limit: int = 500):
        _require_admin(request)
        lim = min(max(limit, 1), 2000)
        records: List[Dict[str, Any]] = []
        space_store = getattr(app.state, "space_store", None)
        if space_store is not None:
            try:
                rows = await space_store._fetchall(
                    "SELECT tenant_id, actor, action, resource, detail_hash, entry_hash, created_at "
                    "FROM audit_log ORDER BY created_at ASC, id ASC LIMIT ?",
                    (lim,),
                )
                for row in rows:
                    ts = _iso_to_epoch(str(row["created_at"] or ""))
                    if since and ts < since:
                        continue
                    records.append(
                        {
                            "time_unix_nano": ts * 1e9,
                            "severity": "INFO",
                            "body": f"audit {row['action']} {row['resource']}",
                            "attributes": {
                                "event.name": "audit",
                                "tenant_id": row["tenant_id"],
                                "actor": row["actor"],
                                "action": row["action"],
                                "resource": row["resource"],
                                "detail_hash": row["detail_hash"],
                                "entry_hash": row["entry_hash"],
                            },
                        }
                    )
            except Exception as e:
                logger.warning(f"admin audit query failed: {e}")
        for record in runs._runs.values():
            finished = record.get("finished_at")
            if not finished:
                continue
            if since and float(finished) < since:
                continue
            records.append(
                {
                    "time_unix_nano": float(finished) * 1e9,
                    "severity": "ERROR" if record.get("status") == "failed" else "INFO",
                    "body": f"run.finished {record.get('workflow_name', '')}",
                    "attributes": {
                        "event.name": "run.finished",
                        "run_id": record.get("run_id", ""),
                        "workflow_id": record.get("workflow_id", ""),
                        "status": record.get("status", ""),
                        "steps": len(record.get("steps") or []),
                        "error": record.get("error") or "",
                    },
                }
            )
        # chronological order — audit rows and run records share one log stream
        records.sort(key=lambda r: r["time_unix_nano"])
        return _otlp_logs(records[:lim])

    @app.post("/api/admin/push/test")
    async def admin_push_test(request: Request):
        _require_admin(request)
        egress = getattr(app.state, "push_egress", None)
        if egress is None or not getattr(egress, "enabled", False):
            return {
                "enabled": False,
                "delivered": False,
                "hint": "配置 FUSION_COWORK_PUSH_WEBHOOK_URL 后可用",  # # i18n: user-facing
            }  # i18n: user-facing
        delivered = await egress.push_event("test", {"source": "admin_console"})
        return {"enabled": True, "delivered": delivered}

    # ── issue #154: takeover WS — any authenticated device attaches to a running run, event stream + steering frames ──
    @app.websocket("/ws/takeover/{run_id}")
    async def takeover_ws(websocket: WebSocket, run_id: str):
        from fastapi import WebSocketDisconnect

        # browser WS cannot send custom headers — token goes via query param; no key configured = allow (#146 contract)
        from ..auth import verify_any_token

        if auth_configured():
            token = str(websocket.query_params.get("token", ""))
            if not any(verify_any_token(token, expected) is not None for expected in _accepted_keys()):
                await websocket.accept()
                await websocket.close(code=4401)
                return
        await websocket.accept()
        record = runs.get(run_id)
        if record is None:
            await websocket.send_json({"event": "error", "message": f"运行不存在: {run_id}"})  # i18n: user-facing
            await websocket.close(code=4404)
            return
        try:
            await _takeover_session(websocket, run_id, runs, permission_manager)
        except WebSocketDisconnect:
            logger.info(f"takeover WS disconnected: run={run_id}")
        return

    # ── W20 plugin panel — /rpc endpoint (rpc_bridge plugins/* delegation, -32603 when dependency missing) ──
    @app.post("/rpc")
    async def rpc_endpoint(request: Request):
        from .rpc_bridge import dispatch_rpc

        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"/rpc JSON parse failed: {e}")
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                status_code=400,
            )
        return JSONResponse(await dispatch_rpc(body))

    # ── W11 unified session list (migration doc §6 P1) ──
    # ── W11/A12 unified session list: engine session + space chat + scheduler history ──
    @app.get("/api/sessions")
    async def sessions_list(status: str = "", source: str = "", limit: int = 50):
        if source not in ("", "engine", "space", "scheduler"):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": "source 必须是 engine/space/scheduler"},  # i18n: user-facing
            )
        lim = min(max(limit, 1), 200)
        items: List[Dict[str, Any]] = []
        if source in ("", "engine"):
            for s in session_store.list_sessions(status=status or None, limit=lim):
                item = _serialize_session(s)
                item.update(
                    {
                        "source": "engine",
                        "title": s.workflow_name or s.id,
                        "space_id": "",
                        "space_name": "",
                        "href": "#/runs",
                    }
                )
                items.append(item)
        if source in ("", "space"):
            space_store = getattr(app.state, "space_store", None)
            if space_store is not None:
                from ..tenant import resolve_tenant_id

                tid = resolve_tenant_id(None)
                try:
                    # issue #496: live messages live on the kernel plane now —
                    # read the last one from the plane (default state dir is
                    # derived from the same store), legacy chat table as
                    # read-compat fallback for pre-migration rows.
                    from ..space.kernel_plane import SpaceKernelPlane

                    plane = SpaceKernelPlane(space_store)
                    for sp in await space_store.list_spaces(limit=lim):
                        last: Optional[Dict[str, Any]] = None
                        last_ts = 0.0
                        try:
                            recent = await plane.history(sp.id, limit=1)
                            if recent:
                                m0 = recent[-1]
                                last = {"content": m0.content, "role": m0.role, "created_at": m0.created_at}
                                last_ts = _iso_to_epoch(m0.created_at) or 0.0
                        except (ValueError, RuntimeError) as exc:
                            logger.debug(f"sessions plane read skipped space={sp.id}: {exc}")
                        if last is None:
                            last = await space_store._fetchone(
                                "SELECT content, role, created_at FROM space_messages "
                                "WHERE space_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT 1",
                                (sp.id, tid),
                            )
                            last_ts = _iso_to_epoch(last["created_at"]) if last else 0.0
                        created = _iso_to_epoch(sp.created_at)
                        item = {
                            "id": f"space_{sp.id}",
                            "workflow_id": "",
                            "workflow_name": sp.name,
                            "status": sp.status,
                            "initial_input": {},
                            "execution_id": "",
                            "created_at": created,
                            "updated_at": last_ts or _iso_to_epoch(sp.updated_at) or created,
                            "completed_at": None,
                            "metadata": {
                                "space_id": sp.id,
                                "last_message": (last["content"][:200] if last else ""),
                                "last_role": (last["role"] if last else ""),
                            },
                            "tenant_id": sp.tenant_id,
                            "source": "space",
                            "title": sp.name,
                            "space_id": sp.id,
                            "space_name": sp.name,
                            "href": "#/spaces",
                        }
                        items.append(item)
                except Exception as e:
                    logger.warning(f"space session merge failed (skipped): {e}")
        if source in ("", "scheduler") and scheduler is not None:
            try:
                for task in scheduler.list_tasks():
                    item = {
                        "id": f"sched_{task.id}",
                        "workflow_id": task.workflow_id,
                        "workflow_name": task.name,
                        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                        "initial_input": {},
                        "execution_id": "",
                        "created_at": task.created_at,
                        "updated_at": task.last_run or task.created_at,
                        "completed_at": None,
                        "metadata": {
                            "task_id": task.id,
                            "trigger_type": task.trigger_type,
                            "run_count": task.run_count,
                            "fail_count": task.fail_count,
                        },
                        "tenant_id": getattr(task, "tenant_id", "default"),
                        "source": "scheduler",
                        "title": task.name,
                        "space_id": "",
                        "space_name": "",
                        "href": "#/schedule",
                    }
                    items.append(item)
            except Exception as e:
                logger.warning(f"scheduler session merge failed (skipped): {e}")
        if status:
            items = [i for i in items if i.get("status") == status]
        items.sort(key=lambda i: i.get("updated_at") or i.get("created_at") or 0, reverse=True)
        items = items[:lim]
        return {"sessions": items, "count": len(items)}

    @app.get("/api/sessions/{session_id}")
    async def sessions_get(session_id: str):
        s = session_store.get(session_id)
        if not s:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"会话不存在: {session_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return _serialize_session(s)

    # ── issue #190: team messaging surface — roster / mailbox / consumer-pool lifecycle ──
    @app.get("/api/team/status")
    async def team_status():
        return app.state.runner_pool.status()

    @app.get("/api/team/roster")
    async def team_roster_list():
        items = app.state.team_roster.list()
        return {"agents": items, "count": len(items), "db": app.state.team_mailbox.path}

    @app.post("/api/team/roster", status_code=201)
    async def team_roster_register(request: Request):
        body = await request.json()
        try:
            row = app.state.team_roster.register(
                agent_id=str(body.get("agent_id") or ""),
                workflow_id=str(body.get("workflow_id") or ""),
                name=str(body.get("name") or ""),
                team=str(body.get("team") or ""),
                enabled=bool(body.get("enabled", True)),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_roster", "message": str(e)}) from e
        app.state.runner_pool.wake()
        return row

    @app.delete("/api/team/roster/{agent_id}")
    async def team_roster_remove(agent_id: str):
        if not app.state.team_roster.remove(agent_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"agent 未登记: {agent_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return {"removed": agent_id}

    @app.post("/api/team/send", status_code=201)
    async def team_send(request: Request):
        body = await request.json()
        try:
            message = app.state.team_mailbox.send(
                sender=str(body.get("sender") or ""),
                recipient=str(body.get("recipient") or ""),
                body=str(body.get("body") or ""),
                kind=str(body.get("kind") or "message"),
                team=str(body.get("team") or ""),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "invalid_message", "message": str(e)}) from e
        return message

    @app.get("/api/team/inbox")
    async def team_inbox(agent_id: str, unread: int = 1, limit: int = 100):
        if not agent_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_agent", "message": "agent_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        messages = app.state.team_mailbox.inbox(agent_id, unread_only=bool(unread), limit=limit)
        return {"agent_id": agent_id, "messages": messages, "count": len(messages)}

    @app.post("/api/team/notify")
    async def team_notify():
        # push path: the peer daemon calls this endpoint after writing the mailbox, so the
        # consumer pool wakes immediately (instead of waiting for the poll interval)
        app.state.runner_pool.wake()
        return {"notified": True, "running": app.state.runner_pool.running}

    @app.post("/api/team/pool/start")
    async def team_pool_start():
        await app.state.runner_pool.start()
        return {"running": app.state.runner_pool.running}

    @app.post("/api/team/pool/stop")
    async def team_pool_stop():
        await app.state.runner_pool.stop()
        return {"running": app.state.runner_pool.running}

    # ── issue #595: demand engine web API — leads / metrics / catalog / settings ──
    # LLM endpoints stay behind fusion-gateway :11432; this surface only reads
    # and updates the LeadStore + catalog + IMAP settings. Mail content is
    # untrusted data: only what LeadStore already persists is echoed back.

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _demand_store():
        from ..demand.store import LeadStore, default_db_path

        store = LeadStore(demand_db_path or default_db_path())
        await store.connect()
        try:
            yield store
        finally:
            await store.close()

    def _demand_catalog_file() -> str:
        from ..demand.catalog import runtime_catalog_path

        return demand_catalog_path or runtime_catalog_path()

    @app.get("/api/demand/leads")
    async def demand_leads(status: str = "", limit: int = 200):
        async with _demand_store() as store:
            leads = await store.list_leads(status=status.strip(), limit=max(1, min(int(limit), 500)))
        return {"leads": leads, "count": len(leads)}

    @app.post("/api/demand/leads", status_code=201)
    async def demand_lead_ingest(request: Request):
        # issue #605: direct-ingest channel (fusion-site#141 E1) — structured
        # intake form payloads, source="direct". Dedup on the existing
        # idx_leads_external unique index: duplicate POST returns the existing
        # lead with 200. Same spam filter + match stage as mailbox leads, but
        # zero LLM calls here (rule spam markers only, model stays behind the
        # gateway plane). The commerce path on fusion-site is fire-and-forget,
        # so this endpoint must never hang: no network, local DB only.
        import time as _time

        from ..demand.catalog import load_catalog, match_lead
        from ..demand.normalize import classify_rules

        body = await request.json()
        source = str(body.get("source") or "").strip().lower()
        external_id = str(body.get("external_id") or "").strip()[:64]
        title = str(body.get("title") or "").strip()[:120]
        description = str(body.get("description") or "").strip()[:4000]
        if source != "direct":
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_source", "message": "source 必须是 direct"},  # # i18n: user-facing
            )  # i18n: user-facing
        if not external_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_external_id", "message": "external_id 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        if not title or not description:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_body", "message": "title 与 description 必填"},  # i18n: user-facing
            )
        skills_raw = body.get("skills") or []
        skills = (
            [str(s).lower().strip()[:40] for s in skills_raw if str(s).strip()][:8]
            if isinstance(skills_raw, list)
            else []
        )
        client_meta_raw = body.get("client_meta") or {}
        client_meta = (
            {str(k)[:40]: str(v)[:200] for k, v in list(client_meta_raw.items())[:10]}
            if isinstance(client_meta_raw, dict)
            else {}
        )
        budget_hint = str(body.get("budget_hint") or "").strip()[:80]
        async with _demand_store() as store:
            async with store.db.execute(
                "SELECT * FROM leads WHERE source=? AND external_id=? AND external_id != '' ORDER BY id LIMIT 1",
                ("direct", external_id),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                from fastapi.responses import JSONResponse

                existing = store._lead_dict(row)
                logger.info(f"[demand] direct ingest dedup hit: external_id={external_id} -> lead #{existing['id']}")
                return JSONResponse(
                    status_code=200,
                    content={
                        "lead_id": existing["id"],
                        "status": existing["status"],
                        "matched_capabilities": existing.get("matched_capabilities", []),
                        "match_score": existing.get("match_score", 0),
                        "duplicate": True,
                    },
                )
            # spam filtering stays in the normalize stage: rule markers on the
            # description text; the direct channel keeps source="direct"
            verdict = classify_rules(title, "", description)
            status = "spam" if verdict.get("source") == "spam" else "normalized"
            lead_id = await store.insert_lead(
                {
                    "message_id": f"direct-{external_id}",
                    "source": "direct",
                    "external_id": external_id,
                    "title": title,
                    "description": description,
                    "budget_hint": budget_hint,
                    "skills": skills,
                    "client_meta": client_meta,
                    "arrived_at": _time.time(),
                    "status": status,
                    "extraction": "rules",
                }
            )
            response = {
                "lead_id": lead_id,
                "status": status,
                "matched_capabilities": [],
                "match_score": 0,
                "duplicate": False,
            }
            if status != "spam":
                try:
                    catalog = load_catalog(_demand_catalog_file())
                except (FileNotFoundError, ValueError):
                    catalog = load_catalog()  # in-repo seed fallback (same as GET /api/demand/catalog)
                lead = await store.get_lead(lead_id)
                result = match_lead(lead, catalog)
                lead_status = "parked" if result["parked"] else "matched"
                await store.update_lead(
                    lead_id,
                    {
                        "status": lead_status,
                        "matched_capabilities": result["matched_capabilities"],
                        "match_score": result["match_score"],
                        "match_reason": result["match_reason"],
                        "catalog_version": result["catalog_version"],
                    },
                )
                response.update(
                    {
                        "status": lead_status,
                        "matched_capabilities": result["matched_capabilities"],
                        "match_score": result["match_score"],
                    }
                )
        logger.info(f"[demand] direct ingested: lead #{lead_id} external_id={external_id} status={response['status']}")
        return response

    @app.get("/api/demand/leads/{lead_id}")
    async def demand_lead_detail(lead_id: int):
        async with _demand_store() as store:
            lead = await store.get_lead(lead_id)
        if lead is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"lead 不存在: {lead_id}"},  # # i18n: user-facing
            )  # i18n: user-facing
        return lead

    @app.post("/api/demand/leads/{lead_id}/approve")
    async def demand_lead_approve(lead_id: int, request: Request):
        body = await request.json()
        pick = str(body.get("pick") or "")
        if pick not in ("a", "b"):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_pick", "message": "pick 必须是 a 或 b"},  # # i18n: user-facing
            )  # i18n: user-facing
        edited = str(body.get("text") or "")
        async with _demand_store() as store:
            lead = await store.get_lead(lead_id)
            if lead is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"lead 不存在: {lead_id}"},  # # i18n: user-facing
                )  # i18n: user-facing
            if lead.get("status") != "drafted":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "invalid_status",
                        "message": f"仅 drafted 状态可审批, 当前: {lead.get('status')}",  # # i18n: user-facing
                    },  # i18n: user-facing
                )
            fields: Dict[str, Any] = {"status": "approved", "draft_pick": pick}
            if edited:
                import difflib

                base = lead.get(f"draft_{pick}") or ""
                fields["draft_final"] = edited
                ratio = difflib.SequenceMatcher(None, base, edited).ratio()
                fields["draft_edit_distance"] = round(1.0 - ratio, 4)
            await store.update_lead(lead_id, fields)
            updated = await store.get_lead(lead_id)
        logger.info(f"[demand] lead {lead_id} approved pick={pick} edited={bool(edited)}")
        return updated

    @app.post("/api/demand/leads/{lead_id}/discard")
    async def demand_lead_discard(lead_id: int, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = str(body.get("reason") or "")
        async with _demand_store() as store:
            lead = await store.get_lead(lead_id)
            if lead is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"lead 不存在: {lead_id}"},  # # i18n: user-facing
                )  # i18n: user-facing
            await store.update_lead(lead_id, {"status": "discarded", "discard_reason": reason})
            updated = await store.get_lead(lead_id)
        logger.info(f"[demand] lead {lead_id} discarded reason={reason[:80]}")
        return updated

    @app.get("/api/demand/metrics")
    async def demand_metrics():
        async with _demand_store() as store:
            return await store.metrics()

    @app.get("/api/demand/catalog")
    async def demand_catalog_get():
        from ..demand.catalog import load_catalog

        path = _demand_catalog_file()
        if not Path(path).exists():
            path = ""  # fall back to the bundled seed
        return load_catalog(path)

    @app.put("/api/demand/catalog")
    async def demand_catalog_put(request: Request):
        from ..demand.catalog import load_catalog, match_lead, save_catalog

        body = await request.json()
        capabilities = body.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_catalog", "message": "capabilities 必须是非空列表"},  # i18n: user-facing
            )
        if not str(body.get("version") or "").strip():
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_catalog", "message": "version 必填"},  # # i18n: user-facing
            )  # i18n: user-facing
        old_version = ""
        try:
            old_version = str(
                load_catalog(_demand_catalog_file() if Path(_demand_catalog_file()).exists() else "").get("version", "")
            )
        except Exception:
            pass
        version = str(body["version"]).strip()
        if version == old_version:
            # issue #595: every save must move the version — auto-bump on repeat
            parts = version.rsplit(".", 1)
            version = f"{parts[0]}.{int(parts[1]) + 1}" if len(parts) == 2 and parts[1].isdigit() else f"{version}.1"
        data = {**body, "version": version}
        save_catalog(data, _demand_catalog_file())
        # re-score parked leads against the new version
        threshold = float(body.get("threshold", 3.0))
        re_scored = 0
        async with _demand_store() as store:
            for lead in await store.list_leads(status="parked", limit=500):
                result = match_lead(lead, data, threshold=threshold)
                await store.update_lead(
                    lead["id"],
                    {
                        "matched_capabilities": result["matched_capabilities"],
                        "match_score": result["match_score"],
                        "match_reason": result["match_reason"],
                        "catalog_version": result["catalog_version"],
                        "status": "parked" if result["parked"] else "matched",
                    },
                )
                re_scored += 1
        logger.info(f"[demand] catalog updated v{old_version} -> v{version}, parked re-scored={re_scored}")
        return {"version": version, "previous_version": old_version, "parked_re_scored": re_scored}

    @app.get("/api/demand/settings")
    async def demand_settings_get():
        from ..demand.settings import read_settings

        data = read_settings()
        async with _demand_store() as store:
            data["cursor"] = await store.get_cursor(f"uid:{data['folder'] or 'INBOX'}")
        return data  # password never included — presence flag only

    @app.put("/api/demand/settings")
    async def demand_settings_put(request: Request):
        from ..demand.settings import write_settings

        body = await request.json()
        port = body.get("port")
        try:
            port = int(port) if port else 0
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_port", "message": "port 必须是整数"},  # i18n: user-facing
            ) from None
        path = write_settings(
            host=str(body.get("host") or ""),
            port=port,
            user=str(body.get("user") or ""),
            password=str(body.get("password") or ""),  # empty = keep stored; never logged
            folder=str(body.get("folder") or ""),
        )
        logger.info(f"[demand] settings updated: {path}")
        return {"saved": True, "env_path": str(path)}

    @app.post("/api/demand/settings/test")
    async def demand_settings_test():
        from ..demand.settings import load_env_file, test_imap

        load_env_file()
        return await test_imap()

    # ── issue #597: delivery rail — orders (S0-S7 state machine, human gates) ──
    # Read surface for the pipeline view + the ONLY path that advances an
    # order out of a human gate. The system drafts; the human approves here.

    from contextlib import asynccontextmanager as _alcm

    @_alcm
    async def _delivery_store():
        from ..delivery.store import OrderStore
        from ..delivery.store import default_db_path as delivery_default_db

        store = OrderStore(delivery_db_path or delivery_default_db())
        await store.connect()
        try:
            yield store
        finally:
            await store.close()

    @app.get("/api/delivery/orders")
    async def delivery_orders(stage: str = "", limit: int = 100):
        async with _delivery_store() as store:
            orders = await store.list_orders(stage=stage.strip(), limit=max(1, min(int(limit), 200)))
        return {"orders": orders, "count": len(orders)}

    @app.get("/api/delivery/orders/{order_no}")
    async def delivery_order_detail(order_no: str):
        async with _delivery_store() as store:
            order = await store.get_order(order_no)
            if order is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"order 不存在: {order_no}"},  # i18n: user-facing
                )
            verified = await store.audit_verify()
        order["audit_chain_ok"] = verified
        return order

    @app.post("/api/delivery/orders/{order_no}/approve")
    async def delivery_order_approve(order_no: str):
        # human gate advance — machine stages never skip; approve moves the
        # order to the next stage in the S0-S7 ladder
        async with _delivery_store() as store:
            order = await store.get_order(order_no)
            if order is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": f"order 不存在: {order_no}"},  # i18n: user-facing
                )
            updated = await store.advance_stage(order_no)
        logger.info(f"[delivery] order {order_no} approved: {order['stage']} -> {updated['stage']}")
        return updated

    # ── issue #609: invite token HTTP surface — peek + single-use consume ──
    # Called by the fusion-site BFF (service key) to resolve /invite/[token].
    # Token travels in the path but is stored/logged hashed or prefix-only.

    def _invite_log_tag(token: str) -> str:
        return f"{token[:6]}…" if len(token) > 6 else "…"

    def _invite_payload(invite: Dict[str, Any], consumed: bool = False) -> Dict[str, Any]:
        ttl_hours = max(0, round((invite["expires_at"] - invite["created_at"]) / 3600))
        from datetime import datetime

        return {
            "token_status": "active",
            "ttl_hours": ttl_hours,
            "expires_at": datetime.fromtimestamp(invite["expires_at"], tz=UTC).isoformat(),
            "order": {
                "order_no": invite["order_no"],
                "title": invite.get("title", ""),
                "stage": invite.get("stage", ""),
                "repo": invite.get("repo", ""),
            },
            **({"consumed": True} if consumed else {}),
        }

    @app.get("/api/delivery/invites/{token}")
    async def delivery_invite_peek(token: str):
        async with _delivery_store() as store:
            invite = await store.peek_invite(token)
        if invite is None:
            # unknown token — same 404 shape, no detail leaked
            raise HTTPException(status_code=404, detail={"code": "unknown_token", "message": "invite token not found"})
        if invite["used_at"] or invite["expires_at"] < time.time():
            # known but consumed/expired — indistinguishable from unknown to
            # the caller's happy path; explicit code kept for the site BFF
            logger.info(f"[delivery] invite peek unavailable: {_invite_log_tag(token)}")
            raise HTTPException(
                status_code=404, detail={"code": "token_unavailable", "message": "invite token not found"}
            )
        logger.info(f"[delivery] invite peek ok: {_invite_log_tag(token)} order={invite['order_no']}")
        return _invite_payload(invite)

    @app.post("/api/delivery/invites/{token}/consume")
    async def delivery_invite_consume(token: str):
        async with _delivery_store() as store:
            order_no = await store.consume_invite(token)
            if order_no is None:
                # unknown / expired / already-used all land here — consume is
                # atomic in the store, so a double-POST fails the second time
                logger.info(f"[delivery] invite consume rejected: {_invite_log_tag(token)}")
                raise HTTPException(
                    status_code=404, detail={"code": "token_unavailable", "message": "invite token not found"}
                )
            invite = await store.peek_invite(token)
        logger.info(f"[delivery] invite consumed: {_invite_log_tag(token)} order={order_no}")
        return _invite_payload(invite or {"order_no": order_no}, consumed=True)

    # ── P2 W12/W13 space collaboration surface — space-domain sub-app mount (SSE / WS / artifacts included) ──
    app.mount("/api/space", space_api)

    dist = Path(dist_dir) if dist_dir else _WEB_DIR / "dist"
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="spa")
        logger.info(f"web SPA mounted: {dist}")
    else:
        logger.info(f"web/dist not built, static hosting skipped: {dist}")

    return app


def _version() -> str:
    from .. import __version__

    return __version__


def _shell_registration_state() -> Dict[str, Any]:
    # issue #448: shell registration state for supervisor alerting. The
    # enabled probe never raises (prod may lack the admin key it requires).
    from .shell_registry import registration_health, shell_registration_enabled

    state = dict(registration_health())
    try:
        state["enabled"] = shell_registration_enabled()
    except Exception:
        state["enabled"] = False
    return state
