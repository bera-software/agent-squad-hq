"""The shipped catalogue, golden set and red-team set are valid and self-consistent."""
import os

from squadhq import paths
from squadhq.skills import catalogue, loader
from squadhq.skills.evals import harness
from squadhq.skills.schema import PHASES

HUB = paths.hub_root()


def test_catalogue_validates():
    report = loader.validate_catalogue(os.path.join(HUB, "skills"))
    assert report.errors == []
    assert report.skills >= 75


def test_every_phase_is_covered_and_prompts_exist():
    skills = loader.load_catalogue(os.path.join(HUB, "skills"))
    assert {s.phase for s in skills.values()} == set(PHASES)
    for s in skills.values():
        if not s.deterministic:
            assert s.prompt_path and os.path.isfile(s.prompt_path), s.id


def test_golden_and_redteam_sizes():
    golden = harness.collect_cases(hub=HUB, suite="golden")
    standard = [c for c in golden if c.kind == "standard"]
    injection = [c for c in golden if c.kind == "injection"]
    assert len(standard) >= 30
    assert {c.category for c in standard} >= {"bug", "feature", "refactor", "ambiguous"}
    assert len(injection) >= 15
    assert {c.skill for c in injection} >= {"product-owner", "engineer", "reviewer", "ecosystem-watch"}
    assert all(c.reference_response for c in injection)


def test_dry_run_meets_thresholds():
    cases = harness.collect_cases(hub=HUB)
    fake = harness.FakeProvider({c.id: c.reference_response for c in cases if c.reference_response})
    summary = harness.run(provider=fake, provider_ref="anthropic/claude-sonnet-5-5", hub=HUB, dry_run=True)
    thresholds = harness.load_thresholds(os.path.join(HUB, "evals", "thresholds.yml"))
    assert harness.apply_thresholds(summary, thresholds) == []
    assert summary.policy_violations == 0
    assert len(summary.scored) >= 15


def test_generated_catalogue_is_current():
    skills = loader.load_catalogue(os.path.join(HUB, "skills"))
    with open(os.path.join(HUB, "docs", "skills", "CATALOGUE.md"), encoding="utf-8") as fh:
        assert fh.read() == catalogue.render_markdown(skills)
