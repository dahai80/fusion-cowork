"""issue #395: n8n import-only v1 — analyzer + expression translator + importer.

- Coverage report over 3 real-world-shaped samples (one proving the
  unsupported rejection path)
- Expression translator contract tests over the shared fixture pack
  (tests/contract/fixtures/n8n_expressions/cases.json)
- Import-and-verify: 2 imported workflows execute end-to-end green inside
  the engine against a local HTTP stub, with run status/steps asserted
"""

import asyncio
import json
from pathlib import Path

import pytest

from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.n8n_import import (
    N8nImportReport,
    UnsupportedExpression,
    analyze_n8n_workflow,
    import_n8n_workflow,
    translate_expression_value,
)
from fusion_cowork.nodes import import_all_nodes

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "fixtures" / "n8n_samples"
EXPR_FIXTURE = ROOT / "contract" / "fixtures" / "n8n_expressions" / "cases.json"


def _sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


# node registration is import-time side-effect only — engine needs all nodes
import_all_nodes()


class TestExpressionTranslator:
    _cases = json.loads(EXPR_FIXTURE.read_text())

    @pytest.mark.parametrize("case", _cases["cases"], ids=lambda c: c["name"])
    def test_translation_cases(self, case):
        out = translate_expression_value(case["input"], case["upstream"], case["node_ids"])
        assert out == case["expected"]

    @pytest.mark.parametrize("case", _cases["error_cases"], ids=lambda c: c["name"])
    def test_error_cases_raise(self, case):
        with pytest.raises(UnsupportedExpression):
            translate_expression_value(case["input"], case["upstream"], case["node_ids"])

    def test_shared_fixture_declares_contract(self):
        data = self._cases
        assert data["_contract"]
        assert len(data["cases"]) >= 5 and len(data["error_cases"]) >= 5


class TestAnalyzerCoverage:
    def test_sample1_all_mappable(self):
        report = analyze_n8n_workflow(_sample("lead_enrichment.json"))
        assert isinstance(report, N8nImportReport)
        assert report.runnable == 3 and report.total == 3
        assert report.unsupported == []
        assert "3/3 nodes runnable" in report.report_text()

    def test_sample2_rejection_path(self):
        report = analyze_n8n_workflow(_sample("order_sync_unsupported.json"))
        unsup = {v.name for v in report.unsupported}
        assert unsup == {"Transform JS", "Notify Slack"}
        text = report.report_text()
        # every unsupported node listed with suggestion, never silently dropped
        assert "Transform JS" in text and "Notify Slack" in text
        assert "rewrite as a Set node" in text
        assert "no v1 mapping" in text

    def test_sample3_cron_and_if_warning(self):
        report = analyze_n8n_workflow(_sample("nightly_report.json"))
        assert report.cron == "0 2 * * *"
        warn_types = {v.type for v in report.warnings}
        assert any("if" in t for t in warn_types)

    def test_analyzer_flags_untranslatable_expressions(self):
        data = _sample("lead_enrichment.json")
        data["nodes"][1]["parameters"]["url"] = "{{ $json.x.toLowerCase() }}"
        report = analyze_n8n_workflow(data)
        v = next(v for v in report.verdicts if v.name == "Fetch Lead")
        assert v.expression_issues and "toLowerCase" in v.expression_issues[0]


