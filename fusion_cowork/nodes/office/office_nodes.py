# issue #614: office DAG node family — the Agentic Office plan §2.2 executor
# leg. Each node is one structured office operation against fusion-office
# office-api (:11492) plus outcome reporting to /v1/outcomes (§4.1 signal
# convention: task_success / user_edited / error) with the gateway rollout_id.
# office_artifact registers outputs into the artifacts-engine version chain
# (JSON-RPC artifact.create / artifact.update). Lazy imports + httpx AsyncClient;
# office-api / artifacts-engine are upstream services — this file only calls them.

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

import httpx

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)

_DEFAULT_OFFICE_API = "http://127.0.0.1:11492"
_DEFAULT_ARTIFACTS = "http://127.0.0.1:11451"
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# §4.1 outcome signals mirrored from office-api SIGNAL_SCORES
_SIGNAL_SUCCESS = "task_success"
_SIGNAL_EDITED = "user_edited"
_SIGNAL_ERROR = "error"


def _office_base(params: Dict[str, Any]) -> str:
    return (params.get("office_api_url") or os.environ.get("FUSION_OFFICE_API_URL") or _DEFAULT_OFFICE_API).rstrip("/")


def _artifacts_base(params: Dict[str, Any]) -> str:
    return (params.get("artifacts_url") or os.environ.get("FUSION_ARTIFACTS_URL") or _DEFAULT_ARTIFACTS).rstrip("/")


async def _emit_outcome(base: str, rollout_id: str, signal: str, summary: str) -> None:
    # best-effort: outcome reporting must never fail the node itself
    try:
        record = {
            "schema_version": 1,
            "rollout_id": rollout_id,
            "model": "fusion-cowork-office-node",
            "messages": [
                {"role": "user", "content": "office DAG node execution"},
                {"role": "assistant", "content": summary[:2000] or signal},
            ],
            "outcome": {"signal": signal},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{base}/v1/outcomes", json=record)
        logger.info(f"[office] outcome recorded rollout={rollout_id} signal={signal} status={resp.status_code}")
    except Exception as e:
        logger.warning(f"[office] outcome reporting failed (ignored): {e}")


def _read_file(file_path: str, max_bytes: int = _MAX_UPLOAD_BYTES) -> bytes:
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {file_path}")
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"file too large: {len(data)} bytes (max {max_bytes})")
    return data


def _write_output(output_path: str, data: bytes, fallback_name: str) -> str:
    # empty output_path -> ~/.fusion-cowork/office/ (same home convention as
    # the demand store's default_db_path)
    path = Path(output_path).expanduser() if output_path else Path.home() / ".fusion-cowork" / "office" / fallback_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


class OfficeBaseNode(BaseNode):
    # shared outcome wiring: emit error on failure, task_success / user_edited
    # on success (fill leaving unfilled placeholders means human editing is
    # still needed). Subclasses call _finish / _fail.
    category = NodeCategory.FUSION_ECOSYSTEM

    async def _finish(self, params: Dict[str, Any], result: NodeResult, edited: bool = False) -> NodeResult:
        rollout_id = str(params.get("rollout_id") or "")
        if rollout_id:
            await _emit_outcome(
                _office_base(params),
                rollout_id,
                _SIGNAL_EDITED if edited else _SIGNAL_SUCCESS,
                result.summary or "",
            )
        return result

    async def _fail(self, params: Dict[str, Any], error: str, summary: str) -> NodeResult:
        rollout_id = str(params.get("rollout_id") or "")
        if rollout_id:
            await _emit_outcome(_office_base(params), rollout_id, _SIGNAL_ERROR, summary)
        logger.warning(f"[office] {self.name} failed: {error}")
        return NodeResult(status=NodeStatus.FAILED, error=error, summary=summary)


