"""issue #151 + #152: xlsx_write / pptx_write deliverable nodes + reconciliation skill + weekly report template.

Contract:
- xlsx_write: multi-sheet, formula cells ("=..." recalculated on open), header style
  (bold + fill + freeze), conditional formatting rules persisted, column widths;
  explicit failure on missing spec / no sheets.
- pptx_write: title/content/chart/table layouts, period_compare auto-generates a
  delta slide (>threshold% flagged with warning), output re-readable by python-pptx
  (editable).
- /spreadsheet-recon skill: budget vs export directory reconciliation, over-threshold
  flagging + unmatched notes, produces a reconciliation workbook.
- weekly_readout_pptx template: end-to-end offline run through WorkflowEngine
  (local fixture spec).
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.nodes.io.deliverables import PptxWriteNode as PptxCls
from fusion_cowork.nodes.io.deliverables import XlsxWriteNode as XlsxCls


@pytest.fixture(autouse=True)
def _register_nodes():
    saved = dict(NodeRegistry._registry)
    for cls in (XlsxCls, PptxCls):
        if cls.name not in NodeRegistry._registry:
            NodeRegistry._registry[cls.name] = cls
    yield
    NodeRegistry._registry.clear()
    NodeRegistry._registry.update(saved)


def _node(name: str, params: dict):
    node = NodeRegistry.create(name, config=NodeConfig(params=params))
    assert node is not None, f"节点 {name} 未注册"  # i18n: user-facing
    return node


class TestXlsxWrite:
    @pytest.mark.asyncio
    async def test_multi_sheet_formula_style_conditional(self, tmp_path):
        spec = {
            "sheets": [
                {
                    "name": "明细",  # i18n: user-facing
                    "headers": ["科目", "预算", "实际"],  # i18n: user-facing
                    "rows": [
                        ["营销", 100, 128],  # i18n: user-facing
                        ["研发", 200, 190],  # i18n: user-facing
                        ["差旅", 50, "=B3*2"],  # i18n: user-facing
                    ],
                    "column_widths": [20, 12, 12],
                    "conditional": [{"range": "C2:C4", "op": ">", "value": 150, "color": "FFFFC7CE"}],
                },
                {
                    "name": "汇总",  # i18n: user-facing
                    "rows": [["合计", "=SUM(B2:B4)", "=SUM(C2:C4)"]],
                    "header_style": False,
                },  # i18n: user-facing
            ],
            "active_sheet": "汇总",  # i18n: user-facing
        }
        node = _node("xlsx_write", {"output_path": str(tmp_path), "file_name": "wb_test", "overwrite": True})
        result = await node.execute({"spec": spec})
        assert result.status.value == "success", result.error
        out = tmp_path / "wb_test.xlsx"
        assert out.is_file()

        import openpyxl

        wb = openpyxl.load_workbook(out)
        assert wb.sheetnames == ["明细", "汇总"]  # i18n: user-facing
        ws = wb["明细"]  # i18n: user-facing
        assert ws.cell(row=1, column=1).value == "科目"  # i18n: user-facing
        assert ws.cell(row=1, column=1).font.bold, "表头应加粗"  # i18n: user-facing
        assert ws.freeze_panes == "A2", "表头应冻结"  # i18n: user-facing
        assert ws.cell(row=4, column=3).value == "=B3*2", "公式字符串应按公式写入"  # i18n: user-facing
        assert ws.column_dimensions["A"].width == 20
        cf_ranges = [str(rng) for rng in ws.conditional_formatting]
        assert any("C2:C4" in r for r in cf_ranges), f"条件格式应落盘: {cf_ranges}"  # i18n: user-facing
        assert (  # i18n: user-facing
            wb["汇总"].cell(row=1, column=1).font.bold is not True or wb["汇总"].cell(row=2, column=1).value == "合计"
        )

    @pytest.mark.asyncio
    async def test_missing_spec_fails(self, tmp_path):
        node = _node("xlsx_write", {"output_path": str(tmp_path)})
        result = await node.execute({})
        assert result.status.value == "failed"
        assert "spec" in (result.error or "")

    @pytest.mark.asyncio
    async def test_spec_json_string_accepted(self, tmp_path):
        import json

        node = _node(
            "xlsx_write",
            {
                "output_path": str(tmp_path),
                "file_name": "wb_str",
                "spec": json.dumps({"sheets": [{"name": "S", "rows": [[1, 2]]}]}),
            },
        )
        result = await node.execute({})
        assert result.status.value == "success"
        assert result.data["sheet_count"] == 1


class TestPptxWrite:
    @pytest.mark.asyncio
    async def test_full_deck_with_period_compare(self, tmp_path):
        spec = {  # i18n: user-facing
            "title": "周度读出",  # i18n: user-facing
            "subtitle": "本期 vs 上期",  # i18n: user-facing
            "slides": [
                {"layout": "content", "title": "要点", "bullets": ["第一条", "第二条"]},  # i18n: user-facing
                {
                    "layout": "chart",
                    "title": "对比",  # i18n: user-facing
                    "chart": {
                        "type": "column",
                        "categories": ["营收", "订单"],  # i18n: user-facing
                        "series": [{"name": "本期", "values": [120, 80]}],  # i18n: user-facing
                    },
                },
                {"layout": "table", "title": "明细", "headers": ["A", "B"], "rows": [[1, 2]]},  # i18n: user-facing
            ],
            "period_compare": {
                "metric": "指标",  # i18n: user-facing
                "categories": ["营收", "订单", "活跃"],  # i18n: user-facing
                "previous": [100, 90, 180],
                "current": [128, 84, 185],
                "threshold_pct": 10,
            },
        }
        node = _node("pptx_write", {"output_path": str(tmp_path), "file_name": "deck_test", "overwrite": True})
        result = await node.execute({"spec": spec})
        assert result.status.value == "success", result.error
        out = tmp_path / "deck_test.pptx"
        assert out.is_file()

        from pptx import Presentation

        prs = Presentation(out)
        # Title slide + 3 slides + period_compare 2 pages
        assert len(prs.slides) == 6
        slide_count = len(prs.slides)
        has_chart = any(getattr(shape, "has_chart", False) for shape in prs.slides[2].shapes)
        assert has_chart, "图表页应含真实图表对象 (可编辑)"  # i18n: user-facing
        # period_compare delta table: revenue +28% flagged, orders -6.7% not flagged
        text_all = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text_all.append(shape.text_frame.text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        text_all.append(" | ".join(c.text for c in row.cells))
        joined = "\n".join(text_all)
        assert "⚠️ 超阈值" in joined  # i18n: user-facing
        assert slide_count >= 5

    @pytest.mark.asyncio
    async def test_missing_spec_fails(self, tmp_path):
        node = _node("pptx_write", {"output_path": str(tmp_path)})
        result = await node.execute({})
        assert result.status.value == "failed"

    @pytest.mark.asyncio
    async def test_period_compare_zero_previous_no_crash(self, tmp_path):
        spec = {
            "period_compare": {
                "categories": ["新科目"],  # i18n: user-facing
                "previous": [0],
                "current": [10],
                "threshold_pct": 10,
            }
        }
        node = _node("pptx_write", {"output_path": str(tmp_path), "file_name": "deck_zero", "overwrite": True})
        result = await node.execute({"spec": spec})
        assert result.status.value == "success"


class TestSpreadsheetReconSkill:
    @pytest.mark.asyncio
    async def test_recon_end_to_end(self, tmp_path):
        import openpyxl

        budget = tmp_path / "budget.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["科目", "预算"])  # i18n: user-facing
        ws.append(["营销", 100])  # i18n: user-facing
        ws.append(["研发", 200])  # i18n: user-facing
        ws.append(["法务", 50])  # i18n: user-facing
        wb.save(budget)

        exports = tmp_path / "exports"
        exports.mkdir()
        (exports / "week1.csv").write_text("科目,实际\n营销,128\n研发,190\n", encoding="utf-8")  # i18n: user-facing
        (exports / "week2.csv").write_text("科目,实际\n营销,10\n", encoding="utf-8")  # i18n: user-facing
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        ws2.append(["科目", "实际"])  # i18n: user-facing
        ws2.append(["行政", 30])  # i18n: user-facing
        wb2.save(exports / "misc.xlsx")

        from fusion_cowork.skills.builtin import _spreadsheet_recon_handler

        result = await _spreadsheet_recon_handler(f"{budget} {exports} 10")
        assert "error" not in result, result
        data = result["data"]
        assert data["matched"] == 2
        assert data["flagged"] == 1, "营销 138 vs 100 (+38%) 应超 10% 阈值"  # i18n: user-facing
        assert data["unreconciled_budget"] == ["法务"]  # i18n: user-facing
        assert data["unreconciled_actual"] == ["行政"]  # i18n: user-facing
        assert "法务" in data["note"] and "行政" in data["note"]  # i18n: user-facing

        out = openpyxl.load_workbook(data["file_path"])
        rows = list(out.active.iter_rows(values_only=True))
        assert rows[0] == ("科目", "预算", "实际", "差异", "标记")  # i18n: user-facing
        flagged = [r for r in rows[1:] if r[4]]
        assert len(flagged) == 1 and flagged[0][0] == "营销"  # i18n: user-facing

    @pytest.mark.asyncio
    async def test_missing_args(self):
        from fusion_cowork.skills.builtin import _spreadsheet_recon_handler

        result = await _spreadsheet_recon_handler("only-one-arg")
        assert "error" in result


class TestWeeklyReadoutTemplate:
    @pytest.mark.asyncio
    async def test_template_runs_offline(self, tmp_path):
        from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
        from fusion_cowork.templates.template_manager import TemplateManager

        mgr = TemplateManager()
        tpl = mgr.get_template("weekly_readout_pptx")
        assert tpl is not None, "weekly_readout_pptx 模板应存在"  # i18n: user-facing
        wf = Workflow.from_dict(tpl["workflow"])
        engine = WorkflowEngine()
        result = await engine.execute(wf)
        assert result.status.value in ("success", "completed"), getattr(result, "error", None) or result.summary
        pptx_node = next(n for n in wf.nodes.values() if n.name == "pptx_write")
        assert pptx_node.config.params.get("spec"), "模板应内置本地 fixture spec"  # i18n: user-facing

    def test_registration_and_aliases(self):
        assert NodeRegistry.get("xlsx_write") is not None
        assert NodeRegistry.get("pptx_write") is not None
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES.get("写入Excel") == "xlsx_write"  # i18n: user-facing
        assert NODE_NAME_ALIASES.get("写入PPT") == "pptx_write"  # i18n: user-facing
