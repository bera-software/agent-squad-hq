"""Tests for squadhq.onboard. No network: gh goes through a fake
GitHubClient runner, git/clone through the module's run/sh seams. The hub is a
temp copy of tests/fixtures/onboard (+ the real config/flows.yml) via
SQUADHQ_HOME, so the registry write never touches the live config."""
import json
import os
import shutil
import subprocess
from unittest import mock

import pytest

from squadhq import github
from squadhq import onboard as ob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "onboard")


def cp(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeGH:
    """Answers gh calls. `files` = repo paths that exist; labels are served
    as one JSON value per line, the way `--paginate --jq '.[]|tojson'` does
    for multiple pages."""

    def __init__(self, files=(), labels=(), protected=True, secrets=None, reachable=True, labels_rc=0):
        self.files, self.labels, self.protected = set(files), list(labels), protected
        self.secrets, self.reachable, self.labels_rc = secrets, reachable, labels_rc
        self.calls = []

    def __call__(self, cmd, **kw):
        a = cmd[1:]
        self.calls.append(a)
        if a[:2] == ["api", "--paginate"]:
            return cp(self.labels_rc, "\n".join(json.dumps({"name": n}) for n in self.labels), "HTTP 403")
        if a[0] == "api":
            path = a[1]
            if path.endswith("/protection"):
                return cp(0 if self.protected else 1)
            if path.endswith("/actions/secrets"):
                return cp(0, json.dumps(self.secrets)) if self.secrets is not None else cp(1, "", "HTTP 403")
            if "/contents/" in path:
                return cp(0 if path.split("/contents/", 1)[1] in self.files else 1, "{}", "")
            if path.startswith("/users/"):
                return cp(0, "4242\n")
            return cp(0, "main\n") if self.reachable else cp(1, "", "HTTP 404")
        if a[:2] == ["pr", "create"]:
            return cp(0, "https://github.com/acme/new/pull/1\n")
        return cp(0)

    def created_issue(self):
        return next((c for c in self.calls if c[:2] == ["issue", "create"]), None)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    home = tmp_path / "hub"
    shutil.copytree(FIXTURE, home)
    shutil.copy(os.path.join(ROOT, "config", "flows.yml"), home / "config" / "flows.yml")
    monkeypatch.setenv("SQUADHQ_HOME", str(home))
    monkeypatch.setenv("SQUADHQ_HUB_REPO", "acme/squad-hub")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(ob.time, "sleep", lambda s: None)
    monkeypatch.delenv("SQUADHQ_MODE", raising=False)
    monkeypatch.delenv("SQUADHQ_DEPLOYMENT", raising=False)
    monkeypatch.setattr(ob.stub_sync, "placeholders", lambda hub=None, ref=None: dict(STUB_SUBS, __HUB_REPO__=hub))
    github.reset_identity_cache()
    yield home
    github.set_client(None)
    github.reset_identity_cache()


STUB_SUBS = {"__HUB_REPO__": "acme/squad-hub", "__HUB_REF__": "f" * 40, "__HUB_TAG__": "v1.0.0"}


def use(fake):
    github.set_client(github.GitHubClient(runner=fake, retries=0))
    return fake


def all_labels():
    return [n for n, _ in ob.label_entries()]


def test_main_validates_inputs(hub, capsys):
    assert ob.main(["audit", "not a repo", "X"]) == 2
    assert ob.main(["audit", "acme/x", "bad;name"]) == 2
    assert "invalid" in capsys.readouterr().err


def test_hub_repo_fallback(monkeypatch):
    for v in ("SQUADHQ_HUB_REPO", "HQ_REPO", "HUB_REPO", "SQUADHQ_ORG", "GITHUB_REPOSITORY_OWNER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/hub")
    assert ob.hub_repo() == "me/hub"


def test_audit_unreachable(hub, capsys):
    use(FakeGH(reachable=False))
    assert ob.main(["audit", "acme/demo", "Demo"]) == 1
    assert "FATAL" in capsys.readouterr().out


def test_audit_all_ready_with_paginated_labels(hub, tmp_path, monkeypatch, capsys):
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    labels = all_labels()
    assert len(labels) > 30  # more than one API page
    fake = use(FakeGH(files={".github/workflows/agents.yml", "CLAUDE.md", "docs/lessons.md",
                             "docs/adr/0001-record-architecture-decisions.md"},
                      labels=labels, secrets={"secrets": [{"name": "ANTHROPIC_API_KEY"}]}))
    assert ob.audit("acme/demo", "Demo") == 0
    out = capsys.readouterr().out
    assert "8/8 ready" in out, out
    assert "Onboarding audit: Demo" in summary.read_text()
    issue = fake.created_issue()
    assert issue[issue.index("-R") + 1] == "acme/squad-hub"


def test_audit_reports_missing(hub, capsys):
    use(FakeGH(labels=["P0"], protected=False, labels_rc=0))
    ob.audit("acme/new", "New")
    out = capsys.readouterr().out
    assert "❌ **Labels (1/" in out and "...)" in out
    assert "❌ **Registered in projects.json**" in out
    assert "❌ **Branch protection" in out
    assert "unreadable with this token" in out


def test_audit_labels_unreadable(hub, capsys):
    use(FakeGH(labels_rc=1))
    ob.audit("acme/demo", "Demo")
    assert "Labels (unreadable)" in capsys.readouterr().out


def fake_clone(existing=()):
    """run/sh seam: `gh repo clone` creates the dir content; git commands succeed."""
    def sh(args, check=True, cwd=None):
        if args[:3] == ["gh", "repo", "clone"]:
            td = args[4]
            for p in existing:
                os.makedirs(os.path.dirname(os.path.join(td, p)) or td, exist_ok=True)
                open(os.path.join(td, p), "w").write("kept")
        return ""
    return sh


def test_apply_seeds_pushes_and_registers(hub, monkeypatch):
    fake = use(FakeGH(labels=all_labels(), secrets={"secrets": []}))
    seeded = {}
    real_seed = ob.seed_files

    def spy(td, *a):
        out = real_seed(td, *a)
        for root, _, files in os.walk(td):
            for f in files:
                p = os.path.join(root, f)
                seeded[os.path.relpath(p, td)] = open(p).read()
        return out
    pushes = []

    def run(args, cwd=None):
        if "push" in args:
            pushes.append(args)
            return cp(1, "", "rejected") if len(pushes) == 1 else cp(0)
        return cp(0)
    os.makedirs(hub / "templates")
    (hub / "templates" / "AGENTS.md.template").write_text("# __PROJECT__ on __BRANCH__\n")
    with mock.patch.object(ob, "sh", fake_clone(existing=["docs/lessons.md"])), \
            mock.patch.object(ob, "run", run), mock.patch.object(ob, "seed_files", spy):
        assert ob.main(["apply", "acme/new", "New App"]) == 0
    assert "acme/squad-hub/.github/workflows/router.yml@" + "f" * 40 + "  # v1.0.0" in \
        seeded[".github/workflows/agents.yml"]
    assert ".github/workflows/squadhq-agent.yml" not in seeded
    assert seeded["CLAUDE.md"].startswith("# New App") and "main" in seeded["CLAUDE.md"]
    assert seeded["AGENTS.md"] == "# New App on main\n"
    assert seeded["docs/lessons.md"] == "kept"
    assert len(pushes) == 3  # branch push retried once, then the registry push
    reg = json.loads((hub / "config" / "projects.json").read_text())
    assert any(p["repo"] == "acme/new" for p in reg["projects"])
    assert any(c[:2] == ["pr", "create"] for c in fake.calls)
    assert any(c[:3] == ["label", "create", "project:new app"] for c in fake.calls)


def test_apply_push_failure_stops_before_pr(hub, capsys):
    fake = use(FakeGH())
    with mock.patch.object(ob, "sh", fake_clone()), \
            mock.patch.object(ob, "run", lambda args, cwd=None: cp(1, "", "denied") if "push" in args else cp(0)):
        assert ob.main(["apply", "acme/new", "New"]) == 1
    assert "PUSH FAILED" in capsys.readouterr().err
    assert not any(c[:2] == ["pr", "create"] for c in fake.calls)


def test_apply_existing_project_keeps_branch_and_registry(hub):
    use(FakeGH(files={"CLAUDE.md"}))
    with mock.patch.object(ob, "sh", fake_clone(existing=[
            ".github/workflows/agents.yml", "CLAUDE.md", "docs/adr/0000-template.md",
            "docs/adr/0001-record-architecture-decisions.md", "docs/lessons.md"])), \
            mock.patch.object(ob, "run", lambda args, cwd=None: cp(0)):
        assert ob.apply("acme/demo", "Demo") == 0
    reg = json.loads((hub / "config" / "projects.json").read_text())
    assert len(reg["projects"]) == 1


def test_register_commit_failure(hub):
    use(FakeGH())
    with mock.patch.object(ob, "run", lambda args, cwd=None: cp(1, "", "nothing") if "commit" in args else cp(0)), \
            mock.patch.object(ob, "sh", lambda *a, **k: ""), pytest.raises(ob.PushError):
        ob.register("acme/x", "X", "main")


def test_seed_refuses_leftover_placeholder(tmp_path):
    src = tmp_path / "t.md"
    src.write_text("hello __UNKNOWN__")
    with pytest.raises(RuntimeError, match="__UNKNOWN__"):
        ob.seed(str(tmp_path), "out.md", str(src))


def test_seams_and_helpers(hub, tmp_path):
    use(FakeGH())
    assert ob.gh(["issue", "list"]) == ""
    with mock.patch.object(github.client(), "run", return_value=cp(1, "", "x")), pytest.raises(RuntimeError):
        ob.gh(["boom"])
    assert ob.sh(["true"]) == ""
    with pytest.raises(RuntimeError):
        ob.sh(["false"])
    assert ob.run(["true"]).returncode == 0
    ob.write_step_summary("no summary set")  # no-op


def test_apply_server_mode_seeds_agent_stub(hub, monkeypatch):
    use(FakeGH(labels=all_labels(), secrets={"secrets": []}))
    seeded = {}
    real_seed = ob.seed_files

    def spy(td, *a):
        out = real_seed(td, *a)
        p = os.path.join(td, ".github", "workflows", "squadhq-agent.yml")
        if os.path.exists(p):
            seeded["server"] = open(p).read()
        return out
    monkeypatch.setenv("SQUADHQ_DEPLOYMENT", "server")
    with mock.patch.object(ob, "sh", fake_clone()), mock.patch.object(ob, "run", lambda args, cwd=None: cp(0)), \
            mock.patch.object(ob, "seed_files", spy):
        assert ob.main(["apply", "acme/new", "New"]) == 0
    assert "agent.yml@" + "f" * 40 in seeded["server"]


def test_apply_unresolvable_hub_ref_fails_before_seeding(hub, monkeypatch, capsys):
    def boom(hub=None, ref=None):
        raise ob.stub_sync.StubError("cannot resolve hub ref 'v9'")
    monkeypatch.setattr(ob.stub_sync, "placeholders", boom)
    fake = use(FakeGH())
    assert ob.main(["apply", "acme/new", "New", "--ref", "v9"]) == 1
    assert "cannot resolve" in capsys.readouterr().err
    assert not any(c[:2] == ["pr", "create"] for c in fake.calls)


def test_audit_unreachable_header_first_and_404_vs_auth(hub, capsys, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    use(FakeGH(reachable=False))
    assert ob.main(["audit", "acme/demo", "Demo App"]) == 1
    out = capsys.readouterr().out
    assert out.index("# Onboarding audit: Demo App (acme/demo)") < out.index("FATAL")
    assert "not found" in out and "keyring" in out and "GH_TOKEN" not in out
    monkeypatch.setenv("GH_TOKEN", "x")
    msg = ob.access_failure("acme/demo", "HTTP 401: Bad credentials")
    assert "authentication failed" in msg and "$GH_TOKEN" in msg and "Re-mint" in msg
    monkeypatch.delenv("GH_TOKEN")
    assert "gh auth login" in ob.access_failure("acme/demo", "To get started with GitHub CLI, run: gh auth login")
    assert "no output from gh" in ob.access_failure("acme/demo", "")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    assert ob.credential_source() == "token from $GITHUB_TOKEN"


def test_audit_rerun_command_quotes_project_name(hub, capsys):
    use(FakeGH())
    ob.main(["audit", "acme/demo", "Demo App"])
    assert "python -m squadhq.onboard audit acme/demo 'Demo App'" in capsys.readouterr().out


@pytest.fixture
def hub_with_packs(hub):
    shutil.copytree(os.path.join(ROOT, "packs"), hub / "packs")
    shutil.copy(os.path.join(ROOT, "onboarding", "CLAUDE.md.template"), hub / "onboarding" / "CLAUDE.md.template")
    return hub


def test_apply_tailors_starter_files_to_detected_packs(hub_with_packs):
    fake = use(FakeGH(labels=all_labels(), secrets={"secrets": []}))
    seeded = {}

    def sh(args, check=True, cwd=None):
        if args[:3] == ["gh", "repo", "clone"]:
            td = args[4]
            for rel, text in {"web/package.json": "{}", "web/next.config.js": "", "api/pyproject.toml": "",
                              "api/.importlinter": "kept", "go.mod": "module x\n",
                              "pom.xml": "<project/>"}.items():
                os.makedirs(os.path.dirname(os.path.join(td, rel)) or td, exist_ok=True)
                open(os.path.join(td, rel), "w").write(text)
        if args[0] == "git" and "commit" in args:
            for root, _, files in os.walk(cwd):
                for f in files:
                    p = os.path.join(root, f)
                    seeded[os.path.relpath(p, cwd)] = open(p).read()
        return ""
    with mock.patch.object(ob, "sh", sh), mock.patch.object(ob, "run", lambda args, cwd=None: cp(0)):
        assert ob.apply("acme/new", "New") == 0
    claude = seeded["CLAUDE.md"]
    assert "TODO(owner)" in claude and "## Layering rules" in claude and "## Do not" in claude
    assert "`node-next` — Next.js / React (TypeScript) in `web/`" in claude
    assert "npx --no-install depcruise --config .dependency-cruiser.cjs ." in claude
    assert "[python-fastapi · Structure] Routers stay thin" in claude
    assert "[java-spring] Not allowed:" in claude
    assert not any(ph in claude for ph in ("__PACKS__", "__STANDARDS__", "__DO_NOT__"))
    # Arch starters next to each pack's root; never over an existing file.
    assert seeded["web/.dependency-cruiser.cjs"].startswith("// dependency-cruiser rules")
    assert seeded["api/.importlinter"] == "kept"
    assert "arch-go v1.7.0" in seeded["arch-go.yml"]
    assert "[format]" in seeded["api/ruff.toml"] and ".golangci.yml" in seeded
    assert not any(p.endswith("ArchitectureTest.java") for p in seeded)  # needs the ArchUnit dependency
    body = next(c for c in fake.calls if c[:2] == ["pr", "create"])
    body = body[body.index("-b") + 1]
    assert "- [ ] `CLAUDE.md`: replace every `TODO(owner)`" in body
    assert "- [ ] `web/.dependency-cruiser.cjs`: architecture rules" in body and "dependency-cruiser@18.5.0" in body
    assert "- [ ] Architecture test for the `arch` gate (ArchUnit, pack `java-spring`): not seeded" in body
    assert "`api/.importlinter`" not in body


def test_registry_packs_win_over_detection_and_errors_degrade(hub_with_packs, tmp_path):
    td = tmp_path / "clone"
    (td / "frontend").mkdir(parents=True)
    (td / "frontend" / "next.config.js").write_text("")
    sel = ob.selected_packs(str(td), {"packs": ["python-django"], "pack_roots": {"python-django": "svc"}})
    assert [(p.id, r) for p, r in sel] == [("python-django", "svc")]
    assert [p.id for p, _ in ob.selected_packs(str(td))] == ["node-next"]
    assert ob.selected_packs(str(td), {"packs": ["ghost"]}) == []
    subs = ob.claude_md_subs([])
    assert subs["__PACKS__"].startswith("- TODO(owner): no stack pack was detected")
    seeded, notes = ob.seed_quality_files(str(td), sel)
    assert [p for p, _ in seeded] == ["svc/.importlinter", "svc/ruff.toml"] and notes == []
    assert ob.seed_quality_files(str(td), sel) == ([], [])  # second run: everything exists
    with pytest.raises(RuntimeError, match="outside the clone"):
        ob.seed_copy(str(td), "../escape", __file__)


def test_pr_body_without_starters():
    body = ob.pr_body(["stub"])
    assert "- [ ] Review the stub." in body and "After merging" in body


def test_section_bullets(tmp_path):
    md = tmp_path / "x.md"
    md.write_text("# T\n\n## One\n- first\n  continued\n- second\n\n## REQUEST CHANGES (x)\n- a\n- b\ntext\n")
    assert ob.section_bullets(str(md)) == [("One", "first continued"), ("REQUEST CHANGES (x)", "a")]
    assert ob.section_bullets(str(md), per_section=5, only="REQUEST") == [("REQUEST CHANGES (x)", "a"),
                                                                        ("REQUEST CHANGES (x)", "b")]
    assert ob.section_bullets(str(tmp_path / "none.md")) == []
