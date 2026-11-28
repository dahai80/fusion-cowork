import json

import pytest
from click.testing import CliRunner

from fusion_cowork.cli import cli


def _seed_manifest(plugins_dir, name, version="1.0.0"):
    pdir = plugins_dir / name
    pdir.mkdir()
    manifest = {
        "name": name,
        "version": version,
        "description": "test plugin",
        "author": "tester",
        "nodes": ["node_a", "node_b"],
    }
    (pdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture()
def fresh_loader(tmp_path, monkeypatch):
    # Fresh PluginLoader on a temp dir, injected past the process singleton so the
    # test never reads the real ~/.fusion-cowork/plugins or cached loader state
    import fusion_cowork.plugins as plugins_pkg
    from fusion_cowork.plugins import loader as loader_mod

    d = tmp_path / "plugins"
    d.mkdir()
    inst = loader_mod.PluginLoader(plugins_dir=str(d))
    monkeypatch.setattr(plugins_pkg, "get_plugin_loader", lambda plugins_dir="": inst)
    return d


def test_plugin_list_lists_manifest_without_raising(fresh_loader):
    _seed_manifest(fresh_loader, "demo-plugin")
    runner = CliRunner()
    result = runner.invoke(cli, ["plugin", "list"])
    assert result.exit_code == 0, result.output
    assert "demo-plugin" in result.output
    assert "1.0.0" in result.output


def test_plugin_list_empty_dir_reports_none(fresh_loader):
    runner = CliRunner()
    result = runner.invoke(cli, ["plugin", "list"])
    assert result.exit_code == 0, result.output
