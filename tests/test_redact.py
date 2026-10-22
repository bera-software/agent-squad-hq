"""squadhq.redact — credentials never reach an issue, comment or summary."""
import pytest

from squadhq import failure_handler, redact

R = redact.REDACTED


@pytest.mark.parametrize("secret", [
    "ghp_" + "a1B2" * 9, "ghs_" + "Z" * 36, "gho_" + "x" * 30, "ghu_" + "9" * 30,
    "github_pat_11ABCDEFG0" + "x" * 50, "sk-ant-api03-" + "Q" * 40, "sk-" + "a" * 48,
    "sk-proj-" + "b" * 40, "AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghij", "xoxp-" + "1" * 20,
])
def test_token_shapes(secret):
    out = redact.redact(f"error: token {secret} rejected", env={})
    assert secret not in out and R in out and out.startswith("error: token ")


def test_bearer_authorization_url_and_pem():
    text = ("Authorization: Bearer abc.def.ghi123456\nauthorization: token 0123456789abcdef\n"
            "fetch https://user:hunter2pass@example.com/x\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\ntail")
    out = redact.redact(text, env={})
    assert "abc.def.ghi123456" not in out and "Bearer " + R in out
    assert "0123456789abcdef" not in out
    assert "hunter2pass" not in out and "https://" + R + "@example.com" in out
    assert "MIIEow" not in out and out.endswith("tail")
    # an unterminated PEM block is redacted to the end
    assert "MIIE" not in redact.redact("-----BEGIN PRIVATE KEY-----\nMIIE...", env={})


def test_env_values_by_name():
    env = {"MY_API_KEY": "s3cr3t-value-123", "GH_TOKEN": "plainopaquetoken", "DB_PASSWORD": "short",
           "HOME": "/home/runner-not-secret", "CLIENT_SECRET": ""}
    out = redact.redact("key=s3cr3t-value-123 tok=plainopaquetoken pw=short home=/home/runner-not-secret", env=env)
    assert "s3cr3t-value-123" not in out and "plainopaquetoken" not in out
    assert "pw=short" in out  # too short to redact safely
    assert "/home/runner-not-secret" in out
    assert redact.secret_env_values(env) == ["plainopaquetoken", "s3cr3t-value-123"]


def test_extra_values_and_empty(monkeypatch):
    assert redact.redact(None) == "" and redact.redact("") == ""
    assert redact.redact("x my-runtime-secret y", env={}, extra_values=["my-runtime-secret", "", "tiny"]) \
        == f"x {R} y"
    monkeypatch.setenv("SOME_TOKEN", "from-the-real-env")
    assert "from-the-real-env" not in redact.redact("v=from-the-real-env")


def test_ordinary_text_untouched():
    text = "Task failed: overloaded_error (HTTP 529). sk is short. Bearer of bad news."
    assert redact.redact(text, env={}) == text


def test_failure_handler_escalation_is_redacted(monkeypatch, tmp_path):
    tok = "ghp_" + "S" * 36
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
    seen = {}

    class FakeClient:
        def find_or_create_issue_by_title(self, repo, title, body, **kw):
            seen["body"], seen["comment"] = body, kw.get("comment_body")
            return 1, True

    monkeypatch.setattr(failure_handler.github, "client", lambda: FakeClient())
    failure_handler.escalate("o/h", "INFRA", "u", 1, "job", "o/r", "", "", f"Bad credentials {tok}")
    assert tok not in seen["body"] and tok not in (seen["comment"] or "")
    failure_handler.step_summary(f"leak {tok}")
    assert tok not in (tmp_path / "s.md").read_text()


def test_find_secrets_kinds_never_values():
    tok = "ghp_" + "K" * 36
    kinds = redact.find_secrets(f"x {tok} y my-llm-api-key-value", env={"LLM_API_KEY": "my-llm-api-key-value",
                                                                       "SSH_PRIVATE": "short"})
    assert kinds == ["github-token", "env:LLM_API_KEY"]
    assert all(tok not in k for k in kinds)
    assert redact.find_secrets("", env={}) == [] and redact.find_secrets("hello", env={}) == []


def test_cli_check_and_filter(tmp_path, monkeypatch, capsys):
    import io
    clean, dirty = tmp_path / "a.txt", tmp_path / "b.txt"
    clean.write_text("nothing here")
    dirty.write_text("AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setattr(redact.os, "environ", {})
    assert redact.main(["check", str(clean)]) == 0
    assert redact.main(["check", str(clean), str(dirty)]) == 1
    err = capsys.readouterr().err
    assert "aws-access-key" in err and "AKIAIOSFODNN7EXAMPLE" not in err
    assert redact.main(["check", str(tmp_path / "missing")]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("tok ghs_" + "a" * 36))
    assert redact.main(["check"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("Bearer abcdefghijk"))
    assert redact.main(["filter"]) == 0
    assert capsys.readouterr().out == "Bearer " + R
    assert redact.main([]) == 2
    assert redact.main(["--help"]) == 0
