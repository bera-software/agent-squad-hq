## What and why

<!-- One change per PR. Link the issue: "Closes #123". -->

## How it was tested

<!-- Commands you ran, fixtures you added. Tests never make network calls. -->

## Security impact

<!--
Does this change which events can start an LLM job, what an agent may run or push,
which labels a role may apply, token scopes or permissions, the risk floor or merge policy,
how untrusted text is fenced, or server authentication? Explain, or write "none".
Security-sensitive paths need two maintainer approvals (GOVERNANCE.md).
-->

## Checklist

- [ ] Commits are signed off (`git commit -s`, see CONTRIBUTING.md).
- [ ] `ruff check src tests`, `mypy` on the typed subpackages and `pytest` pass.
- [ ] Generated files are up to date (schema, `docs/FLOW-DIAGRAM.md`, `docs/skills/CATALOGUE.md`, `squadhq render --check --config squadhq.example.yml`).
- [ ] New behaviour has tests; new adapters have recorded fixtures.
- [ ] Workflow changes: actions pinned by SHA, least permissions, no `${{ }}` inside `run:`, actionlint and zizmor clean.
- [ ] Untrusted text is fenced; no new path lets an untrusted author trigger an LLM job.
- [ ] Docs and `CHANGELOG.md` (Unreleased) are updated.