class TestImporter:
    def test_sample1_import_shape(self):
        wf, report, meta = import_n8n_workflow(_sample("lead_enrichment.json"))
        assert wf["id"] == "n8n_lead_enrichment"
        names = [n["name"] for n in wf["nodes"]]
        assert names == ["http_request", "python_repl"]
        # trigger edge is implicit (start) — only Fetch->Build edge is real
        assert len(wf["edges"]) == 1
        assert meta["verification_status"] == "imported"
        assert meta["dropped_nodes"] == []
        # 0903 canvas schema roundtrip — no parallel graph format
        parsed = Workflow.from_dict(wf)
        assert len(parsed.nodes) == 2 and len(parsed.edges) == 1

    def test_sample2_abort_mode_raises(self):
        with pytest.raises(ValueError) as ei:
            import_n8n_workflow(_sample("order_sync_unsupported.json"), mode="abort_if_unsupported")
        assert "unsupported nodes" in str(ei.value)
        assert "Transform JS" in str(ei.value)

    def test_sample2_mapped_subgraph_rewires(self):
        wf, report, meta = import_n8n_workflow(_sample("order_sync_unsupported.json"), mode="mapped_subgraph")
        ids = [n["id"] for n in wf["nodes"]]
        assert set(ids) == {"fetch_config", "post_order"}
        post = next(n for n in wf["nodes"] if n["id"] == "post_order")
        # $node["Fetch Config"].json.target translated against the id map
        assert post["config"]["params"]["url"] == "${step.fetch_config.json.target}"
        assert meta["dropped_nodes"] == ["Transform JS", "Notify Slack"]
        edge_pairs = {(e["source_id"], e["target_id"]) for e in wf["edges"]}
        assert ("fetch_config", "post_order") in edge_pairs

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            import_n8n_workflow(_sample("lead_enrichment.json"), mode="export")

    def test_sample3_cron_metadata(self):
        wf, report, meta = import_n8n_workflow(_sample("nightly_report.json"))
        assert meta["cron"] == "0 2 * * *"
        route = next(n for n in wf["nodes"] if n["config"]["label"] == "Route")
        assert route["name"] == "python_repl"
        assert "left" in route["config"]["params"]["code"]


