"""Delivery pipeline intake mode (issue #400).

Customer input is exactly one thing — a requirement (text or document).
Everything else is derived server-side:

- slug: deterministic normalization from the requirement (collision-safe
  `-2`/`-3` suffix, pattern `^ai-[a-z0-9]+(-[a-z0-9]+)*$`)
- repo: private `dahai80/business/ai-<slug>` created via the GitHub API at
  launch (idempotent — existing repo is reused, cow#361/#368 no-op contract)
- registry: `business/registry.yaml` gains a `source: pipeline` entry with the
  run_id in notes; check-slugs.py must pass 0 errors before repo creation

Token discipline: GITHUB_TOKEN env only, never request body, never logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import yaml

logger = logging.getLogger("fusion_cowork.intake")

# test seam — inject a stub module/object instead of the real implementation
INTAKE_FACTORY: Any = None

_SLUG_PATTERN = re.compile(r"^ai-[a-z0-9]+(-[a-z0-9]+)*$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_DEFAULT_REGISTRY = Path.home() / "fusion" / "business" / "registry.yaml"
_MAX_ATTEMPTS = 20


def registry_path() -> Path:
    return Path(os.environ.get("FUSION_BUSINESS_REGISTRY") or _DEFAULT_REGISTRY)


def derive_slug(requirement: str) -> str:
    """Deterministic slug from the requirement: ASCII words from the first
    meaningful line; CJK-only requirements fall back to a content hash so the
    same requirement always derives the same slug."""
    text = (requirement or "").strip()
    if not text:
        raise ValueError("requirement is empty — nothing to derive a slug from")
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    words = _WORD_RE.findall(first_line.lower())
    words = [w for w in words if len(w) >= 2][:4]
    if words:
        base = "ai-" + "-".join(words)
    else:
        base = "ai-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    if not _SLUG_PATTERN.match(base):
        base = "ai-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return base


def load_registry_slugs(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else registry_path()
    if not p.exists():
        raise FileNotFoundError(f"slug registry not found: {p} (set FUSION_BUSINESS_REGISTRY)")
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    return data.get("slugs") or {}


def next_available_slug(base: str, taken: set) -> str:
    if base not in taken:
        return base
    for i in range(2, 2 + _MAX_ATTEMPTS):
        candidate = f"{base}-{i}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"no free slug suffix under {base} ({_MAX_ATTEMPTS} attempts)")


def check_slug_slug_valid(slug: str) -> None:
    if not _SLUG_PATTERN.match(slug):
        raise ValueError(f"slug '{slug}' violates naming pattern {_SLUG_PATTERN.pattern}")


class RepoClient:
    """GitHub repo API — GITHUB_TOKEN env only; client_factory seam for tests."""

    def __init__(self, token: str = "", base_url: str = "", client_factory: Any = None, owner: str = "dahai80"):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.base_url = (base_url or os.environ.get("FUSION_GITHUB_API", "https://api.github.com")).rstrip("/")
        self.client_factory = client_factory
        self.owner = owner

    def _client(self, timeout: float = 15.0) -> Any:
        if self.client_factory is not None:
            return self.client_factory(timeout=timeout)
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout)

    async def get_repo(self, name: str) -> Optional[Dict[str, Any]]:
        async with self._client() as client:
            resp = await client.get(f"/repos/{self.owner}/{name}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        raise RuntimeError(f"GitHub GET /repos/{self.owner}/{name} -> {resp.status_code}")

    async def create_repo(self, name: str) -> Dict[str, Any]:
        async with self._client() as client:
            resp = await client.post("/user/repos", json={"name": name, "private": True, "auto_init": True})
        if resp.status_code in (201, 202):
            return resp.json()
        raise RuntimeError(f"GitHub POST /user/repos ({name}, private) -> {resp.status_code}: {resp.text[:200]}")

    async def ensure_repo(self, name: str) -> Dict[str, Any]:
        """Idempotent: existing repo = reuse (no-op contract), missing = create private."""
        check_slug_slug_valid(name)
        existing = await self.get_repo(name)
        if existing is not None:
            visibility = "private" if existing.get("private") else "public"
            logger.info("intake repo reuse: %s/%s (%s)", self.owner, name, visibility)
            return {
                "created": False,
                "full_name": existing.get("full_name") or f"{self.owner}/{name}",
                "visibility": visibility,
            }
        created = await self.create_repo(name)
        logger.info("intake repo created: %s (private)", created.get("full_name"))
        return {
            "created": True,
            "full_name": created.get("full_name") or f"{self.owner}/{name}",
            "visibility": "private",
        }


def register_slug(slug: str, run_id: str, remote: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Insert/keep a `source: pipeline` entry in registry.yaml slugs block.

    Textual insertion (not yaml.dump) — the file carries comments that must
    survive. Idempotent: an existing entry is left untouched (returns existed=True).
    """
    p = Path(path) if path else registry_path()
    if not p.exists():
        raise FileNotFoundError(f"slug registry not found: {p}")
    lines = p.read_text().splitlines(keepends=True)
    entry = (
        f"  {slug}: {{status: active, source: pipeline, github: {{remote: {remote}, visibility: private}}, "
        f'notes: "run_id: {run_id}"}}\n'
    )
    in_block = False
    insert_at = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "slugs:" and not line.startswith(" "):
            in_block = True
            continue
        if in_block:
            if stripped and not line.startswith(" "):
                break  # next top-level key — insert before it
            m = re.match(r"^\s+([A-Za-z0-9-]+):", line)
            if m and m.group(1) > slug:
                insert_at = i
                break
    if insert_at is None:
        insert_at = len(lines) if in_block else None
    if insert_at is None:
        raise ValueError(f"registry {p} has no top-level 'slugs:' block")
    if any(line.strip().startswith(f"{slug}:") for line in lines):
        logger.info("intake registry: %s already registered — no-op", slug)
        return {"existed": True, "registered": False, "path": str(p)}
    lines.insert(insert_at, entry)
    p.write_text("".join(lines))
    logger.info("intake registry: %s registered (source: pipeline, run_id: %s)", slug, run_id)
    return {"existed": False, "registered": True, "path": str(p)}


