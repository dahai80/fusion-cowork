from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fusion_cowork.review.ultra_review import (
    ReviewFinding,
    ReviewReport,
    _aggregate,
    _fallback_summary,
    _parse_findings,
    run_ultra_review,
)


def _llm_resp(content):
    r = MagicMock()
    r.content = content
    return r


def test_parse_findings_json():
    raw = '[{"file": "a.py", "line": 10, "severity": "high", "category": "injection", "message": "sql 注入"}]'
    findings = _parse_findings(raw, "security")
    assert len(findings) == 1
    assert findings[0].file == "a.py"
    assert findings[0].line == 10
    assert findings[0].severity == "high"
    assert findings[0].lens == "security"


def test_parse_findings_markdown_wrapped():
    raw = '```json\n[{"file": "b.py", "line": 1, "severity": "low", "category": "x", "message": "m"}]\n```'
    findings = _parse_findings(raw, "style")
    assert len(findings) == 1
    assert findings[0].lens == "style"


def test_parse_findings_empty():
    assert _parse_findings("[]", "tests") == []
    assert _parse_findings("not json", "correctness") == []


def test_aggregate_dedup_and_sort():
    findings = [
        ReviewFinding(file="a.py", line=1, severity="low", category="c", message="m", lens="style"),
        ReviewFinding(file="a.py", line=1, severity="low", category="c", message="m", lens="security"),
        ReviewFinding(file="b.py", line=2, severity="critical", category="c2", message="m2", lens="security"),
    ]
    agg = _aggregate(findings)
    assert len(agg) == 2
    assert agg[0].severity == "critical"
    assert agg[1].severity == "low"


def test_fallback_summary():
    contents = {"a.py": "x = 1"}
    md = _fallback_summary(contents)
    assert "降级模式" in md
    assert "a.py" in md


def test_fallback_summary_with_findings():
    contents = {"a.py": "x = 1"}
    findings = [ReviewFinding(file="a.py", line=1, severity="high", category="c", message="m", lens="security")]
    md = _fallback_summary(contents, findings)
    assert "high: 1" in md


def test_run_ultra_review_full_flow(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")

    client = MagicMock()
    security = _llm_resp(
        '[{"file": "sample.py", "line": 1, "severity": "low", "category": "naming", "message": "命名过短"}]'
    )
    client.chat = AsyncMock(
        side_effect=[security, _llm_resp("[]"), _llm_resp("[]"), _llm_resp("[]"), _llm_resp("审查总结: 代码简洁")]
    )
    client.list_models = AsyncMock(return_value=[{"id": "test-model"}])

    report = asyncio.run(run_ultra_review([str(f)], mlx_client=client))

    assert isinstance(report, ReviewReport)
    assert str(f) in report.files_reviewed
    assert len(report.findings) == 1
    assert report.findings[0].severity == "low"
    assert report.degraded is False
    assert "审查总结" in report.summary


def test_run_ultra_review_llm_fail_degrades(tmp_path):
    f = tmp_path / "degraded.py"
    f.write_text("y = 2\n", encoding="utf-8")

    client = MagicMock()
    client.chat = AsyncMock(side_effect=ConnectionError("no llm"))
    client.list_models = AsyncMock(return_value=[])

    report = asyncio.run(run_ultra_review([str(f)], mlx_client=client))

    assert report.degraded is True
    assert len(report.findings) == 0
    assert "降级模式" in report.summary
    assert str(f) in report.files_reviewed


def test_run_ultra_review_no_paths():
    report = asyncio.run(run_ultra_review([]))
    assert report.error != ""
    assert "未指定" in report.error


def test_run_ultra_review_invalid_lens(tmp_path):
    report = asyncio.run(run_ultra_review([str(tmp_path)], lenses=["nonexistent"]))
    assert "未知审查视角" in report.error


def test_run_ultra_review_nonexistent_file(tmp_path):
    report = asyncio.run(run_ultra_review([str(tmp_path / "nope.py")], mlx_client=MagicMock()))
    assert "无可审查文件" in report.error
