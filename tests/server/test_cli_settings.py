from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from squadhq.server import auth
from squadhq.server.__main__ import main
from squadhq.server.observability import JSONFormatter, Registry, configure_logging, request_id_var, tracing
from squadhq.server.settings import ServerSettings
from tests.server.conftest import make_settings


def _settings(tmp_path: Any, **kw: Any) -> ServerSettings:
    return make_settings(db_url=f"sqlite:///{tmp_path}/hq.db", **kw)


def test_migrate_and_create_token(tmp_path: Any, capsys: Any) -> None:
    s = _settings(tmp_path)
    assert main(["migrate"], s) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 3
    assert main(["create-token", "--role", "approver", "--name", "lead"], s) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["role"] == "approver" and out["token"].startswith("sqh_")
    from squadhq.store import open_store

    st = open_store(s.db_url)
    [rec] = st.list_tokens()
    assert rec.token_hash == auth.hash_token(out["token"]) and rec.token_hash != out["token"]
    assert main(["create-token", "--role", "viewer", "--print-only"], s) == 0
    po = json.loads(capsys.readouterr().out)
    assert auth.parse_static_tokens(po["env_entry"])[0].role == "viewer"
    with pytest.raises(SystemExit):
        main(["create-token", "--role", "root"], s)


def test_hash_token(tmp_path: Any, capsys: Any, monkeypatch: Any) -> None:
    assert main(["hash-token", "abc"], _settings(tmp_path)) == 0
    assert capsys.readouterr().out.strip() == auth.hash_token("abc")
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("abc\n"))
    main(["hash-token", "-"], _settings(tmp_path))
    assert capsys.readouterr().out.strip() == auth.hash_token("abc")


