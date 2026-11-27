"""Workflow asset discipline (issue #516): versioning, machine-checkable
acceptance gates, independent audit trail, regression gate.

A workflow definition change is a code change and gets PR-level rigor:
- every save produces an immutable version (v1, v2, ...) with a canonical
  sha256 digest of the workflow body;
- a change may carry machine-checkable acceptance rules the engine evaluates
  at activation (node_count / required_nodes / forbidden_nodes /
  required_params); a failing gate blocks activation;
- an opt-in regression gate replays the new definition against recorded
  golden executions (per-node output digests) before activation — a modified
  workflow that fails the regression suite cannot become active;
- an audit trail records who changed what, when, with which digests and gate
  results, queryable separately from execution logs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ACCEPTANCE_RULE_TYPES = {"node_count", "required_nodes", "forbidden_nodes", "required_params"}


def canonical_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def evaluate_acceptance(rules: Any, workflow: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    # issue #516: machine-checkable acceptance — rules the engine can evaluate,
    # not prose. Unknown rule types fail closed (a gate the engine cannot
    # evaluate must not silently pass).
    results: List[Dict[str, Any]] = []
    if rules is None:
        return True, results
    if not isinstance(rules, list):
        return False, [{"type": "_malformed", "ok": False, "detail": "acceptance must be a list"}]
    nodes = workflow.get("nodes") or []
    node_names = [str(n.get("name") or "") for n in nodes if isinstance(n, dict)]
    for rule in rules:
        if not isinstance(rule, dict) or str(rule.get("type") or "") not in _ACCEPTANCE_RULE_TYPES:
            results.append(
                {
                    "type": str((rule or {}).get("type") if isinstance(rule, dict) else rule),
                    "ok": False,
                    "detail": "unknown acceptance rule type",
                }
            )
            continue
        rtype = str(rule["type"])
        ok, detail = True, ""
        if rtype == "node_count":
            cnt = len(nodes)
            lo, hi = rule.get("min"), rule.get("max")
            if isinstance(lo, int) and cnt < lo:
                ok, detail = False, f"node count {cnt} < min {lo}"
            elif isinstance(hi, int) and cnt > hi:
                ok, detail = False, f"node count {cnt} > max {hi}"
            else:
                detail = f"node count {cnt}"
        elif rtype == "required_nodes":
            missing = [n for n in (rule.get("names") or []) if str(n) not in node_names]
            ok, detail = not missing, ("missing nodes: " + ", ".join(map(str, missing)) if missing else "all present")
        elif rtype == "forbidden_nodes":
            present = [n for n in (rule.get("names") or []) if str(n) in node_names]
            ok, detail = (
                not present,
                ("forbidden present: " + ", ".join(map(str, present)) if present else "none present"),
            )
        elif rtype == "required_params":
            node_id = str(rule.get("node") or "")
            target = next((n for n in nodes if isinstance(n, dict) and str(n.get("id") or "") == node_id), None)
            if target is None:
                ok, detail = False, f"node '{node_id}' not found"
            else:
                params = (target.get("config") or {}).get("params") or {}
                missing = [p for p in (rule.get("params") or []) if str(p) not in params]
                ok, detail = (
                    not missing,
                    (
                        f"node '{node_id}' missing params: " + ", ".join(map(str, missing))
                        if missing
                        else "params present"
                    ),
                )
        results.append({"type": rtype, "ok": ok, "detail": detail})
    return all(r["ok"] for r in results), results


def _node_map(workflow: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(n.get("id") or ""): n for n in (workflow.get("nodes") or []) if isinstance(n, dict)}


def diff_versions(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    # issue #516: audit diff — what changed between two workflow bodies,
    # node-level so the audit query answers "what changed" without logs
    old_nodes, new_nodes = _node_map(old), _node_map(new)
    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))
    changed = []
    for nid in sorted(set(old_nodes) & set(new_nodes)):
        if canonical_digest(old_nodes[nid]) != canonical_digest(new_nodes[nid]):
            changed.append(nid)
    edges_old = old.get("edges") or []
    edges_new = new.get("edges") or []
    edges_added = [e for e in edges_new if e not in edges_old]
    edges_removed = [e for e in edges_old if e not in edges_new]
    return {
        "nodes_added": added,
        "nodes_removed": removed,
        "nodes_changed": changed,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "top_level_changed": sorted(
            k
            for k in set(old) | set(new)
            if k not in ("nodes", "edges") and canonical_digest(old.get(k)) != canonical_digest(new.get(k))
        ),
    }


class WorkflowAssetStore:
    """Versioned workflow assets: versions/, audit.jsonl, golden.json per asset id."""

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root) if root else Path.home() / ".fusion-cowork" / "web" / "assets"
        self._root.mkdir(parents=True, exist_ok=True)

    def _asset_dir(self, workflow_id: str) -> Path:
        safe = "".join(c for c in workflow_id if c.isalnum() or c in "-_")
        if not safe or safe != workflow_id:
            raise ValueError(f"非法工作流 id: {workflow_id}")  # i18n: user-facing
        d = self._root / safe
        (d / "versions").mkdir(parents=True, exist_ok=True)
        return d

    def _versions_dir(self, workflow_id: str) -> Path:
        return self._asset_dir(workflow_id) / "versions"

    def _audit_path(self, workflow_id: str) -> Path:
        return self._asset_dir(workflow_id) / "audit.jsonl"

    def _golden_path(self, workflow_id: str) -> Path:
        return self._asset_dir(workflow_id) / "golden.json"

    def _read_version(self, workflow_id: str, version: int) -> Optional[Dict[str, Any]]:
        p = self._versions_dir(workflow_id) / f"v{version}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"asset version read failed {workflow_id} v{version}: {e}")
            return None

    def current_version(self, workflow_id: str) -> int:
        nums = self.list_versions(workflow_id)
        active = [v["version"] for v in nums if v.get("status") == "active"]
        return max(active) if active else 0

    def list_versions(self, workflow_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        vdir = self._versions_dir(workflow_id)
        for p in sorted(vdir.glob("v*.json"), key=lambda x: int(x.stem[1:])):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "version": rec.get("version"),
                    "digest": rec.get("digest"),
                    "status": rec.get("status", "active"),
                    "actor": rec.get("actor", ""),
                    "created_at": rec.get("created_at"),
                    "gate_results": rec.get("gate_results", []),
                }
            )
        return out

    def get_version(self, workflow_id: str, version: int) -> Optional[Dict[str, Any]]:
        return self._read_version(workflow_id, version)

    def get_active(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        v = self.current_version(workflow_id)
        return self._read_version(workflow_id, v) if v else None

    def diff(self, workflow_id: str, v1: int, v2: int) -> Optional[Dict[str, Any]]:
        a, b = self._read_version(workflow_id, v1), self._read_version(workflow_id, v2)
        if a is None or b is None:
            return None
        return diff_versions(a.get("workflow") or {}, b.get("workflow") or {})

    def audit(self, workflow_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        p = self._audit_path(workflow_id)
        if not p.exists():
            return []
        try:
            lines = p.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def save(
        self,
        workflow_id: str,
        payload: Dict[str, Any],
        actor: str = "web",
        replay_fn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # issue #516: every save = immutable version + gates + audit entry.
        # Gates fail → version recorded as 'rejected', activation blocked
        # (previous active version stays current).
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("workflow body required")
        digest = canonical_digest(workflow)
        existing = self.list_versions(workflow_id)
        next_version = (max((v["version"] or 0) for v in existing) + 1) if existing else 1
        prev_digest = ""
        for v in reversed(existing):
            if v.get("status") == "active":
                prev_digest = str(v.get("digest") or "")
                break
        record: Dict[str, Any] = {
            "version": next_version,
            "digest": digest,
            "status": "active",
            "actor": str(actor or "web"),
            "created_at": time.time(),
            "workflow": copy.deepcopy(workflow),
            "name": payload.get("name", workflow_id),
        }
        gate_results: List[Dict[str, Any]] = []

        ok, acc = evaluate_acceptance(payload.get("acceptance"), workflow)
        gate_results.extend(acc)
        if not ok:
            record["status"] = "rejected"

        # issue #516: regression gate — replay the new definition against
        # recorded golden executions before activation (opt-in per asset)
        regression = payload.get("regression")
        if record["status"] == "active" and isinstance(regression, dict) and regression.get("enabled"):
            golden = self.load_golden(workflow_id)
            cases = golden.get("cases") or []
            if not cases:
                gate_results.append(
                    {"type": "regression", "ok": False, "detail": "regression enabled but no golden cases recorded"}
                )
                record["status"] = "rejected"
            elif replay_fn is None:
                gate_results.append({"type": "regression", "ok": False, "detail": "no replay executor available"})
                record["status"] = "rejected"
            else:
                for case in cases:
                    res = self._replay_case(replay_fn, workflow, case)
                    gate_results.append(res)
                    if not res["ok"]:
                        record["status"] = "rejected"
                        break

        record["gate_results"] = gate_results
        vp = self._versions_dir(workflow_id) / f"v{next_version}.json"
        vp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_audit(
            workflow_id,
            {
                "at": time.time(),
                "actor": record["actor"],
                "version": next_version,
                "digest": digest,
                "prev_digest": prev_digest,
                "status": record["status"],
                "changed": diff_versions(
                    (
                        (self._read_version(workflow_id, self._prev_active_version(existing)) or {}).get("workflow")
                        or {}
                    ),
                    workflow,
                )
                if prev_digest
                else {"initial": True},
                "gate_results": gate_results,
            },
        )
        logger.info(
            f"workflow asset saved: {workflow_id} v{next_version} digest={digest} status={record['status']} by={record['actor']}"
        )
        return record

    @staticmethod
    def _prev_active_version(existing: List[Dict[str, Any]]) -> int:
        for v in reversed(existing):
            if v.get("status") == "active":
                return int(v["version"] or 0)
        return 0

    def _append_audit(self, workflow_id: str, entry: Dict[str, Any]) -> None:
        with self._audit_path(workflow_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def load_golden(self, workflow_id: str) -> Dict[str, Any]:
        p = self._golden_path(workflow_id)
        if not p.exists():
            return {"cases": []}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"golden suite read failed {workflow_id}: {e}")
            return {"cases": []}
        return data if isinstance(data, dict) and isinstance(data.get("cases"), list) else {"cases": []}

    def record_golden_case(self, workflow_id: str, inputs: Dict[str, Any], node_digests: Dict[str, str]) -> int:
        # issue #516: golden recording — prior successful executions become the
        # regression suite (per-node output digests)
        data = self.load_golden(workflow_id)
        data.setdefault("cases", []).append(
            {"recorded_at": time.time(), "inputs": copy.deepcopy(inputs or {}), "node_digests": dict(node_digests)}
        )
        data["cases"] = data["cases"][-10:]
        self._golden_path(workflow_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n = len(data["cases"])
        logger.info(f"golden case recorded: {workflow_id} (suite size {n})")
        return n

    @staticmethod
    def _replay_case(replay_fn: Any, workflow: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, Any]:
        try:
            digests = replay_fn(workflow, case.get("inputs") or {})
        except Exception as e:
            return {
                "type": "regression",
                "ok": False,
                "detail": f"replay error: {e}",
                "case_recorded_at": case.get("recorded_at"),
            }
        golden_digests = case.get("node_digests") or {}
        mismatches = [nid for nid, gd in golden_digests.items() if digests.get(nid) != gd]
        detail = "all node outputs match" if not mismatches else "output mismatch at: " + ", ".join(mismatches)
        return {
            "type": "regression",
            "ok": not mismatches,
            "detail": detail,
            "case_recorded_at": case.get("recorded_at"),
        }
