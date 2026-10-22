# Engineering department charter

**Scope:** project repos. **Agents** (all run by the hub's workflows; there
are no per-project agent workflows):
- Product Owner — shapes new issues and replies (`prompts/product-owner.md`).
- Architect — design gate and quarterly review (`prompts/design.md`,
  `prompts/quarterly-review.md`).
- Engineer — implements tickets and bounded fix rounds (`prompts/implement.md`).
- PR Reviewer — verdict and risk label (`prompts/review.md`).
- Preview QA — smoke tests against preview deployments where configured.
- Tech debt scanner — weekly, per pack (`prompts/debt-scan.md`).

Stack-specific rules come from the packs each project selects (`packs/`).

**KPIs:** merged PRs per week; changed-file coverage at or above the pack's
`min_changed`; debt resolved vs created; zero `severity:high` open longer
than 48 hours.

**Authority:** may create branches, PRs, issues and comments. May not merge,
deploy, edit workflows, change CI thresholds, or change `CLAUDE.md` or ADRs
without a design PR approved by the owner (or approvers).

**Escalation:** ambiguity → `needs-human`; architecture questions →
`needs-design`; CI gaps → `escalation` in the hub.
