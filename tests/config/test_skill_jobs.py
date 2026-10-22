from squadhq.config import loader
from squadhq.config.loader import parse
from squadhq.config.skill_jobs import skill_job_tiers

from .test_loader import doc


def _skill(root, name, body):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "skill.yml").write_text(body)


def test_skill_job_tiers(tmp_path):
    assert skill_job_tiers(str(tmp_path)) is None
    _skill(tmp_path, "a11y", "id: a11y\nmodel_tier: light\nrole: designer\n")
    _skill(tmp_path, "arch", "id: arch\nmodel_tier: heavy\nrole: designer\n")
    _skill(tmp_path, "zz-det", "id: zz-det\nmodel_tier: none\n")
    _skill(tmp_path, "broken", "id: [\n")
    _skill(tmp_path, "list", "- 1\n")
    (tmp_path / "skills" / "nomanifest").mkdir()
    assert skill_job_tiers(str(tmp_path)) == {"a11y": "light", "arch": "heavy", "designer": "heavy"}


def test_skill_jobs_default_and_override(tmp_path):
    _skill(tmp_path, "a11y", "id: a11y\nmodel_tier: light\n")
    _skill(tmp_path, "review", "id: review\nmodel_tier: light\n")  # core job wins
    ctx = loader.hub_context(str(tmp_path))
    cfg, issues = parse(doc(), **ctx)
    assert cfg.providers.jobs["a11y"] == "light" and cfg.providers.jobs["review"] == "heavy"
    assert cfg.providers.jobs["quarterly-review"] == "heavy"
    d = doc(providers={"anthropic": {}, "jobs": {"a11y": "heavy"}})
    cfg, issues = parse(d, **ctx)
    assert cfg.providers.jobs["a11y"] == "heavy"
    assert not [i for i in issues if i.path == "providers.jobs.a11y"]
