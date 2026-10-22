import os

import pytest

from squadhq.skills import loader
from tests.skills.helpers import case, make_hub, write_skill


@pytest.fixture
def hub(tmp_path):
    return make_hub(str(tmp_path))


def validate(hub):
    return loader.validate_catalogue(os.path.join(hub, "skills"))


def test_valid_catalogue(hub):
    write_skill(hub, "alpha")
    write_skill(hub, "beta", requires=["alpha"])
    report = validate(hub)
    assert report.errors == []
    assert report.skills == 2
    assert report.eval_cases == 4
    assert report.ok()
    skills = loader.load_catalogue(os.path.join(hub, "skills"))
    assert sorted(skills) == ["alpha", "beta"]
    assert skills["beta"].to_dict()["triggers"] == ["label needs-design"]
    assert skills["alpha"].labels() == {"needs-design"}


def test_missing_root(tmp_path):
    report = loader.validate_catalogue(str(tmp_path / "nope"))
    assert "skills directory not found" in report.errors[0]
    assert loader.skill_dirs(str(tmp_path / "nope")) == []


def test_id_mismatch_and_schema_error(hub):
    write_skill(hub, "alpha", id="other")
    write_skill(hub, "beta", phase="nope")
    report = validate(hub)
    assert any("does not match directory" in e for e in report.errors)
    assert any("phase must be" in e for e in report.errors)


def test_dir_without_manifest_and_bad_yaml(hub):
    os.makedirs(os.path.join(hub, "skills", "orphan"))
    d = os.path.join(hub, "skills", "broken")
    os.makedirs(d)
    with open(os.path.join(d, "skill.yml"), "w") as fh:
        fh.write("id: [unclosed\n")
    report = validate(hub)
    assert any("orphan: directory has no skill.yml" in e for e in report.errors)
    assert any("broken" in e for e in report.errors)


def test_prompt_problems(hub):
    write_skill(hub, "noprompt")
    os.remove(os.path.join(hub, "skills", "noprompt", "prompt.md"))
    write_skill(hub, "escape", prompt="../../../etc/passwd")
    write_skill(hub, "nopreamble", prompt_text="# no preamble\n")
    write_skill(hub, "shared", prompt="../../prompts/shared.md")
    report = validate(hub)
    assert any("noprompt: prompt file not found" in e for e in report.errors)
    assert any("escape: prompt" in e and "outside the hub" in e for e in report.errors)
    assert any("nopreamble: prompt does not start" in e for e in report.errors)
    assert any("shared: prompt does not start" in w and "shared prompt" in w for w in report.warnings)


def test_eval_count_and_bad_case(hub):
    write_skill(hub, "few", cases=[case("few/a")])
    write_skill(hub, "bad", cases=[case("bad/a", expect={"must_contain": ["("]}), case("bad/b")])
    write_skill(hub, "nojection", cases=[case("nojection/a"), case("nojection/b")])
    report = validate(hub)
    assert any("few: needs 2-5 eval cases (has 1)" in e for e in report.errors)
    assert any("bad regex" in e for e in report.errors)
    assert any("nojection: reads untrusted input but has no injection" in w for w in report.warnings)


def test_duplicate_eval_ids_and_wrong_skill(hub):
    write_skill(hub, "alpha", cases=[case("dup/a"), case("dup/a")])
    write_skill(hub, "beta", cases=[case("beta/a", skill="alpha"), case("beta/b")])
    report = validate(hub)
    assert any("duplicate eval id 'dup/a'" in e for e in report.errors)
    assert any("eval 'beta/a' names skill 'alpha'" in e for e in report.errors)


def test_requires_unknown_and_cycle(hub):
    write_skill(hub, "alpha", requires=["beta"])
    write_skill(hub, "beta", requires=["alpha"])
    write_skill(hub, "gamma", requires=["ghost"])
    report = validate(hub)
    assert any("requires unknown skill 'ghost'" in e for e in report.errors)
    assert any("requires cycle" in e for e in report.errors)


def test_labels_roles_and_badges(hub):
    write_skill(hub, "alpha", outputs=[{"type": "label", "labels": ["made-up", "security:approved",
                                                                      "project:web"]}])
    write_skill(hub, "beta", role="architect", badge="[Designer] ")
    write_skill(hub, "gamma", role="ghost-role", badge="[Ghost] ")
    write_skill(hub, "delta", role="mention", badge="[Engineer] ")
    report = validate(hub)
    assert any("label 'made-up'" in w for w in report.warnings)
    assert not any("security:approved" in w or "project:web" in w for w in report.warnings)
    assert any("beta: badge" in w for w in report.warnings)
    assert any("role 'ghost-role'" in w for w in report.warnings)
    assert not any(w.startswith("delta") for w in report.warnings)
    assert report.ok() and not report.ok(strict=True)


