import json
import os
import subprocess

import pytest

from squadhq import cli
from squadhq.config import app_manifest as AM
from squadhq.config.gh import FakeGh, Result
from tests.config.hubfix import EXAMPLE, MIN_YAML, make_hub


@pytest.fixture
def gh(monkeypatch):
    g = FakeGh()
    monkeypatch.setattr(cli, "GH", g)
    return g


@pytest.fixture
def hub(tmp_path, monkeypatch, gh):
    root = make_hub(tmp_path, config_text=MIN_YAML)
    monkeypatch.chdir(root)
    monkeypatch.delenv("SQUADHQ_CONFIG", raising=False)
    monkeypatch.setenv("SQUADHQ_HOME", root)
    return root


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_version(capsys):
    code, out, _ = run(capsys, "version")
    assert code == 0 and out.startswith("squadhq ")


def test_validate_example_and_explain(capsys, monkeypatch):
    monkeypatch.chdir(os.path.dirname(EXAMPLE))
    code, out, _ = run(capsys, "validate", "squadhq.example.yml", "--explain")
    assert code == 0 and "valid (0 warning(s))" in out
    assert "implement        heavy" in out and "daily_brief" in out


def test_validate_errors(capsys, tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("version: 1\norg: {name: A}\nhub: {repo: nope}\n")
    code, _out, err = run(capsys, "validate", str(p))
    assert code == 1 and "hub.repo" in err and "INVALID" in err
    code, _o, err = run(capsys, "validate", str(tmp_path / "missing.yml"))
    assert code == 2 and "not found" in err


def test_default_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SQUADHQ_CONFIG", "/x/y.yml")
    assert cli.default_config_path() == "/x/y.yml"
    monkeypatch.delenv("SQUADHQ_CONFIG")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path / "hub"))
    assert cli.default_config_path() == str(tmp_path / "hub" / "squadhq.yml")
    (tmp_path / "squadhq.yml").write_text("")
    assert cli.default_config_path() == "squadhq.yml"


def test_render_dry_run_write_check(capsys, hub):
    code, out, err = run(capsys, "render", "--dry-run", "--diff")
    assert code == 0 and "would update config/models.json" in out and "+++ b/config/auth.json" in out
    assert "SQUADHQ_DISABLED_AGENTS=" in out and "gh variable set SQUADHQ_HUB_REPO" in out
    assert "DST" in err
    code, _out, err = run(capsys, "render", "--check", "--diff")
    assert code == 1 and "config/projects.json" in err and "+++" in err
    code, out, _ = run(capsys, "render")
    assert code == 0 and "wrote config/models.json" in out and "wrote .github/workflows/daily-brief.yml" in out
    code, out, _ = run(capsys, "render")
    assert "already up to date" in out
    code, out, _ = run(capsys, "render", "--check")
    assert code == 0 and "up to date" in out
    with open(os.path.join(hub, "config", "projects.json")) as f:
        assert json.load(f)["projects"][0]["pack_roots"] == {"node-next": "web"}


def test_render_example_dry_run(capsys, monkeypatch):
    monkeypatch.chdir(os.path.dirname(EXAMPLE))
    code, out, _ = run(capsys, "render", "--config", "squadhq.example.yml", "--dry-run")
    assert code == 0 and "required secrets" in out


def test_render_invalid_and_schedule_error(capsys, hub, monkeypatch):
    with open("squadhq.yml", "a") as f:
        f.write("bogus: 1\n")
    code, _o, err = run(capsys, "render")
    assert code == 1 and "bogus" in err
    from squadhq.config import render as RN

    def boom(*a, **k):
        raise ValueError("schedules: x")

    with open("squadhq.yml", "w") as f:
        f.write(MIN_YAML)
    monkeypatch.setattr(RN, "plan", boom)
    code, _o, err = run(capsys, "render")
    assert code == 1 and "schedules: x" in err


def test_doctor_offline(capsys, hub):
    code, out, _ = run(capsys, "doctor", "--offline")
    assert code == 1 and "❌ rendered files stale" in out and "fix: run `squadhq render`" in out
    run(capsys, "render")
    code, out, _ = run(capsys, "doctor", "--offline")
    assert code == 0 and "✅ rendered files fresh" in out