def update_registry_run_id(slug: str, run_id: str, path: Optional[Path] = None) -> bool:
    """Fill the real run_id into an existing pipeline entry's notes (best-effort)."""
    p = Path(path) if path else registry_path()
    if not p.exists():
        return False
    text = p.read_text()
    pattern = re.compile(rf"^(\s+{re.escape(slug)}:.*notes: \"run_id: )([^\"]*)(\")", re.MULTILINE)
    new_text, n = pattern.subn(rf"\g<1>{run_id}\g<3>", text)
    if n == 0:
        return False
    p.write_text(new_text)
    logger.info("intake registry: %s notes updated with run_id %s", slug, run_id)
    return True


def run_check_slugs(script_path: Optional[Path] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """Run scripts/check-slugs.py; 0 errors required. Missing script = warn + pass
    (dev machines without the fusion checkout must not brick the pipeline)."""
    candidates = [script_path] if script_path else [Path.home() / "fusion" / "scripts" / "check-slugs.py"]
    script = next((c for c in candidates if c.exists()), None)
    if script is None:
        logger.warning("check-slugs.py not found — skipping slug governance check")
        return {"ran": False, "ok": True, "reason": "script not found"}
    proc = subprocess.run(
        ["python3", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (proc.stdout + proc.stderr)[-2000:]
    ok = proc.returncode == 0
    logger.info("check-slugs: rc=%s ok=%s", proc.returncode, ok)
    return {"ran": True, "ok": ok, "output": output, "returncode": proc.returncode}


def task_title(requirement: str, cap: int = 120) -> str:
    first_line = next((ln.strip() for ln in (requirement or "").splitlines() if ln.strip()), "")
    return first_line[:cap] or "Implement requirement"


async def prepare_intake(
    requirement: str,
    slug_hint: str = "",
    repo_client: Optional[RepoClient] = None,
    path: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Derive + validate slug, ensure the private repo, register the slug.

    Returns the plan dict consumed by the launch endpoint / run detail.
    Raises ValueError on bad input, RuntimeError on GitHub/check-slugs failure.
    """
    text = (requirement or "").strip()
    if len(text) < 10:
        raise ValueError("requirement too short — paste the actual requirement text (>= 10 chars)")
    taken = set(load_registry_slugs(path).keys())
    slug = (slug_hint or "").strip().lower() or derive_slug(text)
    check_slug_slug_valid(slug)
    suggested_from = ""
    if slug in taken:
        suggested_from = slug
        slug = next_available_slug(slug, taken)
    check = run_check_slugs()
    if not check.get("ok", False):
        raise RuntimeError(f"check-slugs failed — refusing repo creation: {check.get('output', '')[-300:]}")
    plan: Dict[str, Any] = {
        "slug": slug,
        "suggested_from": suggested_from,
        "repo": f"{'dahai80'}/{slug}",
        "registry_check": check,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return plan
    client = repo_client or RepoClient()
    repo_info = await client.ensure_repo(slug)
    remote = repo_info["full_name"]
    reg_info = register_slug(slug, "pending", remote, path=path)
    plan.update({"repo_info": repo_info, "registry_info": reg_info})
    return plan