def test_check(tmp_path: Any, capsys: Any) -> None:
    assert main(["check"], _settings(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []
    bad = _settings(tmp_path, webhook_secrets={}, github_token="")
    assert main(["check"], bad) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert len(problems) == 2


def test_worker_once_and_scheduler(tmp_path: Any, capsys: Any) -> None:
    s = _settings(tmp_path)
    assert main(["scheduler", "--list"], s) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["timezone"] == "Europe/London" and listed["schedules"][0]["key"] == "daily_brief"
    assert main(["scheduler", "--once"], s) == 0
    assert json.loads(capsys.readouterr().out)["leader"] is True
    assert main(["worker", "--once"], s) == 0
    assert json.loads(capsys.readouterr().out) == {"processed": 0}
    bad = _settings(tmp_path, inline_config={"schedules": {"x": "whenever"}})
    assert main(["scheduler", "--list"], bad) == 1


def test_settings_from_env(tmp_path: Any) -> None:
    secret_file = tmp_path / "gh"
    secret_file.write_text("from-file\n")
    tok = auth.hash_token("t")
    env = {
        "SQUADHQ_DB_URL": "postgresql://u:p@db/hq", "SQUADHQ_MODE": "worker",
        "SQUADHQ_API_TOKENS": f"admin:admin:{tok}, ro:viewer:{tok}",
        "SQUADHQ_WEBHOOK_SECRET_GITHUB_FILE": str(secret_file), "SQUADHQ_WEBHOOK_SECRET_LINEAR": "lin",
        "SQUADHQ_MAX_BODY_BYTES": "2048", "SQUADHQ_METRICS_PUBLIC": "true", "SQUADHQ_PORT": "9000",
        "SQUADHQ_JOB_TIMEOUT_S": "120", "HQ_REPO": "acme/hq", "SQUADHQ_HOME": "/srv/hub", "SQUADHQ_LOG_FORMAT": "text",
    }
    s = ServerSettings.from_env(env)
    assert s.mode == "worker" and s.db_url.startswith("postgresql://")
    assert [t.name for t in s.static_tokens] == ["admin", "ro"]
    assert s.webhook_secrets == {"github": "from-file", "linear": "lin"}
    assert (s.max_body_bytes, s.metrics_public, s.port, s.job_timeout_s) == (2048, True, 9000, 120.0)
    assert s.hub_repo == "acme/hq" and s.config_path == "/srv/hub/squadhq.yml"
    assert "byo-compute" not in " ".join(s.validate())
    d = ServerSettings.from_env({})
    assert d.mode == "byo-compute" and d.db_url == "sqlite:///squadhq.db" and d.config_path == "squadhq.yml"
    assert any("SQUADHQ_GITHUB_TOKEN" in p for p in d.validate())
    for bad in ({"SQUADHQ_MODE": "cloud"}, {"SQUADHQ_LOG_FORMAT": "xml"},
                {"SQUADHQ_API_TOKENS": "nohash"}, {"SQUADHQ_API_TOKENS": f"x:root:{tok}"},
                {"SQUADHQ_WEBHOOK_SECRET_GITHUB_FILE": str(tmp_path / "missing")}):
        with pytest.raises(ValueError):
            ServerSettings.from_env(bad)


def test_auth_helpers() -> None:
    p = auth.match("t", [("1", "a", "viewer", auth.hash_token("x")), ("2", "b", "admin", auth.hash_token("t"))])
    assert p is not None and p.name == "b" and p.has("approver") and p.has("viewer")
    assert not auth.Principal("v", "viewer").has("approver")
    assert auth.match("t", []) is None
    assert auth.bearer("Bearer  abc ") == "abc"
    assert auth.bearer("bearer x") == "x"
    assert auth.bearer("Token x") is None and auth.bearer(None) is None and auth.bearer("Bearer ") is None
    assert auth.generate_token() != auth.generate_token()


def test_json_logging(capsys: Any) -> None:
    configure_logging("INFO", "json")
    tok = request_id_var.set("rid-1")
    try:
        try:
            raise ValueError("x")
        except ValueError:
            logging.getLogger("t").exception("hello", extra={"job": "j1"})
    finally:
        request_id_var.reset(tok)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["msg"] == "hello" and line["request_id"] == "rid-1" and line["job"] == "j1" and "exc" in line
    configure_logging("WARNING", "text")
    assert isinstance(JSONFormatter().format(logging.LogRecord("n", 20, "", 0, "m", (), None)), str)


def test_registry_and_tracing() -> None:
    r = Registry()
    c = r.counter("c_total", "help", ("a",))
    g = r.gauge("g", "gauge")
    c.inc(a='x"y\n')
    c.inc(2, a='x"y\n')
    g.set(5)
    r.on_collect(lambda: (_ for _ in ()).throw(RuntimeError("collector broke")))
    text = r.render()
    assert 'c_total{a="x\\"y\\n"} 3' in text and "g 5" in text
    assert c.get(a='x"y\n') == 3
    with tracing.span("noop", a=1):
        pass
    enabled = tracing.enable()  # True only when opentelemetry is importable
    try:
        with tracing.span("real", a=1):
            pass
    finally:
        tracing.tracer = None
    assert isinstance(enabled, bool)


def test_missing_server_extra_prints_install_hint(tmp_path: Any, capsys: Any, monkeypatch: Any) -> None:
    import squadhq.server.__main__ as M

    def serve(settings: Any, args: Any) -> int:
        raise ModuleNotFoundError("No module named 'uvicorn'", name="uvicorn")

    def worker(settings: Any, args: Any) -> int:
        try:
            raise ImportError("no psycopg", name="psycopg")
        except ImportError as e:
            raise RuntimeError("Postgres store needs psycopg") from e

    def other(settings: Any, args: Any) -> int:
        raise ModuleNotFoundError("No module named 'nope'", name="nope")

    monkeypatch.setitem(M.COMMANDS, "serve", serve)
    monkeypatch.setitem(M.COMMANDS, "worker", worker)
    monkeypatch.setitem(M.COMMANDS, "check", other)
    s = _settings(tmp_path)
    assert main(["serve"], s) == 2
    err = capsys.readouterr().err
    assert "needs uvicorn" in err and "pip install 'agent-squad-hq[server,postgres]'" in err
    assert main(["worker", "--once"], s) == 2
    assert "needs psycopg" in capsys.readouterr().err
    with pytest.raises(ModuleNotFoundError):
        main(["check"], s)