INIT_FLAGS = ["init", "--non-interactive", "--owner", "acme", "--org-name", "Acme", "--timezone", "Europe/London",
              "--approver", "@acme/leads", "--project", "acme/shop", "--packs", "acme/shop=node-next",
              "--project", "acme/api"]


def test_init_dry_run_prints_plan(capsys, hub, gh):
    os.remove("squadhq.yml")
    code, out, _ = run(capsys, *INIT_FLAGS, "--create-app")
    assert code == 0 and "# would write " in out and "squadhq.yml:" in out
    assert not os.path.exists("squadhq.yml")
    assert "would create GitHub App(s) 'acme-squad', 'acme-squad-agent'" in out
    assert "gh secret set SQUADHQ_AGENT_APP_PRIVATE_KEY --org acme --visibility all < acme-squad-agent.private-key.pem" in out
    assert "gh label create dept:backend -R acme/shop" in out
    assert "gh api -X POST repos/acme/shop/rulesets --input - < (stdin)" in out
    assert "gh variable set SQUADHQ_HUB_REPO --body acme/agent-squad-hq --org acme --visibility all" in out
    assert "squadhq onboard acme/shop --apply" in out
    # dry run: only read-only lookups (existing rulesets), never a mutation
    assert all(c[:2] == ["api", c[1]] and "-X" not in c for c in gh.calls)


def test_init_config_only_and_existing(capsys, hub):
    os.remove("squadhq.yml")
    code, out, _ = run(capsys, *INIT_FLAGS, "--config-only")
    assert code == 0 and "wrote " in out and "squadhq.yml" in out
    assert "rendered config/models.json" in out and "no GitHub changes" in out
    text = open("squadhq.yml").read()
    assert "packs: [\"node-next\"]" in text and "repo: acme/api" in text
    code, out, _ = run(capsys, *INIT_FLAGS, "--config-only")
    assert "exists; keeping it" in out


def test_init_invalid_answers(capsys, hub):
    code, _o, err = run(capsys, "init", "--non-interactive", "--owner", "acme", "--hub-repo", "bad",
                        "--timezone", "UTC")
    assert code == 1 and "refusing" in err
    with pytest.raises(SystemExit):
        cli.main(["init", "--non-interactive"])


def test_init_apply_runs_gh(capsys, hub, gh, monkeypatch, tmp_path):
    os.remove("squadhq.yml")
    gh.routes = {("auth", "status"): Result(0), ("label",): Result(0), ("variable",): Result(0),
                 ("secret",): Result(0), ("api", "-X"): Result(1, "", "403 forbidden"),
                 ("api", "users/acme"): Result(0, "Organization"),
                 ("api", "repos/acme/shop/rulesets"): Result(0, "[]")}
    monkeypatch.setattr(cli, "OPEN_BROWSER", lambda u: True)
    seen = []

    def fake_flow(owner, is_org, slug, hub_repo, **kw):
        seen.append({"owner": owner, "is_org": is_org, "slug": slug,
                     "manifest": os.path.basename(kw["template_path"])})
        cid = "Iv1.abc" if slug == "acme-squad" else "Iv1.agent"
        return AM.Conversion(id=len(seen), slug=slug, client_id=cid, pem="PEM",
                             html_url=f"https://github.com/apps/{slug}")

    monkeypatch.setattr(AM, "run_flow", fake_flow)
    monkeypatch.setattr(gh, "authenticated", lambda: True)
    code, out, _ = run(capsys, *INIT_FLAGS, "--apply", "--create-app", "--pem-dir", str(tmp_path))
    assert code == 0, out
    assert seen == [{"owner": "acme", "is_org": True, "slug": "acme-squad", "manifest": "app-manifest.json"},
                    {"owner": "acme", "is_org": True, "slug": "acme-squad-agent",
                     "manifest": "app-manifest-agent.json"}]
    assert os.path.exists(tmp_path / "acme-squad.private-key.pem")
    assert os.path.exists(tmp_path / "acme-squad-agent.private-key.pem")
    assert "✅ secret SQUADHQ_APP_PRIVATE_KEY" in out and "✅ secret SQUADHQ_AGENT_APP_PRIVATE_KEY" in out
    assert ["variable", "set", "SQUADHQ_AGENT_APP_CLIENT_ID", "--body", "Iv1.agent", "--org", "acme",
            "--visibility", "all"] in gh.calls
    assert "https://github.com/apps/acme-squad-agent/installations/new" in out
    assert "agent_bot:\n    app: acme-squad-agent" in open("squadhq.yml").read()
    assert "❌ create ruleset 'squadhq' on acme/shop" in out and "403" in out
    assert ["variable", "set", "SQUADHQ_APP_CLIENT_ID", "--body", "Iv1.abc", "--org", "acme", "--visibility",
            "all"] in gh.calls
    assert "https://github.com/apps/acme-squad/installations/new" in out
    assert os.path.exists("squadhq.yml")


