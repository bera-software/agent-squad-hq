"""Deterministic, path-based risk floor.

``final risk = max(llm_risk, floor(changed files))`` — a model can raise the
risk of a change, never lower it below what the touched paths imply
(docs/adr/0006-deterministic-risk-floor.md, docs/architecture/CONVENTIONS.md "Risk").

The core globs below cover the change classes that must never auto-merge on a
model's say-so: authentication / authorisation / crypto / payments / billing
code, schema migrations, dependency manifests and lockfiles, CI/CD workflow
definitions, infrastructure-as-code and container definitions, and secret or
environment configuration. Stack packs add their own ``risk_paths``; callers
pass those as ``extra_globs``.

Matching is case-insensitive and deliberately over-inclusive (``**/*auth*``
also matches ``AUTHORS.md``): a false "high" costs one human click, a false
"low" can ship an unreviewed auth change.

CLI::

    python -m squadhq.policy.risk --files-from changed.txt [--extra-globs G ...]
                                  [--llm-risk low|medium|high] [--json]

prints the floor (``low`` | ``medium`` | ``high``) on stdout — or, with
``--llm-risk``, the final risk — and the matched paths on stderr. ``--json``
prints ``{"floor", "final", "matches"}`` instead. Exit code is always 0 on a
successful evaluation, 2 on bad usage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache

RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")
_RANK = {name: i for i, name in enumerate(RISK_LEVELS)}

# Word fragments that mark security-sensitive code wherever they appear in a
# file or directory name.
_SENSITIVE_TOKENS = (
    "auth", "oauth", "oidc", "saml", "jwt", "login", "password", "passwd", "credential",
    "security", "crypto", "cipher", "encrypt", "decrypt", "signing", "permission", "rbac",
    "payment", "billing", "checkout", "invoice", "stripe", "wallet",
    "secret",
)

_SENSITIVE_GLOBS = tuple(g for t in _SENSITIVE_TOKENS for g in (f"**/*{t}*", f"**/*{t}*/**"))

_MIGRATION_GLOBS = (
    "**/migrations/**", "**/migration/**", "**/migrate/**", "**/db/changelog/**",
    "**/flyway/**", "**/liquibase/**", "**/alembic/**", "**/*.sql", "**/schema.prisma",
    "**/prisma/migrations/**",
)

_DEPENDENCY_GLOBS = (
    # JavaScript / TypeScript
    "**/package.json", "**/package-lock.json", "**/npm-shrinkwrap.json", "**/yarn.lock",
    "**/pnpm-lock.yaml", "**/pnpm-workspace.yaml", "**/bun.lockb", "**/bun.lock", "**/.npmrc", "**/.yarnrc*",
    # JVM
    "**/pom.xml", "**/pom.xml.lock", "**/build.gradle", "**/build.gradle.kts", "**/settings.gradle",
    "**/settings.gradle.kts", "**/gradle.lockfile", "**/libs.versions.toml", "**/gradle/wrapper/**",
    "**/gradle.properties", "**/.mvn/**", "**/settings.xml",
    # Python
    "**/requirements*.txt", "**/requirements/**", "**/constraints*.txt", "**/pipfile", "**/pipfile.lock",
    "**/pyproject.toml", "**/poetry.lock", "**/uv.lock", "**/pdm.lock", "**/setup.py", "**/setup.cfg",
    "**/.pypirc",
    # Go / Rust / Ruby / PHP / .NET / others
    "**/go.mod", "**/go.sum", "**/go.work", "**/cargo.toml", "**/cargo.lock", "**/gemfile", "**/gemfile.lock",
    "**/*.gemspec", "**/composer.json", "**/composer.lock", "**/*.csproj", "**/*.fsproj", "**/*.vbproj",
    "**/packages.config", "**/packages.lock.json", "**/directory.packages.props", "**/directory.build.props",
    "**/nuget.config", "**/mix.exs", "**/mix.lock", "**/podfile", "**/podfile.lock", "**/package.swift",
    "**/package.resolved", "**/pubspec.yaml", "**/pubspec.lock", "**/cartfile*", "**/deno.json",
    "**/deno.lock", "**/flake.nix", "**/flake.lock",
)

_CI_GLOBS = (
    ".github/workflows/**", ".github/actions/**", "**/action.yml", "**/action.yaml",
    ".github/codeowners", "**/codeowners", ".github/dependabot.yml", ".github/dependabot.yaml",
    "**/.gitlab-ci.yml", "**/.gitlab/**", "**/jenkinsfile", "**/.circleci/**", "**/azure-pipelines*.yml",
    "**/bitbucket-pipelines.yml", "**/.buildkite/**", "**/.pre-commit-config.yaml",
)

_IAC_GLOBS = (
    "**/*.tf", "**/*.tfvars", "**/*.tf.json", "**/*.hcl", "**/terraform/**", "**/terragrunt/**",
    "**/helm/**", "**/charts/**", "**/chart.yaml", "**/k8s/**", "**/kubernetes/**", "**/kustomization.yaml",
    "**/kustomization.yml", "**/manifests/**", "**/dockerfile", "**/dockerfile.*", "**/*.dockerfile",
    "**/containerfile", "**/docker-compose*.yml", "**/docker-compose*.yaml", "**/compose.yml",
    "**/compose.yaml", "**/.dockerignore", "**/*.bicep", "**/cloudformation/**", "**/cdk.json",
    "**/pulumi*.yaml", "**/serverless.yml", "**/serverless.yaml", "**/vercel.json", "**/railway.json",
    "**/railway.toml", "**/fly.toml", "**/netlify.toml", "**/app.yaml", "**/procfile", "**/nginx*.conf",
    "**/ansible/**", "**/playbooks/**",
)

_SECRETS_CONFIG_GLOBS = (
    "**/.env", "**/.env.*", "**/*.env", "**/*.pem", "**/*.key", "**/*.crt", "**/*.p12", "**/*.pfx",
    "**/*.jks", "**/*.keystore", "**/*.gpg", "**/*.asc", "**/.netrc", "**/id_rsa*", "**/id_ed25519*",
    "**/secrets/**", "**/vault/**", "**/application*.properties", "**/application*.yml",
    "**/application*.yaml", "**/bootstrap*.yml", "**/appsettings*.json", "**/web.config",
    "**/config/prod*/**", "**/config/production*", "**/*.sops.*", "**/.sops.yaml",
)

#: Every core glob forces ``high``. Grouped for documentation and tests.
CORE_GLOBS: Mapping[str, tuple[str, ...]] = {
    "security-sensitive code": _SENSITIVE_GLOBS,
    "schema migration": _MIGRATION_GLOBS,
    "dependency manifest / lockfile": _DEPENDENCY_GLOBS,
    "CI/CD definition": _CI_GLOBS,
    "infrastructure-as-code / container": _IAC_GLOBS,
    "secrets / environment config": _SECRETS_CONFIG_GLOBS,
}


@lru_cache(maxsize=4096)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """picomatch-style subset: ``**/`` spans zero or more directories, ``**``
    anything, ``*`` stops at ``/``, ``?`` is one non-separator character.
    Anchored, case-insensitive."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$", re.IGNORECASE)