@register_node
class OfficeSpreadsheetNode(OfficeBaseNode):
    # office.spreadsheet — xlsx parse via /v1/xlsx/parse; parse validity is the
    # validator leg (invalid workbooks 422 from office-api -> FAILED + error outcome)
    name = "office_spreadsheet"
    display_name = "办公表格"  # i18n: user-facing
    description = "Parse an xlsx workbook via office-api /v1/xlsx/parse; invalid files fail visibly"
    icon = "sheet"
    default_label = "表格解析"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "xlsx file path"},
                "office_api_url": {
                    "type": "string",
                    "description": "office-api base (empty = FUSION_OFFICE_API_URL)",
                    "default": "",
                },
                "rollout_id": {"type": "string", "description": "gateway rollout_id for /v1/outcomes", "default": ""},
            },
            "required": ["file_path"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = {**(self.config.params or {}), **inputs}
        file_path = str(params.get("file_path") or "")
        if not file_path:
            return await self._fail(params, "file_path is required", "missing file_path")
        try:
            data = _read_file(file_path)
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_office_base(params)}/v1/xlsx/parse",
                    files={
                        "file": (
                            Path(file_path).name,
                            data,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
            if resp.status_code != 200:
                return await self._fail(
                    params, f"office-api xlsx/parse {resp.status_code}: {resp.text[:300]}", "workbook invalid"
                )
            parsed = resp.json()
            sheets = parsed.get("sheets") or []
            result = NodeResult(
                status=NodeStatus.SUCCESS,
                data={"sheets": sheets, "sheet_count": len(sheets), "valid": True},
                summary=f"parsed {len(sheets)} sheets from {Path(file_path).name}",
            )
            return await self._finish(params, result)
        except Exception as e:
            return await self._fail(params, str(e), "spreadsheet parse error")


@register_node
class OfficeDocumentNode(OfficeBaseNode):
    # office.document — docx operations: write (markdown -> docx), fill
    # (template + values), parse (docx -> blocks)
    name = "office_document"
    display_name = "办公文档"  # i18n: user-facing
    description = "docx via office-api: write (markdown->docx), fill (template+values), parse"
    icon = "doc"
    default_label = "文档处理"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["write", "fill", "parse"], "default": "write"},
                "file_path": {"type": "string", "description": "docx path (fill/parse input)"},
                "markdown": {"type": "string", "description": "markdown source (write)"},
                "values": {"type": "object", "description": "placeholder replacements (fill)"},
                "output_path": {"type": "string", "description": "where to save the produced docx"},
                "office_api_url": {"type": "string", "default": ""},
                "rollout_id": {"type": "string", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = {**(self.config.params or {}), **inputs}
        op = str(params.get("op") or "write")
        base = _office_base(params)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                if op == "write":
                    markdown = str(params.get("markdown") or "")
                    if not markdown.strip():
                        return await self._fail(params, "markdown is required for op=write", "empty markdown")
                    resp = await client.post(f"{base}/v1/docx/write", data={"markdown": markdown})
                elif op == "fill":
                    file_path = str(params.get("file_path") or "")
                    values = params.get("values")
                    if not file_path or not isinstance(values, dict):
                        return await self._fail(
                            params, "file_path + values(object) required for op=fill", "bad fill params"
                        )
                    resp = await client.post(
                        f"{base}/v1/docx/fill",
                        data={"values": json.dumps(values, ensure_ascii=False)},
                        files={
                            "file": (
                                Path(file_path).name,
                                _read_file(file_path),
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            )
                        },
                    )
                elif op == "parse":
                    file_path = str(params.get("file_path") or "")
                    if not file_path:
                        return await self._fail(params, "file_path is required for op=parse", "missing file_path")
                    resp = await client.post(
                        f"{base}/v1/docx/parse",
                        files={
                            "file": (
                                Path(file_path).name,
                                _read_file(file_path),
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            )
                        },
                    )
                else:
                    return await self._fail(params, f"unknown op: {op}", "bad op")
            if resp.status_code != 200:
                return await self._fail(
                    params, f"office-api docx/{op} {resp.status_code}: {resp.text[:300]}", f"docx {op} rejected"
                )
            if op == "parse":
                result = NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"blocks": resp.json().get("blocks") or []},
                    summary="parsed docx blocks",
                )
                return await self._finish(params, result)
            sha = resp.headers.get("X-Office-Doc-SHA256", "")
            unfilled = [p for p in resp.headers.get("X-Office-Unfilled", "").split(",") if p]
            out_path = _write_output(str(params.get("output_path") or ""), resp.content, "document.docx")
            # unfilled placeholders remain -> human editing still needed (§4.1 user_edited)
            edited = op == "fill" and bool(unfilled)
            result = NodeResult(
                status=NodeStatus.SUCCESS,
                data={"output_path": out_path, "sha256": sha, "unfilled": unfilled},
                summary=f"docx {op} -> {out_path} ({len(resp.content)} bytes, unfilled={len(unfilled)})",
            )
            return await self._finish(params, result, edited=edited)
        except Exception as e:
            return await self._fail(params, str(e), f"docx {op} error")


