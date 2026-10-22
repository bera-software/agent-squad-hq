from __future__ import annotations

from squadhq.trackers import status_maps as sm


def test_defaults():
    assert sm.native_to_labels("jira", "in progress") == ["status:in-progress"]
    assert sm.native_to_labels("jira", "Unknown") == []
    assert sm.native_to_labels("jira", None) == []
    assert sm.label_to_native("linear", "status:in-review") == "In Review"
    assert sm.label_to_native("github", "status:in-review") is None
    assert sm.label_to_native("trello", "backend") is None
    assert sm.candidate_states("trello", "status:in-progress") == ["In Progress", "Doing"]
    assert sm.split_labels("jira", ["a", "needs-human"]) == (["needs-human"], ["a"])
    assert sm.special_state("jira", "done") == "Done"
    assert sm.special_state("github", "done") is None
    assert sm.match_name(["Doing"], ["doing", "x"]) == "doing"
    assert sm.match_name(["nope"], ["x"]) is None


def test_overrides_and_reverse_lookup_fallback():
    ov = {"states": {"Peer Review": ["status:in-review"]}, "label_to_state": {"needs-human": "On Hold"}, "done": "Shipped"}
    assert sm.native_to_labels("jira", "peer review", ov) == ["status:in-review"]
    assert sm.label_to_native("jira", "needs-human", ov) == "On Hold"
    assert sm.special_state("jira", "done", ov) == "Shipped"
    ov2 = {"states": {"QA": ["qa"]}}
    assert sm.label_to_native("jira", "qa", ov2) == "QA"


def test_yaml_ships_as_package_data():
    from importlib import resources

    assert resources.files("squadhq.trackers").joinpath("status_maps.yml").is_file()
