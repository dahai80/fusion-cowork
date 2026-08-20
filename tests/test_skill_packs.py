from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.skills import SkillPack, SkillRegistry, delete_skill_pack, list_skill_packs, save_skill_pack

import_all_nodes()


@pytest.fixture
def skills_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    return d


def test_save_and_list_skill_pack(skills_dir):
    pack = SkillPack(
        name="/my-clean",
        description="自定义清理",
        category="custom",
        type="node",
        node="desktop_clean",
        params={"organize_by_type": True},
        aliases=["我的清理"],
    )
    pdir = save_skill_pack(pack, skills_dir)
    assert (pdir / "skill.json").exists()
    packs = list_skill_packs(skills_dir)
    assert len(packs) == 1
    assert packs[0].name == "/my-clean"
    assert packs[0].node == "desktop_clean"
    assert "我的清理" in packs[0].aliases


def test_delete_skill_pack(skills_dir):
    pack = SkillPack(name="/to-delete", type="node", node="screen_capture")
    save_skill_pack(pack, skills_dir)
    assert delete_skill_pack("/to-delete", skills_dir) is True
    assert list_skill_packs(skills_dir) == []
    assert delete_skill_pack("/to-delete", skills_dir) is False


def test_invalid_skill_type_rejected(skills_dir):
    pack = SkillPack(name="/bad", type="bogus")
    with pytest.raises(ValueError):
        save_skill_pack(pack, skills_dir)


def test_register_user_packs_and_execute(skills_dir):
    pack = SkillPack(
        name="/echo-skill",
        description="echo node",
        type="node",
        node="shell_exec",
        params={"command": "echo pack_ok"},
        aliases=["回显"],
    )
    save_skill_pack(pack, skills_dir)
    from fusion_cowork.skills import register_user_packs

    registry = SkillRegistry()
    names = register_user_packs(registry, skills_dir)
    assert "/echo-skill" in names
    assert registry.get("/echo-skill") is not None
    assert registry.get("回显") is not None
    result = asyncio.run(registry.execute("/echo-skill"))
    assert isinstance(result, dict)
    assert "status" in result
