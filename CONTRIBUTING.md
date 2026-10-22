# Contributing to Agent Squad HQ

Thank you for helping. This guide covers the development setup, the checks every change must pass, and how to add a stack pack, a skill, an LLM provider or a tracker.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through [SECURITY.md](SECURITY.md), never a public issue or PR.

## Developer Certificate of Origin

We use the [Developer Certificate of Origin 1.1](https://developercertificate.org/) (DCO) instead of a contributor licence agreement. Every commit must carry a `Signed-off-by` line with your real name and an email address you control:

```bash
git commit -s -m "fix(policy): treat *.tfvars as high risk"
# adds: Signed-off-by: Your Name <you@example.com>
```

By signing off you certify the following:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Forgot to sign off? `git commit --amend -s` fixes the last commit, and `git rebase --signoff main` fixes a branch. Contributions are licensed under the [Apache License 2.0](LICENSE).

AI-assisted contributions are welcome. You are responsible for them exactly as if you wrote them by hand: you must understand the change, be able to explain it in review, and have the right to submit it.

## Development setup

You need Python 3.11 or newer (CI uses 3.12), [`uv`](https://docs.astral.sh/uv/) (or plain `pip`), `git` and, for GitHub-facing commands, the [`gh` CLI](https://cli.github.com/). The client apps need Node 20 or newer.

```bash
git clone https://github.com/bera-software/agent-squad-hq && cd agent-squad-hq
uv venv --python 3.12 .venv
uv pip install --python .venv -e '.[dev,server]'
.venv/bin/squadhq version
```

Without `uv`: `python -m venv .venv && .venv/bin/pip install -e '.[dev,server]'`. Add the `postgres` extra to run the Postgres store tests against a real database, and `bedrock` if you work on the Bedrock adapter.

Tests never make network calls. Adapters are tested against recorded fixtures under `tests/**/fixtures/`.

## Checks

Hub CI (`.github/workflows/hub-ci.yml`) runs all of these. Run them locally before you push:

```bash
# Python
.venv/bin/ruff check src tests
.venv/bin/mypy -p squadhq.providers -p squadhq.trackers -p squadhq.notify -p squadhq.config -p squadhq.policy \
  -p squadhq.executors -p squadhq.codehost -p squadhq.skills -p squadhq.audit -p squadhq.store
.venv/bin/python -m pytest --cov=squadhq

# Configuration, packs, skills, evals
SQUADHQ_HOME=$PWD .venv/bin/python -m squadhq.flow_reader validate --strict
.venv/bin/squadhq render --check --config squadhq.example.yml
.venv/bin/python -m squadhq.packs validate
.venv/bin/python -m squadhq.skills validate
.venv/bin/python -m squadhq.skills catalogue-md --check
.venv/bin/python -m squadhq.skills.evals run --dry-run

# Client apps (only if you touched apps/)
cd apps && npm ci && npm test && npm run typecheck && npm run lint

# Documentation site
uv pip install --python .venv mkdocs-material
.venv/bin/mkdocs build --strict
```

CI also runs [actionlint](https://github.com/rhysd/actionlint) and [zizmor](https://github.com/zizmorcore/zizmor) on every workflow and on the rendered project stub, refuses actions that are not pinned to a full commit SHA, refuses `secrets: inherit` and unrestricted `Bash(*)`, and greps for references to the private predecessor project.

Generated files must stay in sync:

| If you change | Regenerate with |
|---|---|
| `src/squadhq/config/registry.py` or `schema.py` | `python -m squadhq.config.schema > schema/squadhq.schema.json` |
| `config/flows.yml` | `python -m squadhq.flow_diagram` (writes `docs/FLOW-DIAGRAM.md`) |
| any `skills/*/skill.yml` | `python -m squadhq.skills catalogue-md` (writes `docs/skills/CATALOGUE.md`) |
| `squadhq.example.yml`, schedule defaults, or a workflow cron marker | `squadhq render --config squadhq.example.yml` |

## Code style

The rules are in [docs/architecture/CONVENTIONS.md](docs/architecture/CONVENTIONS.md). In short:

- `ruff check` must pass. New subpackages are fully typed and checked by `mypy` (strict overrides in `pyproject.toml`).
- Use standard-library HTTP (`squadhq.http`) unless a dependency is unavoidable. Optional dependencies go in `[project.optional-dependencies]`.
- Every network call has a timeout. Every list call paginates or warns when it hits its cap.
- Times are UTC (`datetime.now(timezone.utc)`).
- Tests mirror `src/squadhq/` under `tests/`.
- Workflows: `permissions: {}` at the top and the least each job needs, actions pinned by SHA with the version in a comment, event text passed through `env:`, never spliced into `run:` with `${{ }}`.
- Anything an agent reads that someone outside the approver set could have written goes through `squadhq.untrusted.fence()`. Whether an event may start an LLM job is decided in code, never by a model.

## Adding a stack pack

1. Copy the pack closest to your stack under `packs/<id>/` and change `id` (it must equal the directory name), `name`, `detect.files` and `paths`. The `pack.yml` schema is in [CONVENTIONS.md](docs/architecture/CONVENTIONS.md#packs-stack-specific-behaviour).
2. Use `ci` commands that run on a clean runner without cloud credentials. Never put deploy, apply or publish commands in a pack.
3. Keep `tools` narrow (`Bash(<command> *)`). `Bash(*)` is always rejected.
4. List paths that must force `risk:high` under `risk_paths`.
5. Write `implement.md`, `review.md` (with a REQUEST CHANGES list and a security checklist) and `debt.md`.
6. Run `python -m squadhq.packs validate` and add a fixture test if detection is non-obvious.

See [packs/README.md](packs/README.md) and [docs/concepts/packs.md](docs/concepts/packs.md).

## Adding a skill

Follow [docs/skills/authoring-skills.md](docs/skills/authoring-skills.md): a `skills/<id>/skill.yml` manifest, a `prompt.md` (unless the skill is deterministic) and at least one eval under `skills/<id>/evals/`, including an injection case if the skill reads untrusted input. Then run `python -m squadhq.skills validate`, `python -m squadhq.skills catalogue-md` and `python -m squadhq.skills.evals run --dry-run`.

## Adding an LLM provider (hub loops)

1. Implement the `LLMProvider` protocol from `src/squadhq/interfaces.py` in `src/squadhq/providers/<name>.py`. Subclass `HTTPProvider` (`providers/base.py`) or, for an OpenAI-compatible API, a class in `providers/openai_compatible.py`. Raise `TransientError`, `FatalError` or `CapacityError` with `model_specific` / `provider_wide` set correctly, because the fallback policy in `providers/fallback.py` depends on them.
2. Register a factory in `_REGISTRY` in `src/squadhq/providers/__init__.py`.
3. Add prices and capabilities to `src/squadhq/providers/prices.yml`.
4. If users should configure it in `squadhq.yml`, add it to `PROVIDER_AUTH`, `PROVIDER_SECRETS` and (optionally) `KNOWN_MODELS` in `src/squadhq/config/registry.py`, then regenerate the schema.
5. Add a recorded response under `tests/providers/fixtures/` and tests in `tests/providers/`.
6. Document the environment variables in [docs/concepts/providers.md](docs/concepts/providers.md).

## Adding a tracker

1. Implement the `IssueTracker` protocol from `src/squadhq/interfaces.py` in `src/squadhq/trackers/<name>.py`, using `trackers/_common.py`. Map the author to an `AuthorAssociation` conservatively: if you cannot prove membership, return `NONE`.
2. Register it in `_REGISTRY` in `src/squadhq/trackers/__init__.py`.
3. Add a native-state map to `src/squadhq/trackers/status_maps.yml`.
4. Add webhook signature verification and normalisation in `src/squadhq/trackers/webhooks.py`, and the source to `WEBHOOK_SOURCES` in `src/squadhq/server/app.py`.
5. To make it selectable in `squadhq.yml`, add it to `TRACKERS` in `src/squadhq/config/registry.py` and to the required-keys table in `config/loader.py`, then regenerate the schema.
6. Add fixtures and tests under `tests/`, and document it in [docs/concepts/trackers.md](docs/concepts/trackers.md).

## Pull requests

- Keep each PR to one change. Open an issue first for anything large or for a new public interface.
- Use [Conventional Commits](https://www.conventionalcommits.org/) style subjects (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`).
- Add an entry under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- Changes to `src/squadhq/policy/`, `merge_policy.py`, `label_guard.py`, `untrusted.py`, `executors/tool_profiles.py`, `.github/actions/run-agent/`, `router.yml` trigger conditions or `server/auth.py` need a second maintainer's review (see [GOVERNANCE.md](GOVERNANCE.md)). Explain the security effect in the PR description.

### PR checklist

- [ ] Commits are signed off (`git commit -s`).
- [ ] `ruff`, `mypy` (typed subpackages) and `pytest` pass locally.
- [ ] Generated files are regenerated (schema, flow diagram, skill catalogue, rendered crons).
- [ ] New behaviour has tests; new adapters have recorded fixtures; there are no network calls in tests.
- [ ] Workflow changes: actions pinned by SHA, least permissions, no `${{ }}` in `run:`, actionlint and zizmor clean.
- [ ] Untrusted text is fenced; no new path lets an untrusted author start an LLM job.
- [ ] Docs and `CHANGELOG.md` are updated.
