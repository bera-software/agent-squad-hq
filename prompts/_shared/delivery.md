# Delivery discipline

- **Push is part of the deliverable.** The runner is destroyed when you
  finish; an unpushed commit is lost. Push with `git push origin HEAD:refs/heads/<your
  branch>` (see branch discipline), confirm it exited 0, and that the PR
  shows your commit (`gh pr view <N> --json headRefOid`). If push fails, retry once. If it still
  fails, do not ask anyone to push: comment `PUSH FAILED — work lost, run
  needs re-trigger` with the error verbatim, add `needs-human`, and treat the
  run as failed. Never describe work as done unless it is pushed.
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