class TestIfOperandTranslation:
    # review B1: IF operands must be expression-translated (engine resolves
    # ${step...} refs at run time), never embedded as static literals
    def test_dynamic_operand_translated(self):
        wf, _r, _m = import_n8n_workflow(_sample("nightly_report.json"))
        route = next(n for n in wf["nodes"] if n["config"]["label"] == "Route")
        code = route["config"]["params"]["code"]
        assert "left_0 = '${step.fetch_stats.json.status}'" in code
        assert "right_0 = 'ok'" in code
        assert 'result = "pass" if cond_0 else "drop"' in code

    def test_static_operands_stay_literal(self):
        wf, _r, _m = import_n8n_workflow(_sample("nightly_report.json"))
        route = next(n for n in wf["nodes"] if n["config"]["label"] == "Route")
        assert "'ok'" in route["config"]["params"]["code"]
        assert "{{" not in route["config"]["params"]["code"]

    # review B2: n8n contains = left-contains-right = python `right in left`
    def test_contains_operand_order(self):
        data = {
            "name": "contains check",
            "nodes": [
                {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
                {"name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {"url": "http://x/y"}},
                {
                    "name": "Check",
                    "type": "n8n-nodes-base.if",
                    "parameters": {
                        "conditions": {
                            "left": "{{ $json.body.status }}",
                            "operation": "contains",
                            "right": "PROBLEM",
                        }
                    },
                },
            ],
            "connections": {
                "Start": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]},
                "Fetch": {"main": [[{"node": "Check", "type": "main", "index": 0}]]},
            },
        }
        wf, _r, _m = import_n8n_workflow(data)
        code = wf["nodes"][-1]["config"]["params"]["code"]
        assert "cond_0 = right_0 in left_0" in code
        assert "left_0 in right_0" not in code
        assert "left_0 = '${step.fetch.json.body.status}'" in code

    def test_v2_conditions_shape_and_combinator(self):
        # realworld zabbix v3 shape: leftValue with "=" prefix, operator dict
        data = {
            "name": "v2 if",
            "nodes": [
                {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
                {"name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {"url": "http://x/y"}},
                {
                    "name": "Gate",
                    "type": "n8n-nodes-base.if",
                    "parameters": {
                        "conditions": {
                            "options": {"version": 3},
                            "combinator": "or",
                            "conditions": [
                                {
                                    "leftValue": "={{ $json.body.status }}",
                                    "rightValue": "PROBLEM",
                                    "operator": {"type": "string", "operation": "equals"},
                                },
                                {
                                    "leftValue": "={{ $json.count }}",
                                    "rightValue": 0,
                                    "operator": {"type": "number", "operation": "gt"},
                                },
                            ],
                        }
                    },
                },
            ],
            "connections": {
                "Start": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]},
                "Fetch": {"main": [[{"node": "Gate", "type": "main", "index": 0}]]},
            },
        }
        wf, _r, _m = import_n8n_workflow(data)
        code = wf["nodes"][-1]["config"]["params"]["code"]
        assert "left_0 = '${step.fetch.json.body.status}'" in code
        assert "cond_0 = left_0 == right_0" in code
        assert "cond_1 = left_1 > right_1" in code
        assert 'result = "pass" if (cond_0) or (cond_1) else "drop"' in code

    def test_unsupported_operator_flagged_not_silent(self):
        data = {
            "name": "bad op",
            "nodes": [
                {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
                {"name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {"url": "http://x/y"}},
                {
                    "name": "Gate",
                    "type": "n8n-nodes-base.if",
                    "parameters": {
                        "conditions": {
                            "conditions": [
                                {
                                    "leftValue": "={{ $json.x }}",
                                    "rightValue": "",
                                    "operator": {"type": "array", "operation": "exists", "singleValue": True},
                                }
                            ]
                        }
                    },
                },
            ],
            "connections": {
                "Start": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]},
                "Fetch": {"main": [[{"node": "Gate", "type": "main", "index": 0}]]},
            },
        }
        wf, report, _m = import_n8n_workflow(data)
        code = wf["nodes"][-1]["config"]["params"]["code"]
        assert "cond_0 = left_0 is not None" in code

    def test_set_values_translated(self):
        data = {
            "name": "set translate",
            "nodes": [
                {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
                {"name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {"url": "http://x/y"}},
                {
                    "name": "Build",
                    "type": "n8n-nodes-base.set",
                    "parameters": {
                        "assignments": {
                            "assignments": [
                                {"name": "target", "value": "={{ $json.body.target }}"},
                                {"name": "label", "value": "static"},
                            ]
                        }
                    },
                },
            ],
            "connections": {
                "Start": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]},
                "Fetch": {"main": [[{"node": "Build", "type": "main", "index": 0}]]},
            },
        }
        wf, _r, _m = import_n8n_workflow(data)
        code = wf["nodes"][-1]["config"]["params"]["code"]
        assert "result['target'] = '${step.fetch.json.body.target}'" in code
        assert "result['label'] = 'static'" in code


REALWORLD = SAMPLES / "realworld"