@register_node
class OfficeSlidesNode(OfficeBaseNode):
    # office.slides — pptx operations: parse (deck -> slides) and write
    # (markdown or ops JSON -> deck, optional base file for ops mode)
    name = "office_slides"
    display_name = "办公幻灯片"  # i18n: user-facing
    description = "pptx via office-api: parse deck, or write deck from markdown/ops JSON"
    icon = "slides"
    default_label = "幻灯片处理"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["write", "parse"], "default": "write"},
                "file_path": {"type": "string", "description": "pptx path (parse input; ops-mode base deck)"},
                "slides": {"type": "string", "description": "markdown or JSON ops array (write)"},
                "format": {"type": "string", "enum": ["markdown", "ops"], "default": "markdown"},
                "output_path": {"type": "string", "description": "where to save the produced pptx"},
                "office_api_url": {"type": "string", "default": ""},
                "rollout_id": {"type": "string", "default": ""},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = {**(self.config.params or {}), **inputs}
        op = str(params.get("op") or "write")
        base = _office_base(params)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                if op == "parse":
                    file_path = str(params.get("file_path") or "")
                    if not file_path:
                        return await self._fail(params, "file_path is required for op=parse", "missing file_path")
                    resp = await client.post(
                        f"{base}/v1/pptx/parse",
                        files={
                            "file": (
                                Path(file_path).name,
                                _read_file(file_path),
                                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            )
                        },
                    )
                    if resp.status_code != 200:
                        return await self._fail(
                            params, f"office-api pptx/parse {resp.status_code}: {resp.text[:300]}", "deck invalid"
                        )
                    slides = resp.json().get("slides") or []
                    result = NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={"slides": slides, "slide_count": len(slides)},
                        summary=f"parsed {len(slides)} slides",
                    )
                    return await self._finish(params, result)
                if op != "write":
                    return await self._fail(params, f"unknown op: {op}", "bad op")
                slides = str(params.get("slides") or "")
                if not slides.strip():
                    return await self._fail(params, "slides content is required for op=write", "empty slides")
                fmt = str(params.get("format") or "markdown")
                file_path = str(params.get("file_path") or "")
                files = {"slides": (None, slides)}
                if file_path and Path(file_path).expanduser().is_file():
                    files["file"] = (
                        Path(file_path).name,
                        _read_file(file_path),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                resp = await client.post(f"{base}/v1/pptx/write", data={"format": fmt}, files=files)
                if resp.status_code != 200:
                    return await self._fail(
                        params, f"office-api pptx/write {resp.status_code}: {resp.text[:300]}", "deck write rejected"
                    )
                out_path = _write_output(str(params.get("output_path") or ""), resp.content, "deck.pptx")
                result = NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={
                        "output_path": out_path,
                        "sha256": resp.headers.get("X-Office-Doc-SHA256", ""),
                        "slide_count": int(resp.headers.get("X-Office-Slide-Count", "0") or 0),
                    },
                    summary=f"pptx write -> {out_path} ({len(resp.content)} bytes)",
                )
                return await self._finish(params, result)
        except Exception as e:
            return await self._fail(params, str(e), f"pptx {op} error")