def test_init_apply_requires_auth(capsys, hub, gh):
    os.remove("squadhq.yml")
    code, _o, err = run(capsys, *INIT_FLAGS, "--apply")
    assert code == 1 and "gh auth login" in err


def test_init_interactive(capsys, hub, gh, monkeypatch):
    os.remove("squadhq.yml")
    gh.routes = {("api", "user"): Result(0, "alice\n")}
    monkeypatch.setattr(cli, "ASK", lambda q, d: "Europe/London" if q.startswith("Timezone") else d)
    code, out, _ = run(capsys, "init", "--config-only")
    assert code == 0 and "squadhq.yml" in out and "wrote " in out
    assert 'approvers: ["@alice"]' in open("squadhq.yml").read()


def test_init_interactive_needs_a_terminal(capsys, hub, gh, monkeypatch):
    import io
    monkeypatch.setattr(cli, "ASK", None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, _o, err = run(capsys, "init", "--config-only")
    assert code == 2 and "stdin is not a terminal" in err and "--non-interactive" in err
    assert gh.calls == []  # no gh lookups before the check


def test_missing_config_is_exit_2_everywhere(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    missing = str(tmp_path / "nope.yml")
    for argv in (["validate", missing], ["render", "--check", "--config", missing], ["drill", "--config", missing]):
        code, _o, err = run(capsys, *argv)
        assert code == 2 and err.startswith(f"error: {missing} not found"), (argv, err)


def test_delegate_usage_names_the_cli(capsys):
    for cmd in ("skills", "evals"):
        with pytest.raises(SystemExit):
            cli.main([cmd, "--help"])
        assert capsys.readouterr().out.startswith(f"usage: squadhq {cmd} ")


def test_init_detect_packs(capsys, hub, gh):
    os.remove("squadhq.yml")
    gh.routes = {("api", "repos/acme/shop/contents"): Result(0, json.dumps([{"name": "package.json", "type": "file"}]))}
    code, _o, _e = run(capsys, "init", "--non-interactive", "--owner", "acme", "--timezone", "UTC",
                       "--project", "acme/shop", "--detect-packs", "--config-only")
    assert code == 0
    assert 'packs: ["node-next"]' in open("squadhq.yml").read()


def test_onboard(capsys, hub, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli, "SUBPROCESS_RUN", fake_run)
    code, out, _ = run(capsys, "onboard", "acme/my-shop")
    assert code == 0 and "audit only" in out
    assert calls[0][0][1:] == ["-m", "squadhq.onboard", "audit", "acme/my-shop", "My Shop"]
    assert calls[0][1]["timeout"] == 900.0 and calls[0][1]["env"]["SQUADHQ_HOME"] == hub
    code, out, _ = run(capsys, "onboard", "acme/my-shop", "--apply", "--name", "Shop")
    assert calls[1][0][3:] == ["apply", "acme/my-shop", "Shop"] and "squadhq render" in out

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(cli, "SUBPROCESS_RUN", slow)
    code, _o, err = run(capsys, "onboard", "acme/x", "--apply")
    assert code == 124 and "timed out" in err


def test_drill_dry_run(capsys, hub, gh):
    code, out, _ = run(capsys, "drill")
    assert code == 0 and "gh issue create -R acme/storefront" in out and gh.calls == []


def _clock(monkeypatch, step=10.0):
    t = {"now": 0.0}
    monkeypatch.setattr(cli, "NOW", lambda: t["now"])

    def sleep(s):
        t["now"] += s

    monkeypatch.setattr(cli, "SLEEP", sleep)


def test_drill_apply_pass(capsys, hub, gh, monkeypatch):
    _clock(monkeypatch)
    polls = {"n": 0}

    def view(args, _input):
        polls["n"] += 1
        comments = [] if polls["n"] < 2 else [{"user": {"login": "squadhq-agent[bot]", "type": "Bot"}}]
        return Result(0, json.dumps(comments))

    gh.routes = {("issue", "create"): Result(0, "https://github.com/acme/storefront/issues/42\n"),
                 ("api", "repos/acme/storefront/issues/42/comments"): view, ("issue", "close"): Result(0)}
    code, out, _ = run(capsys, "drill", "--apply", "--interval", "5")
    assert code == 0 and "drill passed" in out
    assert gh.calls[-1][:3] == ["issue", "close", "42"]


def test_drill_apply_timeout_and_create_fail(capsys, hub, gh, monkeypatch):
    _clock(monkeypatch)
    gh.routes = {("issue", "create"): Result(0, "https://github.com/acme/x/issues/7"),
                 # a HUMAN named exactly like the App never counts as the bot's reply
                 ("api", "repos/acme/x/issues/7/comments"): Result(0, json.dumps([
                     {"user": {"login": "squadhq-ops", "type": "User"}}, {"user": {"login": "someone"}}])),
                 ("issue", "close"): Result(0)}
    code, out, _ = run(capsys, "drill", "--apply", "--repo", "acme/x", "--timeout", "30", "--interval", "10")
    assert code == 1 and "no reply" in out
    gh.routes = {}
    code, _o, err = run(capsys, "drill", "--apply")
    assert code == 1 and "could not create" in err


def test_drill_invalid_config(capsys, hub):
    with open("squadhq.yml", "w") as f:
        f.write("version: 1\n")
    code, _o, err = run(capsys, "drill")
    assert code == 1 and "org" in err


def test_packs_passthrough(capsys, hub):
    code, out, _ = run(capsys, "packs", "list")
    assert code == 0 and "node-next" in out


@pytest.mark.parametrize("cmd,module", [("server", "squadhq.server.__main__"), ("skills", "squadhq.skills.cli"),
                                        ("evals", "squadhq.skills.evals.cli")])
def test_delegates(capsys, monkeypatch, cmd, module):
    import importlib
    import types
    seen = {}
    fake = types.SimpleNamespace(main=lambda argv, **kw: seen.setdefault("argv", argv) and 3)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake if name == module else None)
    assert cli.main([cmd, "run", "-h", "--x"]) == 3
    assert seen["argv"] == ["run", "-h", "--x"]


def test_delegate_missing_module(capsys, monkeypatch):
    import importlib

    def missing(name):
        raise ImportError("No module named 'fastapi'")

    monkeypatch.setattr(importlib, "import_module", missing)
    code, _o, err = run(capsys, "server", "serve")
    assert code == 2 and "install agent-squad-hq[server]" in err


def test_doctor_single_check_and_list(capsys, hub):
    code, out, _ = run(capsys, "doctor", "--list-checks")
    assert code == 0 and out.splitlines()[0].startswith("config ")
    code, out, _ = run(capsys, "doctor", "--check", "rendered")
    assert code == 1 and "rendered files stale" in out and "✅ model" not in out
    code, out, _ = run(capsys, "doctor", "--check", "models")
    assert code == 0 and "claude-sonnet-5-5" in out
    code, _o, err = run(capsys, "doctor", "--check", "bogus")
    assert code == 2 and "unknown check" in err


def test_version_flag(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("squadhq ")


def test_packs_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["packs", "-h"])
    out = capsys.readouterr().out
    for cmd in ("files", "ci-plan", "tools", "risk-globs", "detect"):
        assert cmd in out
    with pytest.raises(SystemExit):
        cli.main(["packs", "risk-globs", "-h"])
    assert "force risk >= high" in capsys.readouterr().out


def test_not_a_hub_exits_2(capsys, tmp_path, monkeypatch, gh):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path))
    (tmp_path / "squadhq.yml").write_text(MIN_YAML)
    for argv in (["render"], ["doctor", "--offline"], ["packs", "list"], ["skills", "validate"],
                 ["evals", "list"], ["onboard", "acme/x"]):
        code, _o, err = run(capsys, *argv)
        assert code == 2 and "not a hub checkout: run `squadhq init` or set SQUADHQ_HOME" in err, argv


