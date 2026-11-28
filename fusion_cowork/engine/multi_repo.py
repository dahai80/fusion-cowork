# issue #653: multi-repo orchestration — spec-first runtime behind FUSION_COWORK_MULTI_REPO=1.
# Admission validation (repo spec shape + credential-literal rejection), the identity
# token-resolution seam, and per-repo stage timing aggregation. Spec: docs/multi-repo-orchestration.md

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

MULTI_REPO_ENV = "FUSION_COWORK_MULTI_REPO"
_CREDENTIAL_KEYS = ("token", "password", "secret", "ssh_key", "access_token", "private_key")


def multi_repo_enabled() -> bool:
    return os.environ.get(MULTI_REPO_ENV, "").strip().lower() in ("1", "true", "on", "yes")


@dataclass
class RepoSpec:
    url: str
    ref: str = "main"
    token_source: str = ""


def _repo_block(node: Any) -> Optional[Dict[str, Any]]:
    params = getattr(node.config, "params", None) or {}
    repo = params.get("repo")
    return repo if isinstance(repo, dict) else None


def parse_repo_spec(node: Any) -> Optional[RepoSpec]:
    block = _repo_block(node)
    if block is None:
        return None
    url = str(block.get("url", "")).strip()
    if not url:
        raise ValueError(f"node '{node.id}': params.repo.url is required")
    return RepoSpec(
        url=url,
        ref=str(block.get("ref", "main")).strip() or "main",
        token_source=str(block.get("token_source", "")).strip(),
    )


def collect_repo_specs(workflow: Any) -> Dict[str, RepoSpec]:
    # node_id -> spec for nodes declaring params.repo
    specs: Dict[str, RepoSpec] = {}
    for node_id, node in workflow.nodes.items():
        spec = parse_repo_spec(node)
        if spec is not None:
            specs[node_id] = spec
    return specs


def validate_multi_repo_workflow(workflow: Any) -> Dict[str, RepoSpec]:
    # Admission gate (fail-closed): shape check + credential-literal rejection +
    # waits_on_artifact edge verification. Raises ValueError on violation.
    specs: Dict[str, RepoSpec] = {}
    for node_id, node in workflow.nodes.items():
        block = _repo_block(node)
        if block is None:
            continue
        spec = parse_repo_spec(node)
        _reject_credential_literals(node_id, block)
        specs[node_id] = spec
        source = str((node.config.params or {}).get("waits_on_artifact", "")).strip()
        if source:
            src_node = source.split(":", 1)[0]
            if src_node not in workflow.nodes:
                raise ValueError(f"node '{node_id}': waits_on_artifact references unknown node '{src_node}'")
            if not any(e.source_id == src_node and e.target_id == node_id for e in workflow.edges):
                raise ValueError(
                    f"node '{node_id}': waits_on_artifact requires a DAG edge from '{src_node}' "
                    f"(cross-repo waits ride the topological order)"
                )
    return specs


def _reject_credential_literals(node_id: str, block: Dict[str, Any]) -> None:
    def _scan(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in _CREDENTIAL_KEYS:
                    raise ValueError(
                        f"node '{node_id}': params.repo.{path}{k} looks like an inline credential — "
                        f"use token_source (identity://...) instead; workflow YAML never carries secrets"
                    )
                _scan(v, f"{path}{k}.")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _scan(v, f"{path}{i}.")

    _scan(block, "")


def resolve_repo_token(token_source: str, base_url: str = "") -> str:
    # Identity-service resolution seam — identity://<tenant>/<token-name> exchanged
    # for a short-lived repo token at execution time. Failure is explicit (no env fallback).
    if not token_source.startswith("identity://"):
        raise ValueError(f"unsupported token_source '{token_source}' — only identity:// URIs are accepted")
    if not token_source[len("identity://") :].strip():
        raise ValueError(f"empty token_source '{token_source}'")
    base = (base_url or os.environ.get("FUSION_IDENTITY_URL", "")).strip()
    if not base:
        raise ValueError("identity service URL not configured (FUSION_IDENTITY_URL) — cannot resolve token_source")
    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/api/v1/tokens/resolve",
            json={"token_source": token_source},
            timeout=10.0,
        )
        resp.raise_for_status()
        token = str(resp.json().get("token", "")).strip()
    except Exception as e:
        raise ValueError(f"identity token resolution failed for '{token_source}': {e}") from e
    if not token:
        raise ValueError(f"identity service returned an empty token for '{token_source}'")
    return token


def repo_workspace(url: str, root: Optional[Path] = None) -> Path:
    base = root or (Path.home() / ".fusion-cowork" / "repos")
    ws = base / hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@dataclass
class RepoTiming:
    node_id: str
    started_at: float
    duration_s: float


def repo_timings(specs: Dict[str, RepoSpec], steps: List[Any]) -> Dict[str, Dict[str, Any]]:
    # Aggregate per-repo stage timings from execution steps (pure function —
    # bench#70 / commerce#16 consume this without touching the engine).
    out: Dict[str, Dict[str, Any]] = {}
    for step in steps:
        spec = specs.get(getattr(step, "node_id", ""))
        if spec is None:
            continue
        entry = out.setdefault(spec.url, {"stages": [], "total_s": 0.0})
        entry["stages"].append(
            {
                "node_id": step.node_id,
                "started_at": getattr(step, "started_at", 0.0),
                "duration_s": float(getattr(step, "execution_time", 0.0) or 0.0),
            }
        )
    for entry in out.values():
        entry["stages"].sort(key=lambda s: s["started_at"])
        entry["total_s"] = round(sum(s["duration_s"] for s in entry["stages"]), 6)
    return out
