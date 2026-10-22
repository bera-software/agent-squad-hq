# Project context

**Paths.** `prompts/`, `packs/`, `agents/`, `config/` and `org/` live in the
hub checkout: `.squadhq/` when you run inside a project repo, the repo root
when you run in the hub itself.

**Read first:** the project's `CLAUDE.md`, the hub's `org/CLAUDE-org.md`,
`org/lessons.md`, and the project's `docs/lessons.md` and `docs/adr/`. ADRs
are binding.

**Flow data.** `config/flows.yml` is the single source of truth for label
strings (`labels.*`), your authority and numeric limits (`roles.<role>`),
flows and idempotency rules. Use only label strings registered there. You
cannot take any action outside your role's authority. If this prompt and
`config/flows.yml` disagree on a number, `config/flows.yml` wins.

**Stack rules.** The run instructions append the rules from the packs this
project uses (`packs/<id>/<role>.md`). They are binding for files those
packs cover. If no pack rules are appended, follow `CLAUDE.md` and the
conventions of the existing code.

**Codebase navigation.** If `graphify-out/graph.json` exists, query the
knowledge graph before reading raw files (`graphify query '<question>'`,
`graphify path A B`, `graphify explain <Concept>`; see `CLAUDE.md`). If the
command is missing, install the version pinned in `CLAUDE.md`. If the graph
contradicts the code, trust the code and note the staleness. Grep or read
files for what the graph does not answer.

**CI topology.** Build, test, coverage and review run centrally from the hub
through the project's `agents.yml` stub; they do not appear as workflow
files in the project repo. Never create or modify anything under
`.github/workflows/` in a project repo. If you find a genuine CI gap,
describe it in a comment labelled `escalation`: hub changes need the
owner's (or approvers') approval. App-level build config (for example a
hosting provider's build command) is allowed; it is app config, not CI.

**GitHub CLI.** Follow `prompts/_shared/gh-usage.md`: only the listed
`gh` forms work, labels go through `gh issue edit` / `gh pr edit`, and
`gh api` is limited to PR review comments and reviews.

**Identity.** Every comment, PR body and issue body you write starts with
your role badge (for example `[Engineer] `). No exceptions.
