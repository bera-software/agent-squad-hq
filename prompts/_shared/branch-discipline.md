# Branch discipline

The tool policy enforced in code allows only what is below; anything else is
refused.

1. **Integration branch.** The `Branching` section of the project's
   `CLAUDE.md` names it. Never assume `main`; fall back to the repository
   default branch only if none is named. Merging into it may be a
   production release; there may be no separate promotion step.
2. **Your branch prefix.** Engineer: `agent/<name>` (normally
   `agent/issue-<N>`). Architect: `design/<name>` (normally
   `design/issue-<N>`). Fix-round and mention runs work only on the PR's own
   head branch (`agent/*` or `design/*`).
3. **Cut from latest.** `git fetch origin` then `git checkout -b <branch>
   origin/<integration>`. Never branch from whatever is checked out.
4. **Update by merging, never by rebasing.** To bring in new commits from
   the base: `git fetch origin`, `git merge origin/<base>`, resolve
   conflicts, re-run the tests, commit. `<base>` is your PR's base (the
   integration branch, or another PR's branch for a stacked PR). Never
   rebase onto the base and never force-push (any form, including
   `--force-with-lease`).
5. **Push only your own branch:** `git push origin
   HEAD:refs/heads/<prefix>/<branch>` (for example `git push origin
   HEAD:refs/heads/agent/issue-42`). Never push to the integration branch or
   any other ref.
6. **Open the PR** with `gh pr create --base <base> --head <branch>`.
7. **Status labels** go on the linked **issue**, with `gh issue edit`
   (never via `gh api`). Before writing anything: `gh issue edit <N>
   --add-label "status:in-progress" --remove-label "status:in-review"`
   (ignore removal errors). When the PR is open, swap them.
8. **Never merge** (`gh pr merge` is not available) and never enable
   auto-merge.
