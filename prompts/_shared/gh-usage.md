# GitHub CLI usage

The tool policy enforced in code allows these forms only:

- Read: `gh issue view|list`, `gh pr view|list|diff|checks`.
- Write: `gh issue comment|create|edit|close`, `gh pr comment|create|edit|review`
  (as your role's authority allows).
- Labels: `gh issue edit <N> --add-label …/--remove-label …` and `gh pr edit
  <N> --add-label …/--remove-label …`. The label guard checks every change
  against your role's authority.
- `gh api` only for PR review comments and reviews:
  `gh api repos/<owner>/<repo>/pulls/<N>/comments` (GET to list, POST to
  add an inline comment or reply) and
  `gh api repos/<owner>/<repo>/pulls/<N>/reviews` (GET/POST).
- Never: `gh pr merge`, `gh api -X PUT|PATCH|DELETE`, GraphQL, labels via
  `gh api`, or any other endpoint. `curl`/`wget` are not available.
