# Delivery discipline

- **Push is part of the deliverable.** The runner is destroyed when you
  finish; an unpushed commit is lost. Push with `git push origin HEAD:refs/heads/<your
  branch>` (see branch discipline), confirm it exited 0, and that the PR
  shows your commit (`gh pr view <N> --json headRefOid`). If push fails, retry once. If it still
  fails, do not ask anyone to push: comment `PUSH FAILED — work lost, run
  needs re-trigger` with the error verbatim, add `needs-human`, and treat the
  run as failed. Never describe work as done unless it is pushed.
- **Commands your shell accepts.** One command per call (no `;`, `&&` or
  pipes into another command). Commit with plain `git commit -m "<summary>"`,
  repeating `-m` for more paragraphs: `$`, backticks, heredocs and `-F` are
  refused. Edit files with your edit tool, not `sed -i`. A refused command is
  not done: switch to the allowed form, and a push that was refused counts as
  a failed push (above). Whatever happens, finish with a comment that says
  what you did or why you stopped.
- **Bodies with code, `$` or several paragraphs** (comments, issues, PR
  descriptions): inline shell arguments refuse `$` and backticks. Write the
  body with your Write tool to `.squadhq-out/<name>.md`, then post it with
  `python -P -m squadhq.post_body comment <number> --file .squadhq-out/<name>.md`
  (also `issue --title "..." [--label L]`, `pr --title "..." --head <branch>`,
  `edit-issue <number>`; add `--repo owner/name` for another repository).
  Short plain bodies can stay inline (`--body "..."`). `--body-file` is refused.
- **Hand-off files** (`.squadhq-out/agent-findings.json`, `.squadhq-out/pr-title.txt`,
  `.squadhq-out/pr-body.md`, when your run asks for them): create them with
  your Write tool, which creates the folder too. Never with the shell
  (`mkdir`, `cat >`, heredocs are refused), never anywhere else.
- **Session continuity.** As your last action on a PR you opened, post a PR
  comment whose first line is exactly `<!-- session-context -->`, followed
  by a `## Notes` section: key decisions, alternatives rejected and why,
  files touched and their roles, known trade-offs and open questions. A
  later fix-round or mention run reads it to continue your reasoning. Write
  it for that agent, not as a summary for humans.
- **Unattended runs.** When the run instructions say the run is unattended,
  nobody will answer questions. Never wait for a reply. If you are blocked,
  write the question into your deliverable (or comment + `needs-human` where
  your role allows) and finish.
- **Code graph.** When `graphify-out/graph.json` exists, orient with
  `graphify query "<question>"`, `graphify path "A" "B"` or `graphify explain "X"`
  before reading raw files. Whatever you changed (code, comments, docs or
  config), run `graphify update .` once, just before your final commit, and commit `graphify-out/graph.json`,
  `graphify-out/GRAPH_REPORT.md` and `graphify-out/manifest.json` when they
  changed: only those three, never `graph.html` or `graphify-out/cache/`. It
  reads code only (no model, no key) and takes seconds; keeping the graph in
  your PR spares a separate refresh PR after the merge. If the command is
  missing or fails, skip it and say so in the PR body.
