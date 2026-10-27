"""Hub loops reach every model provider in Actions mode: hub-agent.yml declares and maps each
secret of squadhq.config.registry.HUB_LOOP_SECRETS, and every caller passes all of them."""
import glob
import os

import pytest
import yaml

from squadhq.config import loader
from squadhq.config.registry import HUB_LOOP_SECRETS, PROVIDER_SECRETS
from squadhq.paths import _SOURCE_ROOT as ROOT

WF = os.path.join(ROOT, ".github", "workflows")


def _load(name):
    with open(os.path.join(WF, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_hub_agent_declares_and_maps_every_provider_secret():
    wf = _load("hub-agent.yml")
    declared = set(wf[True]["workflow_call"]["secrets"])
    assert set(HUB_LOOP_SECRETS) <= declared
    run = next(s for s in wf["jobs"]["run"]["steps"] if "agent_runner" in str(s.get("run", "")))
    for name in HUB_LOOP_SECRETS:
        assert run["env"][name] == f"${{{{ secrets.{name} }}}}", name


CALLERS = sorted(os.path.basename(p) for p in glob.glob(os.path.join(WF, "*.yml"))
                 if "./.github/workflows/hub-agent.yml" in open(p, encoding="utf-8").read())


@pytest.mark.parametrize("name", CALLERS)
def test_every_caller_passes_every_provider_secret(name):
    for job in _load(name)["jobs"].values():
        if job.get("uses") == "./.github/workflows/hub-agent.yml":
            passed = job.get("secrets") or {}
            missing = [s for s in HUB_LOOP_SECRETS if passed.get(s) != f"${{{{ secrets.{s} }}}}"]
            assert not missing, (name, missing)


def test_default_provider_secrets_reach_hub_loops():
    for name, by_auth in PROVIDER_SECRETS.items():
        if "api" in by_auth:
            assert by_auth["api"] in HUB_LOOP_SECRETS, name


def test_custom_secret_names_warn_in_actions_mode():
    text = ("version: 1\norg: {name: a, approvers: ['@a']}\nhub: {repo: a/h}\n"
            "providers:\n  openai: {secret: MY_OPENAI}\n"
            "  routing:\n    heavy: {primary: openai/gpt-5}\n    light: {primary: openai/gpt-5-mini}\n")
    cfg, issues = loader.load_text(text)
    assert cfg is not None
    assert any(i.level == "warning" and i.path == "providers.openai.secret" for i in issues)
