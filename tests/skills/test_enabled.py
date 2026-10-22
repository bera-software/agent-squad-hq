import os

import pytest

from squadhq.skills import enabled, loader
from tests.skills.helpers import make_hub, write_skill


@pytest.fixture
def skills(tmp_path):
    hub = make_hub(str(tmp_path))
    write_skill(hub, "threat-modeler")
    write_skill(hub, "growth", default_enabled=False)
    write_skill(hub, "pro-thing", tier="pro", default_enabled=True)
    write_skill(hub, "needs-growth", requires=["growth"])
    return loader.load_catalogue(os.path.join(hub, "skills"))


def test_defaults(skills):
    res = enabled.resolve(skills, {})
    assert res["threat-modeler"].enabled and res["threat-modeler"].reason == "default"
    assert not res["growth"].enabled
    assert res["needs-growth"].missing_requires == ["growth"]


def test_switches_and_precedence(skills):
    cfg = {
        "agents": {"threat_modeler": {"enabled": False}, "growth": True},
        "skills": {"threat-modeler": True, "growth": {"enabled": "maybe"}},
        "projects": [{"name": "Web", "repo": "acme/web", "agents": {"growth": {"enabled": False}}}],
    }
    res = enabled.resolve(skills, cfg)
    assert res["threat-modeler"].enabled and res["threat-modeler"].reason == "skills"
    assert res["growth"].enabled and res["growth"].reason == "agents"
    assert res["needs-growth"].missing_requires == []
    res = enabled.resolve(skills, cfg, project="acme/web")
    assert not res["growth"].enabled and res["growth"].reason == "project.agents"


def test_edition_gate(skills):
    res = enabled.resolve(skills, {"edition": "core", "skills": {"pro-thing": True}})
    assert not res["pro-thing"].enabled and res["pro-thing"].reason == "needs pro edition"
    with pytest.raises(ValueError, match="edition"):
        enabled.resolve(skills, {"edition": "gold"})


def test_unknown_project(skills):
    with pytest.raises(ValueError, match="not found"):
        enabled.resolve(skills, {"projects": ["junk"]}, project="ghost")


def test_load_config(tmp_path):
    p = tmp_path / "squadhq.yml"
    p.write_text("agents: {growth: {enabled: true}}\n")
    assert enabled.load_config(str(p))["agents"]["growth"]["enabled"] is True
    p.write_text("- 1\n")
    with pytest.raises(ValueError):
        enabled.load_config(str(p))
