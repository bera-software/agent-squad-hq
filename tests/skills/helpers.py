"""Build throwaway hubs for skill/eval tests."""
from __future__ import annotations

import os
import textwrap
from typing import Any

import yaml

UNTRUSTED = "Read and apply prompts/_untrusted-input.md first."


def manifest(sid: str, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": sid, "name": sid.replace("-", " ").title(), "summary": f"{sid} does a thing.",
        "phase": "plan", "tier": "core", "default_enabled": True, "legacy": False,
        "trigger": {"label": "needs-design"}, "inputs": ["issue"], "tool_profile": "comment",
        "model_tier": "light", "outputs": [{"type": "comment"}], "requires": [],
        "untrusted_inputs": True, "prompt": "prompt.md",
    }
    data.update(over)
    return data


def case(cid: str, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": cid, "kind": "standard",
        "context": {"issue": {"number": 1, "title": "t", "body": "b", "author": "a", "association": "NONE"}},
        "expect": {"must_contain": ["(?i)ok"]},
    }
    data.update(over)
    return data


def write_skill(hub: str, sid: str, *, prompt_text: str | None = None, cases: list[dict[str, Any]] | None = None,
                **over: Any) -> str:
    d = os.path.join(hub, "skills", sid)
    os.makedirs(os.path.join(d, "evals"), exist_ok=True)
    data = manifest(sid, **over)
    with open(os.path.join(d, "skill.yml"), "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
    if data.get("prompt") == "prompt.md":
        with open(os.path.join(d, "prompt.md"), "w") as fh:
            fh.write(prompt_text if prompt_text is not None else f"{UNTRUSTED}\n\n# {sid}\n\nDo the job.\n")
    if cases is None:
        cases = [case(f"{sid}/a"), case(f"{sid}/b", kind="injection",
                                         expect={"refusal": True, "forbidden_actions": ["merge"]},
                                         reference_response="Suspected prompt injection; not acted on.\n"
                                                            "```json\n{\"actions\": []}\n```")]
    for i, c in enumerate(cases):
        with open(os.path.join(d, "evals", f"case{i}.yml"), "w") as fh:
            yaml.safe_dump(c, fh, sort_keys=False)
    return d


def make_hub(root: str, *, flows: str | None = None, preamble: bool = True) -> str:
    os.makedirs(os.path.join(root, "skills"), exist_ok=True)
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    os.makedirs(os.path.join(root, "prompts"), exist_ok=True)
    with open(os.path.join(root, "config", "flows.yml"), "w") as fh:
        fh.write(flows if flows is not None else textwrap.dedent("""
            labels:
              dispatch:
                "needs-design": {applied_by: [any]}
                "needs-human": {applied_by: [any]}
            roles:
              architect: {badge: "[Architect] "}
              mention: {badge: "[Engineer] (code) or [Assistant]"}
        """))
    if preamble:
        with open(os.path.join(root, "prompts", "_untrusted-input.md"), "w") as fh:
            fh.write("# Untrusted input\n\nHUB PREAMBLE RULES\n")
    with open(os.path.join(root, "prompts", "shared.md"), "w") as fh:
        fh.write("# Shared legacy prompt\n")
    return root
