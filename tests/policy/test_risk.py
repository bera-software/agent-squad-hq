"""squadhq.policy.risk — the deterministic risk floor. 100% branch coverage
is a release gate for this module (docs/adr/0006-deterministic-risk-floor.md)."""
import io
import json

import pytest

from squadhq.policy import risk


@pytest.mark.parametrize("path", [
    "src/auth/Login.java", "backend/security/Filter.kt", "lib/crypto.py", "app/payments/stripe.ts",
    "billing/invoice.go", "api/OAuthController.java", "web/src/login/page.tsx",
    "db/migrations/V1__init.sql", "prisma/migrations/x/migration.sql", "schema.sql",
    "package.json", "frontend/package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pom.xml",
    "build.gradle.kts", "gradle.lockfile", "requirements.txt", "requirements-dev.txt", "poetry.lock",
    "uv.lock", "pyproject.toml", "go.mod", "go.sum", "Cargo.lock", "Gemfile.lock", "app/App.csproj",
    ".github/workflows/ci.yml", ".github/actions/setup/action.yml", ".gitlab-ci.yml", "Jenkinsfile",
    "infra/main.tf", "deploy/helm/values.yaml", "k8s/deployment.yaml", "Dockerfile", "api/Dockerfile.prod",
    "docker-compose.yml", "vercel.json", ".env", ".env.production", "certs/server.pem",
    "src/main/resources/application-prod.yml", "appsettings.Production.json", "secrets/db.txt",
    "./src/AUTH/x.py", "/.github/workflows/x.yml", "src\\auth\\win.py",
])
def test_core_paths_force_high(path):
    assert risk.floor([path]) == "high"


@pytest.mark.parametrize("path", [
    "README.md", "docs/guide.md", "src/main/java/com/acme/Order.java", "frontend/src/App.tsx",
    "graphify-out/graph.json", "docs/FLOW-DIAGRAM.md", "tests/test_orders.py",
])
def test_ordinary_paths_are_low(path):
    assert risk.floor([path]) == "low"


def test_floor_is_the_max_over_files():
    assert risk.floor(["README.md", "src/App.tsx"]) == "low"
    assert risk.floor(["README.md", "pom.xml"]) == "high"


def test_unknown_file_list_is_high_and_empty_is_low():
    assert risk.floor(None) == "high"
    assert risk.floor([]) == "low"
    assert risk.floor(["", "   "]) == "low"


def test_extra_globs_list_and_mapping():
    assert risk.floor(["docs/rfc/1.md"], ["docs/rfc/**"]) == "high"
    assert risk.floor(["docs/rfc/1.md"], {"docs/rfc/**": "medium"}) == "medium"
    assert risk.floor(["docs/rfc/1.md"], {"docs/rfc/**": "risk:low"}) == "low"
    # unknown level in a mapping fails closed to high
    assert risk.floor(["docs/rfc/1.md"], {"docs/rfc/**": "bogus"}) == "high"
    # empty globs are ignored, None/empty extras are fine
    assert risk.floor(["docs/rfc/1.md"], ["", None]) == "low"
    assert risk.floor(["docs/rfc/1.md"], None) == "low"
    assert risk.floor(["docs/rfc/1.md"], {}) == "low"


def test_explain_keeps_highest_rule_per_file():
    hits = risk.explain(["docs/a.md", "src/auth/x.py"], {"docs/**": "medium", "src/**": "medium"})
    by = {h["file"]: h for h in hits}
    assert by["docs/a.md"]["level"] == "medium"
    assert by["docs/a.md"]["category"] == "project risk path"
    assert by["src/auth/x.py"]["level"] == "high"
    assert by["src/auth/x.py"]["category"] == "security-sensitive code"


@pytest.mark.parametrize("llm,files,expected", [
    ("low", ["README.md"], "low"),
    ("risk:low", ["README.md"], "low"),
    ("Medium", ["README.md"], "medium"),
    ("high", ["README.md"], "high"),
    ("low", ["src/auth/a.py"], "high"),        # model cannot lower below the floor
    (None, ["README.md"], "high"),             # missing verdict fails closed
    ("very-low", ["README.md"], "high"),       # unrecognised verdict fails closed
    (3, ["README.md"], "high"),
    ("low", None, "high"),
])
def test_final_risk(llm, files, expected):
    assert risk.final_risk(llm, files) == expected


def test_normalize_and_max():
    assert risk.normalize_risk(" RISK:HIGH ") == "high"
    assert risk.normalize_risk("") is None
    assert risk.max_risk("low", "medium") == "medium"
    assert risk.RISK_LEVELS == ("low", "medium", "high")


def test_glob_semantics():
    assert risk.matches("a/b/c.txt", "a/**")
    assert risk.matches("c.txt", "**/c.txt")
    assert not risk.matches("a/c.txt", "*.txt")
    assert risk.matches("a.t", "a.?")
    assert not risk.matches("a/t", "a?t")
    assert risk.matches("x/yz", "x/**z")


def test_cli_prints_floor_and_matches(tmp_path, capsys):
    f = tmp_path / "files.txt"
    f.write_text("README.md\n\nsrc/auth/x.py\n")
    assert risk.main(["--files-from", str(f)]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "high"
    assert "src/auth/x.py" in err


def test_cli_final_with_llm_risk_and_extra(tmp_path, capsys):
    f = tmp_path / "files.txt"
    f.write_text("docs/a.md\n")
    assert risk.main(["--files-from", str(f), "--llm-risk", "medium"]) == 0
    assert capsys.readouterr().out.strip() == "medium"
    assert risk.main(["--files-from", str(f), "--extra-globs", "docs/**", "--llm-risk", "low"]) == 0
    assert capsys.readouterr().out.strip() == "high"


def test_cli_json_and_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("Dockerfile\n"))
    assert risk.main(["--files-from", "-", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["floor"] == "high" and data["final"] is None
    assert data["matches"][0]["file"] == "Dockerfile"


def test_cli_unreadable_file(capsys):
    assert risk.main(["--files-from", "/nonexistent/files.txt"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_invalid_llm_risk_warns(tmp_path, capsys):
    f = tmp_path / "files.txt"
    f.write_text("docs/a.md\n")
    assert risk.main(["--files-from", str(f), "--llm-risk", "tiny"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "high" and "warning" in err and "'tiny'" in err
