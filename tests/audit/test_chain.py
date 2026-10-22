from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from typing import Any

import pytest

from squadhq.audit import GENESIS_HASH, AuditEvent, canonical_json, compute_hash, export_jsonl, read_jsonl, verify_chain
from squadhq.audit import verify_store
from squadhq.audit.__main__ import main as audit_main
from squadhq.audit.chain import seal
from squadhq.store import open_store


def _chain(n: int) -> list[AuditEvent]:
    out, prev = [], GENESIS_HASH
    for i in range(1, n + 1):
        ev = seal(id=f"e{i}", seq=i, ts=f"2026-10-21T00:00:0{i}.000000Z", actor="lead", action="approval.approved",
                  subject=f"acme/a:pr#{i}", data={"n": i, "ünï": "çödé"}, prev_hash=prev)
        out.append(ev)
        prev = ev.hash
    return out


def test_hash_definition_is_reproducible() -> None:
    ev = _chain(1)[0]
    expected = hashlib.sha256((GENESIS_HASH + canonical_json(ev.body())).encode()).hexdigest()
    assert ev.hash == expected == compute_hash(GENESIS_HASH, ev.body())
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == '{"a":[1,{"c":3,"d":2}],"b":1}'


def test_intact_chain_verifies() -> None:
    r = verify_chain(_chain(5))
    assert r.ok and r.checked == 5 and r.head_hash == _chain(5)[-1].hash
    assert verify_chain([]).ok


@pytest.mark.parametrize("tamper,reason", [
    (lambda evs: evs.__setitem__(2, dataclasses.replace(evs[2], data={"n": 999})), "hash mismatch"),
    (lambda evs: evs.__setitem__(2, dataclasses.replace(evs[2], actor="mallory")), "hash mismatch"),
    (lambda evs: evs.pop(2), "sequence gap"),
    (lambda evs: evs.insert(2, dataclasses.replace(evs[2])), "sequence gap"),
    (lambda evs: evs.__setitem__(slice(1, 3), [evs[2], evs[1]]), "sequence gap"),
])
def test_tampering_is_detected(tamper: Any, reason: str) -> None:
    evs = _chain(5)
    tamper(evs)
    r = verify_chain(evs)
    assert not r.ok and reason in r.reason
    assert r.first_bad_seq is not None


def test_rehashed_forgery_breaks_the_link() -> None:
    evs = _chain(4)
    forged = seal(id="e2", seq=2, ts=evs[1].ts, actor="mallory", action="x", subject="s", data={},
                  prev_hash=evs[0].hash)  # attacker recomputes event 2's own hash...
    evs[1] = forged
    r = verify_chain(evs)  # ...but event 3 still points at the original
    assert not r.ok and r.first_bad_seq == 3 and "prev_hash" in r.reason


def test_suffix_verification() -> None:
    evs = _chain(5)
    assert verify_chain(evs[2:], start_hash=evs[1].hash, start_seq=3).ok
    assert not verify_chain(evs[2:]).ok


def test_jsonl_roundtrip() -> None:
    buf = io.StringIO()
    assert export_jsonl(_chain(3), buf) == 3
    back = list(read_jsonl(io.StringIO(buf.getvalue() + "\n\n")))
    assert back == _chain(3)
    assert verify_chain(back).ok


def test_store_chain_and_cli(tmp_path: Any, capsys: Any) -> None:
    url = f"sqlite:///{tmp_path}/hq.db"
    s = open_store(url)
    for i in range(4):
        s.append_audit("api", "token.issued", f"token:{i}", {"role": "viewer", "f": 1.5})
    assert verify_store(s).ok
    s.close()

    assert audit_main(["verify", "--db", url]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == 4
    out = tmp_path / "audit.jsonl"
    assert audit_main(["export", "--db", url, "--out", str(out), "--since", "1"]) == 0
    assert len(out.read_text().splitlines()) == 3
    assert audit_main(["export", "--db", url]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 4

    full = tmp_path / "full.jsonl"
    audit_main(["export", "--db", url, "--out", str(full)])
    assert audit_main(["verify", "--file", str(full)]) == 0
    lines = full.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["data"]["role"] = "admin"  # privilege escalation hidden in the log
    lines[1] = json.dumps(rec)
    full.write_text("\n".join(lines))
    capsys.readouterr()
    assert audit_main(["verify", "--file", str(full)]) == 2
    assert json.loads(capsys.readouterr().out)["first_bad_seq"] == 2


def test_cli_uses_env_db(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("SQUADHQ_DB_URL", f"sqlite:///{tmp_path}/env.db")
    assert audit_main(["verify"]) == 0