@register_node
class OfficeArtifactNode(OfficeBaseNode):
    # office.artifact — register output into the artifacts-engine version chain:
    # artifact.create for a new chain, artifact.update (new version) when
    # artifact_id is given. Content comes inline or from a produced file.
    name = "office_artifact"
    display_name = "成果入库"  # i18n: user-facing
    description = "Register/append content into artifacts-engine version chain (JSON-RPC artifact.create/update)"
    icon = "archive"
    default_label = "成果入库"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "artifacts-engine session id"},
                "name": {"type": "string", "description": "artifact name"},
                "content": {"type": "string", "description": "inline content (or use content_path)"},
                "content_path": {
                    "type": "string",
                    "description": "read content from this file (e.g. a produced markdown)",
                },
                "artifact_id": {
                    "type": "string",
                    "description": "existing artifact id -> append a new version",
                    "default": "",
                },
                "artifact_type": {
                    "type": "string",
                    "enum": ["code", "markdown", "html", "react", "svg", "mermaid", "data"],
                    "default": "markdown",
                },
                "kind": {
                    "type": "string",
                    "enum": ["app", "code", "document", "game", "tool", "template"],
                    "default": "document",
                },
                "change_log": {"type": "string", "default": ""},
                "artifacts_url": {
                    "type": "string",
                    "description": "artifacts-engine base (empty = FUSION_ARTIFACTS_URL)",
                    "default": "",
                },
                "office_api_url": {"type": "string", "default": ""},
                "rollout_id": {"type": "string", "default": ""},
            },
            "required": ["session_id", "name"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = {**(self.config.params or {}), **inputs}
        session_id = str(params.get("session_id") or "")
        name = str(params.get("name") or "")
        if not session_id or not name:
            return await self._fail(params, "session_id and name are required", "missing identity params")
        content = str(params.get("content") or "")
        content_path = str(params.get("content_path") or "")
        if not content and content_path:
            try:
                content = Path(content_path).expanduser().read_text(encoding="utf-8")
            except Exception as e:
                return await self._fail(params, f"cannot read content_path: {e}", "content read error")
        if not content:
            return await self._fail(params, "content or content_path is required", "empty content")
        artifact_id = str(params.get("artifact_id") or "")
        if artifact_id:
            method = "artifact.update"
            rpc_params = {
                "artifact_id": artifact_id,
                "content": content,
                "change_log": str(params.get("change_log") or "office DAG update"),
                "source": "manual",
            }
        else:
            method = "artifact.create"
            rpc_params = {
                "session_id": session_id,
                "name": name,
                "type": str(params.get("artifact_type") or "markdown"),
                "kind": str(params.get("kind") or "document"),
                "content": content,
                "summary": str(params.get("change_log") or "")[:200],
                "change_log": str(params.get("change_log") or "Initial version"),
            }
        headers: Dict[str, str] = {}
        api_key = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")
        if api_key:
            headers["X-API-Key"] = api_key
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _artifacts_base(params),
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": rpc_params},
                    headers=headers,
                )
            body = resp.json() if resp.status_code == 200 else {}
            if resp.status_code != 200 or body.get("error"):
                detail = body.get("error", {}).get("message", resp.text[:200])
                return await self._fail(
                    params, f"artifacts-engine {method} failed: {detail}", "artifact registration rejected"
                )
            result_data = body.get("result", {})
            artifact = result_data.get("artifact", {})
            version = result_data.get("version", {})
            result = NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "artifact_id": artifact.get("id", artifact_id),
                    "version": version.get("version"),
                    "content_hash": version.get("content_hash", ""),
                    "method": method,
                },
                summary=f"{method} ok: {artifact.get('id', artifact_id)} v{version.get('version')}",
            )
            return await self._finish(params, result)
        except Exception as e:
            return await self._fail(params, str(e), "artifact registration error")
