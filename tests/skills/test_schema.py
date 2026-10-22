from squadhq.skills import schema
from tests.skills.helpers import manifest


def errs(**over):
    return schema.validate_manifest(manifest("demo-skill", **over))


def test_valid_manifest_has_no_errors():
    assert errs() == []


def test_not_a_mapping():
    assert schema.validate_manifest([1, 2]) == ["manifest must be a mapping"]


def test_bad_id_and_enums():
    e = errs(id="Bad_ID", phase="nope", tier="gold", tool_profile="root", model_tier="huge")
    joined = "\n".join(e)
    for needle in ("kebab-case", "phase must", "tier must", "tool_profile must", "model_tier must"):
        assert needle in joined


def test_unknown_key_and_missing_fields():
    data = manifest("x")
    data["surprise"] = 1
    del data["name"]
    data["default_enabled"] = "yes"
    e = schema.validate_manifest(data)
    assert any("unknown keys" in x for x in e)
    assert "name is required" in e
    assert "default_enabled must be true or false" in e


def test_trigger_rules():
    assert any("exactly one" in x for x in errs(trigger={"label": "a", "command": "b"}))
    assert any("exactly one" in x for x in errs(trigger={}))
    assert any("manual must be true" in x for x in errs(trigger={"manual": "yes"}))
    assert any("event must look like" in x for x in errs(trigger={"event": "Pull Request"}))
    assert any("only valid with an event or label" in x for x in errs(trigger={"command": "x", "filter": "y"}))
    assert any("unknown keys" in x for x in errs(trigger={"label": "x", "when": "y"}))
    assert any("non-empty string" in x for x in errs(trigger={"schedule": ""}))
    assert any("must be a mapping" in x for x in errs(trigger=["label"]))
    assert any("trigger list is empty" in x for x in errs(trigger=[]))
    assert any("trigger is required" in x for x in errs(trigger="label x"))
    assert errs(trigger=[{"event": "support.ticket.created", "filter": "x"}, {"manual": True}]) == []


def test_trigger_summary():
    assert schema.trigger_summary({"command": "threat-model"}) == "command @squad threat-model"
    assert schema.trigger_summary({"manual": True}) == "manual"
    assert schema.trigger_summary({"event": "pull_request", "filter": "paths x"}) == "event pull_request (paths x)"
    assert schema.trigger_summary({}) == "?"
    assert schema.normalise_triggers("x") == []


def test_inputs_and_outputs():
    assert any("unknown input" in x for x in errs(inputs=["issue", "telepathy"]))
    assert any("non-empty list" in x for x in errs(inputs=[]))
    assert any("needs a 'path'" in x for x in errs(outputs=[{"type": "artifact"}]))
    assert any("'labels' list" in x for x in errs(outputs=[{"type": "label", "labels": []}]))
    assert any("type must be" in x for x in errs(outputs=[{"type": "email"}]))
    assert any("must be a mapping" in x for x in errs(outputs=["comment"]))
    assert any("unknown keys" in x for x in errs(outputs=[{"type": "comment", "size": 1}]))
    assert any("outputs must be" in x for x in errs(outputs=[]))


def test_profile_output_consistency():
    assert any("'pr' output needs" in x for x in errs(outputs=[{"type": "pr"}], tool_profile="comment"))
    assert errs(outputs=[{"type": "pr"}], tool_profile="edit-test-push") == []
    assert any("read-only skills" in x for x in errs(tool_profile="read-only", outputs=[{"type": "comment"}]))
    assert errs(tool_profile="read-only", outputs=[{"type": "json"}]) == []


def test_gate_rules():
    assert errs(gate={"check": "squadhq/x", "condition": "always"}) == []
    assert errs(gate={"verdict": "blocking: yes", "condition": "always"}) == []
    assert any("gate needs" in x for x in errs(gate={"condition": "c"}))
    assert any("condition is required" in x for x in errs(gate={"check": "x"}))
    assert any("unknown keys" in x for x in errs(gate={"check": "x", "condition": "c", "y": 1}))
    assert any("gate must be a mapping" in x for x in errs(gate="x"))


def test_requires_and_badge():
    assert any("cannot require itself" in x for x in errs(requires=["demo-skill"]))
    assert any("list of skill ids" in x for x in errs(requires="x"))
    assert any("badge must look like" in x for x in errs(badge="Architect"))
    assert errs(badge="[Architect] ") == []
    assert any("role must be" in x for x in errs(role=""))
    assert any("legacy must be" in x for x in errs(legacy="no"))


def test_deterministic_rules():
    ok = errs(deterministic=True, model_tier="none", prompt=None, entrypoint="python -m squadhq.x")
    assert ok == []
    e = errs(deterministic=True, model_tier="light", prompt=3)
    assert any("model_tier: none" in x for x in e)
    assert any("need an entrypoint" in x for x in e)
    assert any("path or null" in x for x in e)
    assert any("only valid for deterministic" in x for x in errs(model_tier="none"))
    assert any("prompt is required" in x for x in errs(prompt=None))
    assert any("config must be" in x for x in errs(config=[1]))
