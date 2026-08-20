from __future__ import annotations

import asyncio
import shutil

import pytest

from fusion_cowork.code.lsp import (
    _hover_contents,
    _language_id,
    _normalize_locations,
    _uri_to_path,
    query,
)
from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


def _has_server():
    return any(shutil.which(s) for s in ("pylsp", "pyright-langserver", "pyrefly"))


def test_normalize_locations():
    assert _normalize_locations(None) == []
    assert _normalize_locations([]) == []
    locs = _normalize_locations([{"uri": "file:///a/b.py", "range": {"start": {"line": 1, "character": 2}}}])
    assert locs == [{"path": "/a/b.py", "line": 1, "character": 2, "end_line": 1, "end_character": 2}]
    single = _normalize_locations({"uri": "file:///x.py", "range": {"start": {"line": 0, "character": 0}}})
    assert len(single) == 1
    assert single[0]["path"] == "/x.py"


def test_hover_contents():
    assert _hover_contents("plain") == "plain"
    assert _hover_contents({"value": "typed"}) == "typed"
    assert _hover_contents([{"value": "a"}, {"value": "b"}]) == "a\nb"


def test_language_id():
    assert _language_id("/x/foo.py") == "python"
    assert _language_id("/x/foo.ts") == "typescript"
    assert _language_id("/x/foo.rs") == "rust"
    assert _language_id("/x/foo.unknown") == "plaintext"


def test_uri_to_path():
    assert _uri_to_path("file:///tmp/a.py") == "/tmp/a.py"
    assert _uri_to_path("not-a-uri") == "not-a-uri"


def test_query_no_server_degrades(tmp_path):
    if _has_server():
        pytest.skip("LSP server installed; skip degrade test")
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    result = asyncio.run(query("hover", str(f), 0, 0))
    assert "error" in result
    assert "LSP server" in result["error"]


def test_lsp_node_no_server_degrades(tmp_path):
    if _has_server():
        pytest.skip("LSP server installed; skip degrade test")
    node = NodeRegistry.create("lsp", config=NodeConfig(params={"action": "hover", "path": str(tmp_path / "x.py")}))
    r = asyncio.run(node.execute({}))
    assert r.status.value == "failed"
    assert "LSP server" in r.error


@pytest.mark.skipif(not _has_server(), reason="no LSP server installed")
def test_lsp_real_hover(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("value = 42\nprint(value)\n", encoding="utf-8")
    result = asyncio.run(query("hover", str(f), 0, 0, root=str(tmp_path)))
    assert "error" not in result
    assert "hover" in result
