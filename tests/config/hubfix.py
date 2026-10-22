"""Shared fixtures: build a throwaway hub directory for config/CLI/packs tests."""
from __future__ import annotations

import json
import os
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_PACKS = os.path.join(REPO_ROOT, "tests", "fixtures", "packs")
EXAMPLE = os.path.join(REPO_ROOT, "squadhq.example.yml")

MIN_YAML = """\
version: 1
org:
  name: Acme
  timezone: Europe/London
  working_hours: "07:00-20:00"
  approvers: ["@acme/leads"]
hub:
  repo: acme/agent-squad-hq
projects:
  - name: Storefront
    repo: acme/storefront
    packs: [node-next, python-fastapi]
    pack_roots: {node-next: web}
"""

WORKFLOW = """\
name: Daily Brief
on:
  schedule:
    - cron: "30 6 * * 1-5"  # squadhq:schedule:daily_brief
  workflow_dispatch: {}
jobs: {}
"""


def make_hub(tmp, *, packs=("node-next", "python-fastapi", "java-spring"), projects=None,
             workflows=None, config_text=None, flows=True):
    root = str(tmp)
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    os.makedirs(os.path.join(root, "packs"), exist_ok=True)
    for p in packs:
        shutil.copytree(os.path.join(FIXTURE_PACKS, p), os.path.join(root, "packs", p))
    if projects is not None:
        with open(os.path.join(root, "config", "projects.json"), "w") as f:
            json.dump({"projects": projects}, f)
    wf = os.path.join(root, ".github", "workflows")
    os.makedirs(wf, exist_ok=True)
    for name, text in (workflows if workflows is not None else {"daily-brief.yml": WORKFLOW}).items():
        with open(os.path.join(wf, name), "w") as f:
            f.write(text)
    if flows:
        with open(os.path.join(root, "config", "flows.yml"), "w") as f:
            f.write("labels:\n  dept:\n    'dept:backend': {color: 1D76DB, description: Backend}\n"
                    "  hub_only:\n    daily-brief: {color: ededed}\n  dynamic:\n    'project:<name>': {}\n")
    if config_text is not None:
        with open(os.path.join(root, "squadhq.yml"), "w") as f:
            f.write(config_text)
    return root
