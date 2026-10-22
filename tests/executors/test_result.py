import json

import pytest

from squadhq.executors import result as res
from squadhq.interfaces import ErrorClass, ExecutionResult


def test_round_trip(tmp_path):
    r = ExecutionResult(executor="codex", role="review", model="openai/gpt-5", outcome="failure",
                        error_class=ErrorClass.TRANSIENT, error_message="429", cost_usd=0.5,
                        input_tokens=10, output_tokens=2, turns=1, duration_s=3.5,
                        capacity_tripped=False, transcript_path="/tmp/x", extra={"k": (1, 2), "o": object()})
    p = tmp_path / "r.json"
    res.write_result(r, p)
    data = json.loads(p.read_text())
    assert data["error_class"] == "transient" and data["schema_version"] == "1"
    assert data["extra"]["k"] == [1, 2] and isinstance(data["extra"]["o"], str)
    back = res.read_result(p)
    assert back.error_class is ErrorClass.TRANSIENT
    assert back.cost_usd == 0.5 and back.turns == 1 and back.transcript_path == "/tmp/x"


def test_from_dict_tolerant():
    r = res.from_dict({"outcome": "weird", "error_class": "nope", "turns": True, "cost_usd": "1",
                       "future_field": 7})
    assert r.outcome == "failure"
    assert r.error_class is ErrorClass.UNKNOWN
    assert r.turns is None and r.cost_usd is None
    assert r.extra["future_field"] == 7
    assert r.transcript_path is None


def test_read_result_rejects_non_object(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        res.read_result(p)
