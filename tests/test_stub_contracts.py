"""Every project stub calls a hub reusable workflow with only the secrets and
inputs that workflow declares (GitHub rejects undeclared ones at run time)."""
import glob
import os
import re

import pytest
import yaml

from squadhq.paths import _SOURCE_ROOT as ROOT

STUBS = sorted(glob.glob(os.path.join(ROOT, "onboarding", "*.stub"))
               + glob.glob(os.path.join(ROOT, "onboarding", "optional", "*.stub")))
USES = re.compile(r"^__HUB_REPO__/\.github/workflows/([\w.-]+\.ya?ml)@")


def _on(doc):
    # PyYAML (YAML 1.1) reads the bare key `on` as True.
    return doc.get("on", doc.get(True)) or {}


def _calls():
    for stub in STUBS:
        with open(stub, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        for name, job in (doc.get("jobs") or {}).items():
            m = USES.match(str(job.get("uses", "")))
            if m:
                yield pytest.param(stub, job, m.group(1), id=f"{os.path.basename(stub)}:{name}")


CALLS = list(_calls())


def test_stubs_call_hub_workflows():
    assert {os.path.basename(c.values[0]) for c in CALLS} >= {"agents.yml.stub", "squadhq-agent.yml.stub"}


@pytest.mark.parametrize(("stub", "job", "workflow"), CALLS)
def test_stub_passes_only_declared_secrets_and_inputs(stub, job, workflow):
    with open(os.path.join(ROOT, ".github", "workflows", workflow), encoding="utf-8") as f:
        call = _on(yaml.safe_load(f)).get("workflow_call")
    assert isinstance(call, dict), f"{workflow} is not a reusable workflow"
    for kind, passed in (("secrets", job.get("secrets") or {}), ("inputs", job.get("with") or {})):
        declared = call.get(kind) or {}
        missing = sorted(set(passed) - set(declared))
        assert not missing, f"{os.path.basename(stub)} passes {kind} {missing} not declared by {workflow}"
        required = sorted(k for k, v in declared.items() if (v or {}).get("required") and k not in passed)
        assert not required, f"{os.path.basename(stub)} omits required {kind} {required} of {workflow}"
