"""squadhq.github — the single gh client. A fake runner stands in for the
gh binary; nothing here touches the network."""
import base64
import json
import subprocess

import pytest

from squadhq import github


class FakeRunner:
    """Scripted gh: each call pops the next (returncode, stdout, stderr) —
    or a callable(cmd, input) -> tuple, or an exception instance."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, input=None, timeout=None):
        self.calls.append({"cmd": cmd, "input": input, "timeout": timeout})
        r = self.responses.pop(0) if self.responses else (0, "", "")
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            r = r(cmd, input)
        rc, out, err = r
        return subprocess.CompletedProcess(cmd, rc, out, err)


def make(*responses, **kw):
    runner = FakeRunner(*responses)
    sleeps = []
    c = github.GitHubClient(runner=runner, sleep=sleeps.append, **kw)
    return c, runner, sleeps


def test_run_success_passes_timeout_and_input():
    c, r, _ = make((0, "ok", ""), timeout=5)
    out = c.run(["api", "x"], input_text="body")
    assert out.stdout == "ok"
    assert r.calls[0]["cmd"] == ["gh", "api", "x"]
    assert r.calls[0]["timeout"] == 5 and r.calls[0]["input"] == "body"


def test_transient_failures_retry_with_backoff_then_succeed():
    c, r, sleeps = make((1, "", "HTTP 502: Bad Gateway"), (1, "", "secondary rate limit"), (0, "[]", ""))
    assert c.run(["api", "x"]).returncode == 0
    assert len(r.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_non_transient_failure_does_not_retry():
    c, r, sleeps = make((1, "", "HTTP 404: Not Found"))
    assert c.run(["api", "x"]).returncode == 1
    assert len(r.calls) == 1 and sleeps == []


def test_retry_disabled_and_exhausted():
    c, r, _ = make((1, "", "HTTP 500"), (1, "", "HTTP 500"))
    assert c.run(["x"], retry=False).returncode == 1
    assert len(r.calls) == 1
    c, r, _ = make((1, "", "HTTP 500"), (1, "", "HTTP 500"), (1, "", "HTTP 500"), retries=2)
    assert c.run(["x"]).returncode == 1
    assert len(r.calls) == 3


def test_timeout_and_missing_binary():
    c, _, _ = make(subprocess.TimeoutExpired("gh", 1), subprocess.TimeoutExpired("gh", 1),
                   subprocess.TimeoutExpired("gh", 1))
    r = c.run(["x"])
    assert r.returncode == 124 and "timeout" in r.stderr
    c, runner, _ = make(FileNotFoundError("gh"))
    r = c.run(["x"])
    assert r.returncode == 127 and len(runner.calls) == 1


def test_check_raises_github_error():
    c, _, _ = make((1, "", "boom"))
    with pytest.raises(github.GitHubError) as e:
        c.run(["issue", "view"], check=True)
    assert e.value.returncode == 1 and "boom" in str(e.value)


def test_env_timeout(monkeypatch):
    monkeypatch.setenv("SQUADHQ_GH_TIMEOUT", "7")
    assert github.GitHubClient(runner=FakeRunner()).timeout == 7.0
    monkeypatch.setenv("SQUADHQ_GH_TIMEOUT", "nope")
    assert github.GitHubClient(runner=FakeRunner()).timeout == github.DEFAULT_TIMEOUT


def test_json_variants():
    c, _, _ = make((0, '{"a": 1}', ""), (0, "  ", ""), (1, "", "x"), (0, "not json", ""), (0, "nope", ""))
    assert c.json(["x"]) == {"a": 1}
    assert c.json(["x"]) is None
    assert c.json(["x"], default=[]) == []
    assert c.json(["x"], default="d") == "d"
    with pytest.raises(github.GitHubError):
        c.json(["x"])
    c, _, _ = make((1, "", "x"))
    with pytest.raises(github.GitHubError):
        c.json(["x"])


def test_lines():
    c, _, _ = make((0, "a\n\nb\n", ""), (1, "", "e"), (1, "", "e"))
    assert c.lines(["x"]) == ["a", "b"]
    assert c.lines(["x"], default=None) is None
    with pytest.raises(github.GitHubError):
        c.lines(["x"])


def test_paginate_parses_one_value_per_line_across_pages():
    pages = '{"name": "a"}\n{"name": "b"}\n\n{"name": "c"}\n'
    c, r, _ = make((0, pages, ""), (0, '{"id": 1}\n', ""), (1, "", "e"), (1, "", "e"))
    assert [x["name"] for x in c.paginate("repos/o/r/labels")] == ["a", "b", "c"]
    assert r.calls[0]["cmd"] == ["gh", "api", "--paginate", "repos/o/r/labels", "--jq", ".[] | tojson"]
    assert c.paginate("repos/o/r/actions/runs/1/jobs", key="jobs") == [{"id": 1}]
    assert r.calls[1]["cmd"][-1] == ".jobs[] | tojson"
    assert c.paginate("x", default=[]) == []
    with pytest.raises(github.GitHubError):
        c.paginate("x")


def test_list_capped_warns_at_cap(monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    c, r, _ = make((0, json.dumps([{"n": 1}, {"n": 2}]), ""), (0, "[]", ""))
    warns = []
    assert len(c.list_capped(["issue", "list", "-R", "o/r"], 2, what="issues", warnings=warns)) == 2
    assert r.calls[0]["cmd"][-2:] == ["--limit", "2"]
    assert warns and "2-item cap" in warns[0]
    assert "cap" in summary.read_text()
    assert "warning:" in capsys.readouterr().err
    assert c.list_capped(["issue", "list"], 5) == []


def test_warn_tolerates_unwritable_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing-dir" / "s.md"))
    github._warn("x", None)  # must not raise


def test_api_builds_fields_body_and_retry_policy():
    c, r, _ = make((0, "", ""), (1, "", "HTTP 502"), (0, "{}", ""))
    c.api("repos/o/r/issues", method="post", raw_fields={"title": "t"}, fields={"n": 1}, jq=".id",
          body={"a": 1})
    cmd = r.calls[0]["cmd"]
    assert cmd[:5] == ["gh", "api", "-X", "POST", "repos/o/r/issues"]
    assert ["-f", "title=t"] == cmd[5:7] and ["-F", "n=1"] == cmd[7:9]
    assert "--jq" in cmd and cmd[-2:] == ["--input", "-"]
    assert json.loads(r.calls[0]["input"]) == {"a": 1}
    # POST is not retried even on a transient failure...
    c, r, _ = make((1, "", "HTTP 502"), (0, "", ""))
    assert c.api("x", method="POST").returncode == 1 and len(r.calls) == 1
    # ...GET is.
    c, r, _ = make((1, "", "HTTP 502"), (0, "", ""))
    assert c.api("x").returncode == 0 and len(r.calls) == 2


def test_api_json():
    c, _, _ = make((0, '{"x": 1}', ""), (0, "", ""), (1, "", "e"), (1, "", "e"), (0, "bad", ""), (0, "bad", ""))
    assert c.api_json("p") == {"x": 1}
    assert c.api_json("p") is None
    assert c.api_json("p", default=0) == 0
    with pytest.raises(github.GitHubError):
        c.api_json("p")
    assert c.api_json("p", default=5) == 5
    with pytest.raises(github.GitHubError):
        c.api_json("p")


def test_graphql_ok_errors_and_failure():
    c, r, _ = make((0, '{"data": {"x": 1}}', ""), (0, '{"errors": [{"message": "no"}]}', ""), (1, "", "e"))
    assert c.graphql("q", {"a": "b"}, {"n": 3}) == {"data": {"x": 1}}
    cmd = r.calls[0]["cmd"]
    assert ["-f", "a=b"] == cmd[5:7] and ["-F", "n=3"] == cmd[7:9]
    with pytest.raises(github.GitHubError, match="GraphQL errors"):
        c.graphql("q")
    with pytest.raises(github.GitHubError):
        c.graphql("q")


def test_find_issue_by_title_exact_match_only():
    bot = {"login": "app/squadhq-ops"}
    items = [{"number": 1, "title": "Budget cap hit (old)", "author": bot},
             {"number": 3, "title": "Budget cap hit", "author": {"login": "mallory"}},  # spoofed
             {"number": 7, "title": "Budget cap hit", "author": bot}]
    c, r, _ = make((0, json.dumps(items), ""), (1, "", "e"), (0, json.dumps(items[1:2]), ""))
    assert c.find_issue_by_title("o/h", "Budget cap hit", label="escalation") == 7
    assert "--label" in r.calls[0]["cmd"]
    assert c.find_issue_by_title("o/h", "Budget cap hit") is None
    assert c.find_issue_by_title("o/h", "Budget cap hit", bot_only=False) == 3


def test_create_issue_parses_url_and_retries_without_missing_labels():
    c, r, _ = make((0, "https://github.com/o/r/issues/42\n", ""))
    assert c.create_issue("o/r", "t", "b", "bug") == 42
    assert r.calls[0]["cmd"][-2:] == ["-l", "bug"]
    c, r, _ = make((1, "", "could not add label: 'x' not found"), (0, "https://github.com/o/r/issues/5", ""))
    assert c.create_issue("o/r", "t", "b", ["x"]) == 5
    assert "-l" not in r.calls[1]["cmd"]
    c, _, _ = make((1, "", "boom"))
    assert c.create_issue("o/r", "t", "b") is None
    c, _, _ = make((0, "created", ""))
    assert c.create_issue("o/r", "t", "b") is None


def test_find_or_create_comments_on_existing_or_creates():
    existing = json.dumps([{"number": 3, "title": "T", "author": {"login": "app/squadhq-ops"}}])
    c, r, _ = make((0, existing, ""), (0, "", ""))
    assert c.find_or_create_issue_by_title("o/h", "T", "body", comment_body="again") == (3, False)
    assert r.calls[1]["cmd"][:3] == ["gh", "issue", "comment"]
    c, r, _ = make((0, existing, ""))
    assert c.find_or_create_issue_by_title("o/h", "T", "body") == (3, False)
    assert len(r.calls) == 1
    spoof = json.dumps([{"number": 4, "title": "T", "author": {"login": "mallory"}}])
    c, r, _ = make((0, spoof, ""), (0, "https://github.com/o/h/issues/9", ""))
    assert c.find_or_create_issue_by_title("o/h", "T", "body", labels=["escalation"]) == (9, True)


def test_contents_get():
    blob = base64.b64encode(b'{"a": 1}').decode()
    c, r, _ = make((0, json.dumps({"content": blob, "sha": "s1"}), ""), (1, "", "404"),
                   (0, json.dumps({"content": base64.b64encode(b"\xff\xfe").decode()}), ""), (0, "[]", ""))
    assert c.contents_get("o/h", "state/x.json", ref="main") == ('{"a": 1}', "s1")
    assert r.calls[0]["cmd"][-1].endswith("?ref=main")
    assert c.contents_get("o/h", "x") == (None, None)
    assert c.contents_get("o/h", "x") == (None, None)
    assert c.contents_get("o/h", "x") == (None, None)


def test_contents_put_and_update_json_file(monkeypatch):
    monkeypatch.setenv("SQUADHQ_BOT_USER_ID", "123")
    monkeypatch.setenv("SQUADHQ_BOT_LOGIN", "acme-bot[bot]")
    github.reset_identity_cache()
    blob = base64.b64encode(json.dumps({"n": 1}).encode()).decode()
    # read -> PUT loses the race (409) -> re-read -> PUT ok
    c, r, sleeps = make((0, json.dumps({"content": blob, "sha": "s1"}), ""), (1, "", "HTTP 409: conflict"),
                        (0, json.dumps({"content": blob, "sha": "s2"}), ""), (0, "{}", ""))
    assert c.update_json_file("o/h", "state/u.json", lambda cur: {"n": cur["n"] + 1}, "msg")
    put = json.loads(r.calls[3]["input"])
    assert put["sha"] == "s2" and json.loads(base64.b64decode(put["content"])) == {"n": 2}
    assert put["committer"]["email"] == "123+acme-bot[bot]@users.noreply.github.com"
    assert sleeps == [1.0]
    # missing file -> create without sha, on a branch
    c, r, _ = make((0, "{}", ""))
    assert c.contents_put("o/h", "p", "x", "m", branch="b")
    body = json.loads(r.calls[0]["input"])
    assert "sha" not in body and body["branch"] == "b"
    github.reset_identity_cache()


def test_update_json_file_noop_default_and_give_up(monkeypatch):
    monkeypatch.setenv("SQUADHQ_BOT_USER_ID", "1")
    github.reset_identity_cache()
    c, r, _ = make((1, "", "404"))
    assert c.update_json_file("o/h", "p", lambda cur: None, "m")
    assert len(r.calls) == 1
    c, r, _ = make((0, json.dumps({"content": base64.b64encode(b"not json").decode(), "sha": "s"}), ""),
                   (1, "", "422"), (1, "", "404"), (1, "", "422"))
    seen = []
    assert not c.update_json_file("o/h", "p", lambda cur: seen.append(cur) or {"x": 1}, "m",
                                  default={"d": 1}, attempts=2)
    assert seen == [{"d": 1}, {"d": 1}]


@pytest.mark.parametrize("raw,expected", [
    ("bug", ["bug"]), ("bug, P1", ["bug", "P1"]), (["a", "a", " b "], ["a", "b"]),
    ([{"name": "x"}, 3, None, "y,z"], ["x", "y", "z"]), (None, []), (5, []), (("t",), ["t"]),
    ({"s"}, ["s"]),
])
def test_normalize_labels(raw, expected):
    assert github.normalize_labels(raw) == expected


def test_bot_identity_lookup_and_fallback(monkeypatch):
    monkeypatch.delenv("SQUADHQ_BOT_USER_ID", raising=False)
    monkeypatch.setenv("SQUADHQ_BOT_LOGIN", "acme-bot[bot]")
    github.reset_identity_cache()
    c, r, _ = make((0, "999\n", ""))
    assert github.bot_identity(c) == ("acme-bot[bot]", "999+acme-bot[bot]@users.noreply.github.com")
    assert r.calls[0]["cmd"] == ["gh", "api", "/users/acme-bot[bot]", "--jq", ".id"]
    assert github.bot_identity(c)[0] == "acme-bot[bot]"  # cached, no second call
    assert len(r.calls) == 1
    github.reset_identity_cache()
    c, _, _ = make((1, "", "404"))
    assert github.bot_identity(c)[0] == "github-actions[bot]"
    github.reset_identity_cache()


def test_default_client_singleton_and_swap():
    github.set_client(None)
    a = github.client()
    assert a is github.client()
    fake = github.GitHubClient(runner=FakeRunner())
    github.set_client(fake)
    assert github.client() is fake
    github.set_client(None)


def test_is_transient():
    assert github.is_transient("HTTP 503 Service Unavailable")
    assert github.is_transient("read: connection reset by peer")
    assert not github.is_transient("HTTP 422: Validation Failed")
    assert not github.is_transient("")
