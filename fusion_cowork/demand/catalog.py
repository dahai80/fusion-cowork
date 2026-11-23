# issue #587: capability catalog + pure-code matching (Rule 5 — no LLM).
# Catalog = versioned JSON file (schema per fusion-insight
# factory-capability-catalog-spec-2026-11-21); scoring deterministic:
# keyword/skill overlap + budget-range fit. Catalog version recorded on
# every match row so score changes trace to catalog changes.

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "catalog_seed.json"
BUDGET_RE = re.compile(r"[$¥€]?\s?([\d][\d,.]*)\s?(k|w|万)?", re.IGNORECASE)


def default_catalog_path() -> str:
    return os.environ.get("FUSION_COWORK_DEMAND_CATALOG", str(SEED_PATH))


def runtime_catalog_path() -> str:
    # issue #595: PUT writes here (never the in-repo seed); env override wins
    return os.environ.get("FUSION_COWORK_DEMAND_CATALOG", str(Path.home() / ".fusion-cowork" / "demand_catalog.json"))


def save_catalog(data: Dict[str, Any], path: str = "") -> str:
    # atomic write (tmp + replace); returns the version that was saved
    target = Path(path or runtime_catalog_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    logger.info(f"catalog saved: {target} v{data.get('version')}")
    return str(data.get("version", ""))


def load_catalog(path: str = "") -> Dict[str, Any]:
    p = Path(path or default_catalog_path())
    data = json.loads(p.read_text(encoding="utf-8"))
    version = str(data.get("version", ""))
    if not version:
        raise ValueError(f"catalog missing version: {p}")
    caps = [c for c in data.get("capabilities", []) if c.get("enabled", True)]
    logger.info(f"catalog loaded: v{version}, {len(caps)} enabled capabilities")
    return data


def _budget_to_cny(text: str) -> Optional[tuple]:
    """Parse budget hint like '$500-1000' / '¥2万' -> (low, high) CNY-ish numbers."""
    if not text:
        return None
    nums = []
    for m in BUDGET_RE.finditer(text):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit == "k":
            val *= 1000
        elif unit in ("w", "万"):
            val *= 10000
        nums.append(val)
    if not nums:
        return None
    return (min(nums), max(nums))


def score_lead(
    lead: Dict[str, Any],
    catalog: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Score one lead against every enabled capability.
    Deterministic: keyword/skill overlap (title+description+skills) + budget fit.
    Returns per-capability entries sorted by score desc:
    {id, name_en, score, reason, price_cny}."""
    text = " ".join(
        [
            str(lead.get("title", "")),
            str(lead.get("description", ""))[:1500],
            " ".join(str(s) for s in (lead.get("skills") or [])),
        ]
    ).lower()
    lead_skills = {str(s).lower().strip() for s in (lead.get("skills") or [])}
    budget = _budget_to_cny(str(lead.get("budget_hint", "")))
    results: List[Dict[str, Any]] = []
    for cap in catalog.get("capabilities", []):
        if not cap.get("enabled", True):
            continue
        keywords = [str(k).lower() for k in cap.get("keywords", [])]
        hit_kw = [k for k in keywords if k and k in text]
        skill_hit = sorted(lead_skills & {k.lower() for k in keywords})
        score = 0.0
        reasons = []
        if hit_kw:
            score += min(len(hit_kw), 5) * 2.0
            reasons.append(f"keywords: {', '.join(hit_kw[:5])}")
        if skill_hit:
            score += len(skill_hit) * 1.5
            reasons.append(f"skills: {', '.join(skill_hit[:5])}")
        lo, hi = float(cap.get("budget_min_cny", 0)), float(cap.get("budget_max_cny", 10**9))
        if budget:
            if budget[1] >= lo and budget[0] <= hi:
                score += 2.0
                reasons.append(f"budget {budget[0]:.0f}-{budget[1]:.0f} in range {lo:.0f}-{hi:.0f}")
            else:
                score -= 1.0
                reasons.append(f"budget {budget[0]:.0f}-{budget[1]:.0f} outside range {lo:.0f}-{hi:.0f}")
        if score > 0:
            results.append(
                {
                    "id": cap["id"],
                    "name_en": cap.get("name_en", cap["id"]),
                    "score": round(score, 2),
                    "reason": "; ".join(reasons),
                    "price_cny": cap.get("price_cny", 0),
                }
            )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def match_lead(
    lead: Dict[str, Any],
    catalog: Dict[str, Any],
    threshold: float = 3.0,
) -> Dict[str, Any]:
    """Match result for one lead: matched above threshold, parked below (never dropped)."""
    scored = score_lead(lead, catalog)
    matched = [r for r in scored if r["score"] >= threshold]
    top = matched[0] if matched else (scored[0] if scored else None)
    return {
        "matched_capabilities": [r["id"] for r in matched],
        "match_score": top["score"] if top else 0.0,
        "match_reason": top["reason"] if top else "no keyword/skill overlap",
        "catalog_version": str(catalog.get("version", "")),
        "match_details": scored[:5],
        "parked": not matched,
        "price_cny": top["price_cny"] if top else 0,
    }