def test_init_unknown_pack_rejected(capsys, hub):
    code, _o, err = run(capsys, "init", "--non-interactive", "--owner", "acme", "--timezone", "UTC",
                        "--project", "acme/shop", "--packs", "acme/shop=node-nxt", "--config-only")
    assert code == 2 and "unknown pack 'node-nxt'" in err and "node-next" in err


def test_init_writes_bot_app_matching_login(capsys, hub):
    os.remove("squadhq.yml")
    code, out, _ = run(capsys, "init", "--non-interactive", "--owner", "Acme", "--timezone", "UTC",
                       "--config-only")
    assert code == 0
    from squadhq.config import loader
    cfg = loader.load("squadhq.yml")
    assert cfg.hub.bot.app == "acme-squad" and cfg.hub.bot.login == "acme-squad[bot]"
    from squadhq.config import render as RN
    assert RN.repo_variables(cfg)["SQUADHQ_BOT_LOGIN"] == "acme-squad[bot]"


def test_init_scaffolds_new_hub(capsys, tmp_path, monkeypatch, gh):
    from squadhq.config import scaffold as SC
    git_calls = []
    real = SC.scaffold
    monkeypatch.setattr(SC, "scaffold", lambda dest, **kw: real(dest, run=lambda *a, **k: git_calls.append(a[0])))
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "init", "--non-interactive", "--owner", "acme", "--timezone", "UTC",
                       "--project", "acme/shop", "--packs", "acme/shop=node-next", "--hub-dir", "hq")
    assert code == 0 and "would scaffold a new hub" in out and not (tmp_path / "hq").exists()
    code, out, _ = run(capsys, "init", "--non-interactive", "--owner", "acme", "--timezone", "UTC",
                       "--project", "acme/shop", "--packs", "acme/shop=node-next", "--hub-dir", "hq",
                       "--config-only")
    assert code == 0, out
    hq = tmp_path / "hq"
    assert (hq / "config" / "flows.yml").exists() and (hq / "squadhq.yml").exists()
    assert (hq / "config" / "models.json").exists() and "*.pem" in (hq / ".gitignore").read_text()
    assert git_calls and git_calls[0][:2] == ["git", "init"]
    monkeypatch.chdir(hq)
    monkeypatch.setenv("SQUADHQ_HOME", str(hq))
    assert run(capsys, "validate")[0] == 0
    code, out, err = run(capsys, "render", "--check")
    assert code == 0, err


def test_init_single_app_warns_and_creates_one(capsys, hub, gh, monkeypatch, tmp_path):
    os.remove("squadhq.yml")
    gh.routes = {("auth", "status"): Result(0), ("label",): Result(0), ("variable",): Result(0),
                 ("secret",): Result(0), ("api",): Result(0, "[]"), ("api", "users/acme"): Result(0, "Organization")}
    made = []
    monkeypatch.setattr(AM, "run_flow", lambda owner, is_org, slug, hub_repo, **kw: made.append(slug) or
                        AM.Conversion(id=1, slug=slug, client_id="Iv1", pem="P", html_url="https://github.com/apps/x"))
    monkeypatch.setattr(gh, "authenticated", lambda: True)
    code, out, err = run(capsys, *INIT_FLAGS, "--apply", "--create-app", "--single-app", "--pem-dir", str(tmp_path))
    assert code == 0, out
    assert made == ["acme-squad"]
    assert "single-app mode" in err and "disables every unattended merge" in err
    text = open("squadhq.yml").read()
    assert "single_app: true" in text and "agent_bot" not in text