def test_unreadable_flows(tmp_path):
    hub = make_hub(str(tmp_path))
    os.remove(os.path.join(hub, "config", "flows.yml"))
    write_skill(hub, "alpha", outputs=[{"type": "label", "labels": ["anything"]}])
    report = validate(hub)
    assert report.warnings == []
    assert loader.flows_labels(os.path.join(hub, "config", "flows.yml")) is None
    assert loader.flows_roles(os.path.join(hub, "config", "flows.yml")) is None


def test_deterministic_entrypoint_and_config(hub):
    det = {"deterministic": True, "model_tier": "none", "prompt": None, "untrusted_inputs": False}
    write_skill(hub, "sweeper", entrypoint="python -m squadhq.nonexistent_mod", cases=[], **det)
    write_skill(hub, "uptime-watcher", entrypoint="workflow:x.yml#job", cases=[], **det)
    report = validate(hub)
    assert any("entrypoint module squadhq.nonexistent_mod not found" in e for e in report.errors)
    assert any("uptime-watcher: config block is required" in e for e in report.errors)


def test_uptime_config_validated(hub):
    det = {"deterministic": True, "model_tier": "none", "prompt": None, "untrusted_inputs": False}
    write_skill(hub, "uptime-watcher", entrypoint="workflow:x", cases=[], config={"checks": []}, **det)
    report = validate(hub)
    assert any("uptime-watcher: config: checks must be a non-empty list" in e for e in report.errors)


def test_golden_cases_validated(hub):
    write_skill(hub, "alpha")
    det = {"deterministic": True, "model_tier": "none", "prompt": None, "untrusted_inputs": False}
    write_skill(hub, "robot", entrypoint="workflow:x", cases=[], **det)
    golden = os.path.join(hub, "evals", "golden")
    os.makedirs(golden)
    import yaml
    with open(os.path.join(golden, "g.yml"), "w") as fh:
        yaml.safe_dump({"cases": [case("g/1", skill="alpha"), case("g/2", skill="ghost"),
                                  case("g/3", skill="robot"), case("alpha/a", skill="alpha")]}, fh)
    with open(os.path.join(golden, "bad.yml"), "w") as fh:
        fh.write("cases: nope\n")
    report = validate(hub)
    assert any("targets unknown skill 'ghost'" in e for e in report.errors)
    assert any("targets deterministic skill 'robot'" in e for e in report.errors)
    assert any("duplicate eval id 'alpha/a'" in e for e in report.errors)
    assert any("bad.yml" in e for e in report.errors)


def test_get_skill_and_load_errors(hub):
    write_skill(hub, "alpha")
    root = os.path.join(hub, "skills")
    assert loader.get_skill("alpha", root).id == "alpha"
    with pytest.raises(loader.SkillError, match="unknown skill"):
        loader.get_skill("ghost", root)
    write_skill(hub, "beta", tier="platinum")
    with pytest.raises(loader.SkillError, match="tier must be"):
        loader.load_catalogue(root)


def test_duplicate_ids_in_catalogue(hub):
    write_skill(hub, "alpha")
    write_skill(hub, "alpha-copy", id="alpha")
    with pytest.raises(loader.SkillError, match="duplicate skill id"):
        loader.load_catalogue(os.path.join(hub, "skills"))
    report = validate(hub)
    assert any("does not match directory" in e for e in report.errors)


def test_manifest_must_be_mapping(hub):
    d = os.path.join(hub, "skills", "listy")
    os.makedirs(d)
    with open(os.path.join(d, "skill.yml"), "w") as fh:
        fh.write("- a\n- b\n")
    with pytest.raises(loader.SkillError, match="must be a mapping"):
        loader.read_manifest(d)


def test_deterministic_has_no_prompt(hub):
    det = {"deterministic": True, "model_tier": "none", "prompt": None, "untrusted_inputs": False}
    write_skill(hub, "robot", entrypoint="workflow:x", cases=[], **det)
    skill = loader.get_skill("robot", os.path.join(hub, "skills"))
    assert skill.prompt_path is None
    with pytest.raises(loader.SkillError, match="no prompt"):
        skill.read_prompt()


def test_skills_root_uses_hub_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path))
    assert loader.skills_root() == os.path.join(str(tmp_path), "skills")
