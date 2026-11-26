# issue #614: office DAG node family — office_spreadsheet / office_document /
# office_slides (fusion-office office-api :11492) + office_artifact
# (artifacts-engine version chain, JSON-RPC) + /v1/outcomes signal reporting.
#
# Two layers:
# - stub tests (CI-safe): a canned ASGI stub replaces office-api/artifacts-engine
#   via httpx.ASGITransport injection; covers all ops + outcome signal mapping.
# - live E2E (skipped unless office-api :11492 is healthy): real DAG leg —
#   handcrafted xlsx parse -> docx weekly report write -> outcome recorded in
#   the real sink; artifact registration only when an API key is configured
#   (the local artifacts-engine runs fail-closed without one).

import asyncio
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus

# ── stub transports ──


def _office_stub_app(outcomes: list, unfilled: str = ""):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    async def xlsx_parse(request: Request) -> JSONResponse:
        form = await request.form()
        file = form.get("file")
        data = await file.read() if file is not None else b""
        if not data.startswith(b"PK"):
            return JSONResponse({"detail": "invalid xlsx"}, status_code=422)
        return JSONResponse({"sheets": [{"name": "Sheet1", "rows": [["a", "b"], ["1", "2"]]}]})

    async def docx_write(request: Request) -> Response:
        form = await request.form()
        markdown = form.get("markdown", "")
        body = f"docx-bytes:{markdown}".encode()
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"X-Office-Doc-SHA256": "sha-" + str(abs(hash(markdown)))[:12], "X-Office-Unfilled": unfilled},
        )

    async def docx_fill(request: Request) -> Response:
        form = await request.form()
        values = json.loads(form.get("values", "{}"))
        body = f"filled:{sorted(values)}".encode()
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"X-Office-Doc-SHA256": "sha-fill", "X-Office-Unfilled": unfilled},
        )

    async def docx_parse(request: Request) -> JSONResponse:
        return JSONResponse({"blocks": [{"kind": "heading", "text": "周报"}]})

    async def pptx_write(request: Request) -> Response:
        form = await request.form()
        slides = form.get("slides", "")
        return Response(
            content=f"pptx-bytes:{slides}".encode(),
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            headers={"X-Office-Doc-SHA256": "sha-pptx", "X-Office-Slide-Count": "3"},
        )

    async def pptx_parse(request: Request) -> JSONResponse:
        return JSONResponse({"slides": [{"title": "s1"}, {"title": "s2"}]})

    async def record_outcome(request: Request) -> JSONResponse:
        outcomes.append(await request.json())
        return JSONResponse({"ok": True}, status_code=202)

    return Starlette(
        routes=[
            Route("/v1/xlsx/parse", xlsx_parse, methods=["POST"]),
            Route("/v1/docx/write", docx_write, methods=["POST"]),
            Route("/v1/docx/fill", docx_fill, methods=["POST"]),
            Route("/v1/docx/parse", docx_parse, methods=["POST"]),
            Route("/v1/pptx/write", pptx_write, methods=["POST"]),
            Route("/v1/pptx/parse", pptx_parse, methods=["POST"]),
            Route("/v1/outcomes", record_outcome, methods=["POST"]),
        ]
    )


def _artifacts_stub_app(calls: list):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    valid_types = ("code", "markdown", "html", "react", "svg", "mermaid", "data")

    async def rpc(request: Request) -> JSONResponse:
        payload = await request.json()
        calls.append(payload)
        method = payload.get("method")
        params = payload.get("params", {})
        if method == "artifact.create":
            if params.get("type") not in valid_types:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32602, "message": "Invalid type"}}
                )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "artifact": {"id": "art-1", "kind": params.get("kind")},
                        "version": {"version": 1, "content_hash": "h1"},
                        "ref_text": "@art-1",
                    },
                }
            )
        if method == "artifact.update":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "artifact": {"id": params.get("artifact_id")},
                        "version": {"version": 2, "content_hash": "h2"},
                        "ref_text": "@art-1",
                    },
                }
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "nope"}}, status_code=200
        )

    return Starlette(routes=[Route("/", rpc, methods=["POST"]), Route("/", rpc, methods=["GET"])])


def _patch_transports(monkeypatch, office_app=None, artifacts_app=None):
    import fusion_cowork.nodes.office.office_nodes as on

    real_async_client = httpx.AsyncClient

    def make(app):
        def factory(**kwargs):
            kwargs.pop("timeout", None)
            return real_async_client(transport=httpx.ASGITransport(app=app), base_url="http://stub")

        return factory

    app = artifacts_app if artifacts_app is not None else office_app
    if app is not None:
        monkeypatch.setattr(on.httpx, "AsyncClient", make(app))
    return on


def _make(node_name, **params):
    return NodeRegistry.get(node_name)(f"n-{node_name}", NodeConfig(params=params))