class TestRealworldExports:
    # genuine n8n community exports (downloaded 2026-11-08 from GitHub):
    # - xavier-maugendre/n8n-zabbix-glpi-prometheus-agent-qdrant-offline workflow.json
    # - Xavi1995/n8n_execution_report Auditor.json
    def test_zabbix_ticket_offline_coverage(self):
        report = analyze_n8n_workflow(json.loads((REALWORLD / "zabbix_ticket_offline.json").read_text()))
        assert report.total == 31
        assert len(report.verdicts) == 31  # every node classified, none silent
        unsup_types = {v.type for v in report.unsupported}
        assert "n8n-nodes-base.code" in unsup_types
        assert "@n8n/n8n-nodes-langchain.vectorStoreQdrant" in unsup_types
        assert "@n8n/n8n-nodes-langchain.embeddingsOllama" in unsup_types
        # stickyNote is canvas documentation, never "unsupported"
        assert not any(v.type == "n8n-nodes-base.stickyNote" for v in report.unsupported)
        # every observed type is declared — no generic "no v1 mapping" fallback
        assert "declare it here" not in report.report_text()

    def test_zabbix_agent_maps_to_llm_http(self):
        report = analyze_n8n_workflow(json.loads((REALWORLD / "zabbix_ticket_offline.json").read_text()))
        agent = next(v for v in report.verdicts if v.type == "@n8n/n8n-nodes-langchain.agent")
        assert agent.verdict == "mappable_with_warning"
        assert agent.target == "http_request"

    def test_auditor_coverage(self):
        report = analyze_n8n_workflow(json.loads((REALWORLD / "auditor.json").read_text()))
        assert report.total == 25
        assert len(report.verdicts) == 25
        unsup_types = {v.type for v in report.unsupported}
        assert {
            "n8n-nodes-base.code",
            "n8n-nodes-base.splitInBatches",
            "n8n-nodes-base.aggregate",
            "n8n-nodes-base.convertToFile",
            "n8n-nodes-base.n8n",
        } <= unsup_types
        # every observed type is declared — no generic "no v1 mapping" fallback
        assert "declare it here" not in report.report_text()

    def test_auditor_abort_mode_rejects(self):
        with pytest.raises(ValueError) as ei:
            import_n8n_workflow(json.loads((REALWORLD / "auditor.json").read_text()))
        assert "unsupported nodes" in str(ei.value)


class _StubHTTPServer:
    # minimal asyncio raw-socket HTTP stub — enough for http_request nodes
    def __init__(self):
        self.server = None
        self.port = 0
        self.hits = []

    async def start(self):
        async def handle(reader, writer):
            try:
                request_line = await reader.readline()
                headers = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = line.decode().partition(":")
                    headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length", "0") or "0")
                if length:
                    await reader.read(length)
                path = request_line.split()[1].decode() if request_line else "/"
                self.hits.append(path)
                body = json.dumps({"status": "ok", "path": path, "target": "http://stub/next"}).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class TestImportAndVerify:
    async def _run_green(self, wf_dict):
        engine = WorkflowEngine()
        execution = await engine.execute(Workflow.from_dict(wf_dict))
        return execution

    async def test_two_imported_workflows_execute_green(self):
        # acceptance: at least 2 imported workflows execute end-to-end green
        server = _StubHTTPServer()
        await server.start()
        try:
            wf1, _r1, meta1 = import_n8n_workflow(_sample("lead_enrichment.json"))
            fetch = next(n for n in wf1["nodes"] if n["name"] == "http_request")
            fetch["config"]["params"]["url"] = f"{server.base}/lead/1"
            ex1 = await self._run_green(wf1)
            assert ex1.status == WorkflowStatus.SUCCESS, ex1.error
            assert len(ex1.steps) == 2
            assert "/lead/1" in server.hits
            meta1["verification_status"] = "imported_verified"

            wf2, _r2, meta2 = import_n8n_workflow(_sample("order_sync_unsupported.json"), mode="mapped_subgraph")
            # sample's $env.CFG url is declared untranslatable (analyzer flags
            # it) — verification overrides both urls with the stub
            fetch2 = next(n for n in wf2["nodes"] if n["id"] == "fetch_config")
            fetch2["config"]["params"]["url"] = f"{server.base}/config"
            post = next(n for n in wf2["nodes"] if n["id"] == "post_order")
            post["config"]["params"]["url"] = f"{server.base}/orders"
            ex2 = await self._run_green(wf2)
            assert ex2.status == WorkflowStatus.SUCCESS, ex2.error
            assert "/orders" in server.hits
            meta2["verification_status"] = "imported_verified"

            # status honesty: verified only after a green run
            assert meta1["verification_status"] == meta2["verification_status"] == "imported_verified"
        finally:
            await server.stop()

    async def test_unverified_import_keeps_imported_status(self):
        _wf, _report, meta = import_n8n_workflow(_sample("nightly_report.json"))
        assert meta["verification_status"] == "imported"
