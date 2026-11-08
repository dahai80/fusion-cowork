#!/usr/bin/env python3
"""n8n import coverage + verify CLI (issue #395).

Usage:
    python scripts/verify_n8n_import.py <n8n-export.json> [--mode abort_if_unsupported|mapped_subgraph] [--verify]

Prints the user-facing coverage report (runnable/total, warnings, unsupported
list with suggestions). With --verify, imports and executes the workflow
against a local HTTP stub — a green run upgrades the verification status to
"imported_verified" (never fake-honest: unverified imports stay "imported").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.n8n_import import import_n8n_workflow
from fusion_cowork.nodes import import_all_nodes

logger = logging.getLogger("fusion_cowork.n8n_verify")


async def _run_with_stub(wf_dict: dict) -> tuple[str, list, list]:
    """Execute the imported workflow against a local HTTP stub; return (status, steps, hits)."""
    import_all_nodes()

    hits: list = []

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
            hits.append(path)
            body = json.dumps({"status": "ok", "path": path}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    stub_base = f"http://127.0.0.1:{port}"
    try:
        # point untranslatable/external http urls at the stub for verification
        for node in wf_dict["nodes"]:
            params = node.get("config", {}).get("params", {})
            url = str(params.get("url", ""))
            if node.get("name") == "http_request" and (
                not url.startswith("http") or "{{" in url or ("${" not in url and "127.0.0.1" not in url)
            ):
                params["url"] = f"{stub_base}/{node['id']}"
        engine = WorkflowEngine()
        execution = await engine.execute(Workflow.from_dict(wf_dict))
        steps = [
            {"node_id": s.node_id, "node_name": s.node_name, "status": s.status.value, "error": s.error or ""}
            for s in execution.steps
        ]
        status = execution.status.value
        return status, steps, hits
    finally:
        server.close()
        await server.wait_closed()


def main() -> int:
    parser = argparse.ArgumentParser(description="n8n import coverage + verify")
    parser.add_argument("export", help="path to n8n workflow export JSON")
    parser.add_argument("--mode", default="abort_if_unsupported", choices=["abort_if_unsupported", "mapped_subgraph"])
    parser.add_argument("--verify", action="store_true", help="execute the imported workflow against a local stub")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    data = json.loads(Path(args.export).read_text())
    try:
        wf_dict, report, meta = import_n8n_workflow(data, mode=args.mode)
    except ValueError as e:
        print(f"IMPORT REJECTED: {e}")
        report = __import__("fusion_cowork.n8n_import", fromlist=["analyze_n8n_workflow"]).analyze_n8n_workflow(data)
        print(report.report_text())
        return 1

    print(report.report_text())
    print(f"imported workflow: {wf_dict['id']} ({len(wf_dict['nodes'])} nodes, {len(wf_dict['edges'])} edges)")
    print(f"verification status: {meta['verification_status']}")

    if not args.verify:
        return 0

    status, steps, hits = asyncio.run(_run_with_stub(wf_dict))
    print(f"verify run status: {status}")
    for s in steps:
        line = f"  {s['node_id']} ({s['node_name']}): {s['status']}"
        if s["error"]:
            line += f" — {s['error'][:120]}"
        print(line)
    print(f"stub hits: {hits}")
    if status == WorkflowStatus.SUCCESS.value:
        meta["verification_status"] = "imported_verified"
        print("verification status: imported_verified (green run)")
        return 0
    print("verification status: imported (run failed — status stays unverified)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