def _run(node, inputs=None):
    return asyncio.run(node.execute(inputs or {}))


# ── registration + aliases ──


class TestRegistration:
    def test_all_four_nodes_registered_with_aliases(self):
        from fusion_cowork import NODE_NAME_ALIASES

        for name in ("office_document", "office_spreadsheet", "office_slides", "office_artifact"):
            assert NodeRegistry.get(name) is not None, name
        aliases = {k for k, v in NODE_NAME_ALIASES.items() if str(v).startswith("office_")}
        assert {"办公文档", "办公表格", "办公幻灯片", "成果入库"} <= aliases


# ── stub unit tests ──


class TestSpreadsheetNode:
    def test_parse_valid_workbook(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        xlsx = tmp_path / "w.xlsx"
        xlsx.write_bytes(b"PK\x03\x04 fake")
        node = _make("office_spreadsheet", file_path=str(xlsx), rollout_id="ro-1")
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert r.data["sheet_count"] == 1
        assert r.data["valid"] is True
        assert outcomes and outcomes[0]["outcome"]["signal"] == "task_success"
        assert outcomes[0]["rollout_id"] == "ro-1"
        assert outcomes[0]["schema_version"] == 1

    def test_invalid_workbook_fails_with_error_outcome(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        bad = tmp_path / "bad.xlsx"
        bad.write_bytes(b"not a zip")
        node = _make("office_spreadsheet", file_path=str(bad), rollout_id="ro-2")
        r = _run(node)
        assert r.status == NodeStatus.FAILED
        assert outcomes and outcomes[0]["outcome"]["signal"] == "error"


class TestDocumentNode:
    def test_write_markdown_produces_docx_file(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        out = tmp_path / "weekly.docx"
        node = _make("office_document", op="write", markdown="# 周报\n内容", output_path=str(out), rollout_id="ro-3")
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert out.exists() and out.read_bytes().startswith(b"docx-bytes:")
        assert r.data["sha256"].startswith("sha-")
        assert outcomes[0]["outcome"]["signal"] == "task_success"

    def test_fill_with_unfilled_maps_user_edited(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes, unfilled="name,date"))
        tpl = tmp_path / "t.docx"
        tpl.write_bytes(b"PK\x03\x04 fake")
        out = tmp_path / "filled.docx"
        node = _make(
            "office_document",
            op="fill",
            file_path=str(tpl),
            values={"name": "张三"},
            output_path=str(out),
            rollout_id="ro-4",
        )
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert r.data["unfilled"] == ["name", "date"]
        assert outcomes[0]["outcome"]["signal"] == "user_edited"

    def test_fill_complete_maps_task_success(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        tpl = tmp_path / "t.docx"
        tpl.write_bytes(b"PK\x03\x04 fake")
        node = _make(
            "office_document",
            op="fill",
            file_path=str(tpl),
            values={"name": "张三"},
            output_path=str(tmp_path / "f.docx"),
            rollout_id="ro-5",
        )
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert outcomes[0]["outcome"]["signal"] == "task_success"

    def test_parse_returns_blocks(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        doc = tmp_path / "d.docx"
        doc.write_bytes(b"PK\x03\x04 fake")
        node = _make("office_document", op="parse", file_path=str(doc))
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert r.data["blocks"][0]["text"] == "周报"

    def test_no_rollout_id_skips_outcome_post(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        node = _make("office_document", op="write", markdown="# x", output_path=str(tmp_path / "a.docx"))
        assert _run(node).status == NodeStatus.SUCCESS
        assert outcomes == []


class TestSlidesNode:
    def test_write_markdown_deck(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        out = tmp_path / "deck.pptx"
        node = _make("office_slides", op="write", slides="# s1\n# s2", format="markdown", output_path=str(out))
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert out.exists() and out.read_bytes().startswith(b"pptx-bytes:")
        assert r.data["slide_count"] == 3

    def test_parse_deck(self, tmp_path, monkeypatch):
        outcomes = []
        _patch_transports(monkeypatch, office_app=_office_stub_app(outcomes))
        deck = tmp_path / "d.pptx"
        deck.write_bytes(b"PK\x03\x04 fake")
        node = _make("office_slides", op="parse", file_path=str(deck))
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert r.data["slide_count"] == 2


class TestArtifactNode:
    def test_create_registers_new_chain(self, tmp_path, monkeypatch):
        calls = []
        _patch_transports(monkeypatch, artifacts_app=_artifacts_stub_app(calls))
        node = _make("office_artifact", session_id="sess-1", name="周报", content="# 周报")
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert r.data["artifact_id"] == "art-1"
        assert r.data["version"] == 1
        assert r.data["method"] == "artifact.create"
        assert calls[0]["method"] == "artifact.create"
        assert calls[0]["params"]["session_id"] == "sess-1"

    def test_update_appends_version(self, monkeypatch):
        calls = []
        _patch_transports(monkeypatch, artifacts_app=_artifacts_stub_app(calls))
        node = _make(
            "office_artifact",
            session_id="sess-1",
            name="周报",
            artifact_id="art-1",
            content="# 周报 v2",
            change_log="revise",
        )
        r = _run(node)
        assert r.status == NodeStatus.SUCCESS
        assert r.data["version"] == 2
        assert calls[0]["method"] == "artifact.update"
        assert calls[0]["params"]["artifact_id"] == "art-1"

    def test_content_path_read(self, tmp_path, monkeypatch):
        calls = []
        _patch_transports(monkeypatch, artifacts_app=_artifacts_stub_app(calls))
        src = tmp_path / "report.md"
        src.write_text("# from file", encoding="utf-8")
        node = _make("office_artifact", session_id="s", name="n", content_path=str(src))
        assert _run(node).status == NodeStatus.SUCCESS
        assert calls[0]["params"]["content"] == "# from file"

    def test_missing_content_fails(self, monkeypatch):
        _patch_transports(monkeypatch, artifacts_app=_artifacts_stub_app([]))
        node = _make("office_artifact", session_id="s", name="n")
        r = _run(node)
        assert r.status == NodeStatus.FAILED

    def test_engine_error_maps_to_failure(self, monkeypatch):
        _patch_transports(monkeypatch, artifacts_app=_artifacts_stub_app([]))
        node = _make("office_artifact", session_id="s", name="n", content="x", artifact_type="bogus")
        r = _run(node)
        assert r.status == NodeStatus.FAILED
        assert "Invalid type" in (r.error or "")


# ── live E2E against the real office-api (skipped when :11492 down) ──


def _office_live() -> bool:
    try:
        r = httpx.get("http://127.0.0.1:11492/healthz", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _minimal_xlsx(path: Path) -> None:
    # handcrafted minimal xlsx (zip of OOXML parts) — enough for openpyxl
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>项目</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>工时</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>RAG</t></is></c><c r="B2"><v>8</v></c></row>'
        "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


@pytest.mark.slow
class TestLiveOfficeDag:
    def test_real_dag_xlsx_docx_outcome(self, tmp_path):
        if not _office_live():
            pytest.skip("office-api :11492 not healthy")
        xlsx = tmp_path / "weekly.xlsx"
        _minimal_xlsx(xlsx)

        node = NodeRegistry.get("office_spreadsheet")(
            "n1", NodeConfig(params={"file_path": str(xlsx), "rollout_id": "live-614"})
        )
        r = asyncio.run(node.execute({}))
        assert r.status == NodeStatus.SUCCESS, r.error
        assert r.data["sheet_count"] >= 1 and r.data["valid"] is True

        doc = NodeRegistry.get("office_document")(
            "n2",
            NodeConfig(
                params={
                    "op": "write",
                    "markdown": "# 周报\n\n## 本周进展\n- RAG 工时 8h（来自 weekly.xlsx）\n",
                    "output_path": str(tmp_path / "weekly.docx"),
                    "rollout_id": "live-614",
                }
            ),
        )
        r2 = asyncio.run(doc.execute({}))
        assert r2.status == NodeStatus.SUCCESS, r2.error
        produced = Path(r2.data["output_path"])
        assert produced.exists() and produced.stat().st_size > 1000
        assert r2.data["sha256"]

        sink = Path.home() / ".fusion-office" / "rollout-outcomes.jsonl"
        assert sink.exists(), "outcome sink missing"
        records = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line.strip()]
        mine = [x for x in records if x.get("rollout_id") == "live-614"]
        assert mine and mine[-1]["outcome"]["signal"] == "task_success"

    def test_live_artifact_registration(self, tmp_path, monkeypatch):
        if not _office_live():
            pytest.skip("office-api :11492 not healthy")
        import os

        if not os.environ.get("FUSION_ARTIFACTS_API_KEY"):
            pytest.skip("artifacts-engine API key not configured (fail-closed upstream)")
        doc = NodeRegistry.get("office_document")(
            "n1",
            NodeConfig(
                params={
                    "op": "write",
                    "markdown": "# 周报\n\noffice DAG artifact registration test\n",
                    "output_path": str(tmp_path / "a.docx"),
                }
            ),
        )
        assert asyncio.run(doc.execute({})).status == NodeStatus.SUCCESS
        node = NodeRegistry.get("office_artifact")(
            "n2",
            NodeConfig(
                params={
                    "session_id": "cowork-live-614",
                    "name": "office-dag-weekly",
                    "content": "# 周报\n\noffice DAG artifact registration test\n",
                }
            ),
        )
        r = asyncio.run(node.execute({}))
        assert r.status == NodeStatus.SUCCESS, r.error
        assert r.data["artifact_id"]