def matches(path: str, pattern: str) -> bool:
    return glob_to_regex(pattern).match(_clean(path)) is not None


def _clean(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def normalize_risk(value: object) -> str | None:
    """``"risk:low"`` / ``"Low"`` / ``"low"`` -> ``"low"``; anything else -> None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower().removeprefix("risk:")
    return v if v in _RANK else None


def max_risk(*levels: str) -> str:
    return max(levels, key=lambda lv: _RANK[lv])


def _extra_rules(extra_globs: Iterable[str] | Mapping[str, str] | None) -> list[tuple[str, str, str]]:
    """-> [(glob, level, category)]. A plain iterable means "high" for each
    glob; a mapping may name a level per glob (``"**/docs/**": "medium"``).
    An unknown level counts as high (fail closed)."""
    if not extra_globs:
        return []
    if isinstance(extra_globs, Mapping):
        return [(g, normalize_risk(lv) or "high", "project risk path") for g, lv in extra_globs.items()]
    return [(g, "high", "project risk path") for g in extra_globs if g]


def explain(files: Iterable[str], extra_globs: Iterable[str] | Mapping[str, str] | None = None
            ) -> list[dict[str, str]]:
    """Every changed file that raises the floor, with the first rule that hit
    it: ``[{"file", "glob", "category", "level"}]``."""
    rules = [(g, "high", cat) for cat, globs in CORE_GLOBS.items() for g in globs]
    rules += _extra_rules(extra_globs)
    out = []
    for f in files:
        if not f or not f.strip():
            continue
        best: tuple[str, str, str] | None = None
        for g, level, cat in rules:
            if matches(f, g) and (best is None or _RANK[level] > _RANK[best[1]]):
                best = (g, level, cat)
        if best:
            out.append({"file": _clean(f), "glob": best[0], "category": best[2], "level": best[1]})
    return out


def floor(files: Iterable[str] | None, extra_globs: Iterable[str] | Mapping[str, str] | None = None) -> str:
    """The lowest risk the change may carry. ``files=None`` (the file list
    could not be read) is ``high``: an unknown change is a risky change."""
    if files is None:
        return "high"
    hits = explain(files, extra_globs)
    return max_risk("low", *(h["level"] for h in hits))


def final_risk(llm_risk: object, files: Iterable[str] | None,
               extra_globs: Iterable[str] | Mapping[str, str] | None = None) -> str:
    """``max(llm_risk, floor)``. A missing or unrecognised model verdict is
    ``high`` — the model failing to classify is not evidence of low risk."""
    llm = normalize_risk(llm_risk) or "high"
    return max_risk(llm, floor(files, extra_globs))


def _read_files(source: str) -> list[str]:
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source) as fh:
            text = fh.read()
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m squadhq.policy.risk", description=__doc__.split("\n\n")[0])
    ap.add_argument("--files-from", required=True, help="file with one changed path per line ('-' = stdin)")
    ap.add_argument("--extra-globs", nargs="*", default=[], help="additional globs that force high risk")
    ap.add_argument("--llm-risk", help="the model's verdict; prints the final risk instead of the floor")
    ap.add_argument("--json", action="store_true", help="print a JSON object instead of a bare level")
    args = ap.parse_args(argv)
    try:
        files = _read_files(args.files_from)
    except OSError as e:
        print(f"risk: cannot read {args.files_from}: {e}", file=sys.stderr)
        return 2
    if args.llm_risk is not None and normalize_risk(args.llm_risk) is None:
        print(f"risk: warning: --llm-risk {args.llm_risk!r} is not one of {', '.join(RISK_LEVELS)}; "
              "treating it as high", file=sys.stderr)
    hits = explain(files, args.extra_globs)
    fl = floor(files, args.extra_globs)
    final = final_risk(args.llm_risk, files, args.extra_globs) if args.llm_risk is not None else None
    if args.json:
        print(json.dumps({"floor": fl, "final": final, "matches": hits}))
    else:
        print(final or fl)
        for h in hits:
            print(f"  {h['file']}: {h['category']} ({h['glob']}) -> {h['level']}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
