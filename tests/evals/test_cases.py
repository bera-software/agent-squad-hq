import pytest
import yaml

from squadhq.skills.evals import cases as C
from tests.skills.helpers import case


def test_validate_case_good_and_bad():
    assert C.validate_case(case("x/1")) == []
    assert C.validate_case("nope") == ["case must be a mapping"]
    bad = {"id": "", "kind": "weird", "category": "zzz", "context": [], "reference_response": 3,
           "surprise": 1, "expect": {"must_contain": ["("], "forbidden_actions": ["teleport", "add_label:"],
                                    "refusal_pattern": "(", "json_schema": [], "max_cost_usd": -1,
                                    "refusal": "y", "exfil_markers": "evil", "huh": 1}}
    e = "\n".join(C.validate_case(bad))
    for needle in ("unknown keys ['surprise']", "id is required", "kind must be", "category must be",
                   "context must be", "reference_response must be", "expect: unknown keys", "bad regex",
                   "unknown action 'teleport'", "unknown action 'add_label:'", "refusal_pattern",
                   "json_schema must be", "max_cost_usd must be", "expect.refusal must be",
                   "exfil_markers must be a list"):
        assert needle in e, needle
    assert "expect must be a mapping" in C.validate_case({"id": "a", "context": {}, "expect": 1})
    assert "expect has no checks" in C.validate_case({"id": "a", "context": {}, "expect": {"max_cost_usd": 1}})
    assert any("injection cases need" in x for x in C.validate_case(
        {"id": "a", "kind": "injection", "context": {}, "expect": {"must_contain": ["x"]}}))


def test_validate_forbidden():
    assert C.validate_forbidden("merge")
    assert C.validate_forbidden("add_label:risk:low")
    assert C.validate_forbidden("remove_label:needs-design")
    assert not C.validate_forbidden("rm -rf")


def test_parse_single_and_list(tmp_path):
    single = C.parse_cases(case("s/1"), source="f.yml", default_skill="alpha")
    assert single[0].skill == "alpha" and single[0].category == "other"
    multi = C.parse_cases({"skill": "beta", "cases": [case("m/1"), case("m/2", skill="gamma", kind="injection",
                                                                        expect={"refusal": True})]},
                          source="g.yml")
    assert [c.skill for c in multi] == ["beta", "gamma"]
    assert multi[1].category == "injection" and multi[1].expect.refusal
    with pytest.raises(C.CaseError, match="expected a mapping"):
        C.parse_cases([1], source="x")
    with pytest.raises(C.CaseError, match="non-empty list"):
        C.parse_cases({"cases": []}, source="x")
    with pytest.raises(C.CaseError, match="no skill"):
        C.parse_cases(case("n/1"), source="x")
    with pytest.raises(C.CaseError, match=r"x\[0\]: id is required"):
        C.parse_cases({"skill": "a", "cases": [{"context": {}, "expect": {"refusal": True}}]}, source="x")


def test_load_case_file_and_listing(tmp_path):
    d = tmp_path / "evals" / "sub"
    d.mkdir(parents=True)
    (d / "a.yml").write_text(yaml.safe_dump(case("a/1")))
    (tmp_path / "evals" / "b.yaml").write_text(yaml.safe_dump(case("b/1")))
    (tmp_path / "evals" / "notes.txt").write_text("x")
    files = C.case_files(str(tmp_path / "evals"))
    assert [f.rsplit("/", 1)[1] for f in files] == ["b.yaml", "a.yml"]
    assert C.load_case_file(files[0], default_skill="z")[0].id == "b/1"
    (tmp_path / "bad.yml").write_text("id: [oops\n")
    with pytest.raises(C.CaseError):
        C.load_case_file(str(tmp_path / "bad.yml"))
    assert C.case_files(str(tmp_path / "missing")) == []
