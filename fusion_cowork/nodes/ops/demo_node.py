"""issue #452: demo_contract — 交付演示三件套契约门 + bundle 清单 + executor 注册钩子。

契约 (AGENTS.md rule 7, 与 meta repo scripts/check-demo-runnable.py 语义一致):
1. docker-compose.demo.yml — 每个 service 必须带 healthcheck
2. demo.yaml — web.port 或 terminal 入口; mobile 段可选 (platform=android|ios + artifact + stream)
3. stub pack — demo-stubs/ 或 tests/fixtures/stubs/ 非空 (CI 录制 fixture 回放, 不手写 mock)

缺件 = 交付失败 (actionable error 逐条列出)。可选运行 meta repo 门脚本
(check_script 参数, 全局跑; 仅当本仓库被点名才判失败, 别的仓库违规只告警 —
按仓库归属公平)。secret 扫描缺省开启: stub pack 命中疑似凭据 = 失败
(验收 4: bundle 零真实客户数据)。register=true 时向 executor demo API 注册
(FUSION_EXECUTOR_URL / executor_url 参数; fusion-executor#68), 未配置 = 显式
报错不静默。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

from fusion_cowork.engine.node import BaseNode, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)

DEMO_COMPOSE = "docker-compose.demo.yml"
DEMO_DESC = "demo.yaml"
STUB_DIRS = ["demo-stubs", "tests/fixtures/stubs"]
MOBILE_PLATFORMS = {"android", "ios"}
CHECK_SCRIPT_TIMEOUT = 60.0
REGISTER_TIMEOUT = 10.0

_SECRET_PATTERNS = [
    re.compile(r"gh[posr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{24,}"),
]


def check_compose(repo: Path) -> List[str]:
    errs: List[str] = []
    path = repo / DEMO_COMPOSE
    if not path.exists():
        return [f"missing {DEMO_COMPOSE}"]
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"{DEMO_COMPOSE} invalid yaml: {e}"]
    services = (doc or {}).get("services") or {}
    if not services:
        errs.append(f"{DEMO_COMPOSE} has no services")
    for name, svc in services.items():
        svc = svc or {}
        if not svc.get("healthcheck"):
            errs.append(f"{DEMO_COMPOSE} service '{name}' missing healthcheck")
    return errs


def check_demo_yaml(repo: Path) -> List[str]:
    errs: List[str] = []
    path = repo / DEMO_DESC
    if not path.exists():
        return [f"missing {DEMO_DESC}"]
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return [f"{DEMO_DESC} invalid yaml: {e}"]
    if not doc.get("web", {}).get("port") and not doc.get("terminal"):
        errs.append(f"{DEMO_DESC} needs web.port or terminal entry")
    mobile = doc.get("mobile")
    if mobile is not None:
        platform = mobile.get("platform")
        if platform not in MOBILE_PLATFORMS:
            errs.append(f"{DEMO_DESC} mobile.platform must be one of {sorted(MOBILE_PLATFORMS)}")
        if not mobile.get("artifact"):
            errs.append(f"{DEMO_DESC} mobile.artifact missing (apk / simulator build path)")
        if not mobile.get("stream"):
            errs.append(f"{DEMO_DESC} mobile.stream missing (e.g. webrtc)")
    return errs


def check_stub_pack(repo: Path) -> tuple:
    # returns (errors, stub_dir or "")
    for d in STUB_DIRS:
        stub = repo / d
        if stub.is_dir() and any(stub.iterdir()):
            return [], d
    return [f"mock stub pack missing (expected non-empty {' or '.join(STUB_DIRS)})"], ""


def scan_stub_secrets(stub_dir: Path) -> List[str]:
    hits: List[str] = []
    for p in sorted(stub_dir.rglob("*")):
        if not p.is_file() or p.stat().st_size > 1_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in _SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append(f"{p.name}: {m.group(0)[:12]}***")
                break
    return hits


@register_node
class DemoContractNode(BaseNode):
    name = "demo_contract"
    description = "Demo contract gate: validate the three-piece demo set, build the bundle manifest, optionally register to the executor demo API"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "gate on/off (default false = success no-op)"},
                "deliverable_dir": {"type": "string", "description": "delivered repo/worktree path"},
                "slug": {"type": "string", "description": "sample slug (default: dir basename)"},
                "check_script": {"type": "string", "description": "optional meta-repo check-demo-runnable.py path"},
                "register": {"type": "boolean", "description": "register sample to executor demo API (default false)"},
                "executor_url": {
                    "type": "string",
                    "description": "executor demo API base (default FUSION_EXECUTOR_URL)",
                },
            },
            "required": [],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        cfg = self.config.params if self.config else {}
        if not self._to_bool(cfg.get("enabled")):
            logger.info("demo_contract disabled — success no-op (zero-demo behavior unchanged)")
            return NodeResult(status=NodeStatus.SUCCESS, data={"skipped": True}, summary="demo gate disabled (no-op)")

        deliverable = str(cfg.get("deliverable_dir", "") or "").strip()
        slug = str(cfg.get("slug", "") or "").strip()
        if not deliverable:
            # 缺省定位: FUSION_BUSINESS_ROOT/<slug> (脚手架标准布局), 未配置 root 则显式报错
            business_root = os.environ.get("FUSION_BUSINESS_ROOT", "").strip()
            if not slug:
                raise ValueError(
                    "demo_contract enabled but deliverable_dir and slug both empty — cannot locate deliverable"
                )
            if not business_root:
                raise ValueError(
                    "demo_contract enabled but deliverable_dir empty and FUSION_BUSINESS_ROOT unset — "
                    "set deliverable_dir explicitly or export FUSION_BUSINESS_ROOT"
                )
            deliverable = str(Path(business_root) / slug)
        repo = Path(deliverable).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError(f"deliverable_dir not a directory: {repo}")

        slug = slug or repo.name
        violations: List[str] = []
        compose_errs = check_compose(repo)
        yaml_errs = check_demo_yaml(repo)
        stub_errs, stub_dir_rel = check_stub_pack(repo)
        violations = compose_errs + yaml_errs + stub_errs

        demo_doc: Dict[str, Any] = {}
        yaml_path = repo / DEMO_DESC
        if yaml_path.exists() and not any("invalid yaml" in e for e in yaml_errs):
            demo_doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

        stub_dir = repo / stub_dir_rel if stub_dir_rel else None
        secret_hits = scan_stub_secrets(stub_dir) if stub_dir else []
        if secret_hits:
            violations = violations + [f"stub pack secret-scan hit: {h}" for h in secret_hits]

        if violations:
            logger.error(f"demo_contract violations for {slug}: {violations}")
            raise ValueError(f"demo contract violations for '{slug}' ({repo}):\n- " + "\n- ".join(violations))

        script_ok: Dict[str, Any] = {"ran": False}
        script = str(cfg.get("check_script", "") or "").strip()
        if script:
            script_ok = await self._run_check_script(script, repo.name)
            if script_ok.get("ran") and script_ok.get("ok") is False:
                raise ValueError(
                    f"meta demo gate flags '{repo.name}':\n- " + "\n- ".join(script_ok.get("own_violations", []))
                )

        bundle = {
            "slug": slug,
            "compose": DEMO_COMPOSE,
            "services": sorted(
                ((yaml.safe_load((repo / DEMO_COMPOSE).read_text(encoding="utf-8")) or {}).get("services") or {}).keys()
            ),
            "entry": {"web_port": demo_doc.get("web", {}).get("port"), "terminal": demo_doc.get("terminal")},
            "mobile": demo_doc.get("mobile") or None,
            "stub_dir": stub_dir_rel,
            "stub_fixtures": sorted(p.name for p in stub_dir.iterdir() if p.is_file())[:50] if stub_dir else [],
            "check_script": script_ok,
        }
        logger.info(f"demo_contract passed for {slug}: stub_dir={stub_dir_rel} entry={bundle['entry']}")

        registered = False
        if self._to_bool(cfg.get("register")):
            executor_url = (
                str(cfg.get("executor_url", "") or os.environ.get("FUSION_EXECUTOR_URL", "") or "").strip().rstrip("/")
            )
            if not executor_url:
                raise ValueError(
                    "demo_contract register=true but executor_url/FUSION_EXECUTOR_URL unset "
                    "(executor demo API not configured — see fusion-executor#68)"
                )
            registered = await self._register(executor_url, bundle)

        output = {"passed": True, "slug": slug, "bundle": bundle, "registered": registered, "violations": []}
        return NodeResult(
            status=NodeStatus.SUCCESS, data=output, summary=f"demo contract passed for {slug} (registered={registered})"
        )

    @staticmethod
    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    async def _run_check_script(self, script: str, repo_name: str) -> Dict[str, Any]:
        # meta repo gate — global run; only violations naming THIS repo block.
        # Other repos' violations are their owners' business (per-repo fairness).
        script_path = Path(script).expanduser()
        if not script_path.is_file():
            logger.warning(f"check_script not found, skipping meta gate: {script}")
            return {"ran": False, "error": f"script not found: {script}"}

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            logger.warning(f"check_script spawn failed, skipping meta gate: {e}")
            return {"ran": False, "error": str(e)}
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=CHECK_SCRIPT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            logger.warning("check_script timed out, skipping meta gate")
            return {"ran": False, "error": "timeout"}
        text = stderr.decode("utf-8", errors="ignore")
        mine = [ln for ln in text.splitlines() if f"violation: {repo_name}:" in ln]
        if proc.returncode == 0:
            return {"ran": True, "ok": True, "own_violations": []}
        if mine:
            return {"ran": True, "ok": False, "own_violations": mine}
        logger.warning(f"check_script failed on other repos only (this repo not named): exit={proc.returncode}")
        return {"ran": True, "ok": True, "other_repo_violations_only": True}

    async def _register(self, executor_url: str, bundle: Dict[str, Any]) -> bool:
        import httpx

        payload = {
            "slug": bundle["slug"],
            "kind": "mobile" if bundle.get("mobile") else "web",
            "bundle": bundle,
        }
        try:
            async with httpx.AsyncClient(timeout=REGISTER_TIMEOUT) as client:
                resp = await client.post(f"{executor_url}/api/v1/demo/register", json=payload)
        except httpx.HTTPError as e:
            raise ValueError(f"demo register to {executor_url} failed: {e}") from e
        if resp.status_code not in (200, 201, 202):
            raise ValueError(f"demo register to {executor_url} failed: {resp.status_code} {resp.text[:200]}")
        logger.info(f"demo sample registered to executor: {bundle['slug']} kind={payload['kind']}")
        return True
