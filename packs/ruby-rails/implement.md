# Ruby on Rails — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- Strong parameters: `params.require(:x).permit(...)` listing only fields
  the caller may set; never `permit!`.
- Authorization through the project's policy layer (Pundit, Action Policy,
  CanCanCan) on every action; scope queries to the current user/tenant
  (`current_user.projects.find(params[:id])`).
- Active Record with placeholders/hash conditions only; never interpolate
  input into `where`, `order`, `pluck` or `find_by_sql`.
- Every schema change is a migration (reversible `change` or explicit
  `up`/`down`); commit the updated `schema.rb`/`structure.sql`. Large-table
  changes use safe patterns (add column nullable, backfill in batches,
  concurrent indexes). Reviewed as high risk.
- Views escape by default; `html_safe`/`raw` only on sanitised content.
- Background work in Active Job/Sidekiq, idempotent and retry-safe.
- Secrets in credentials or environment, never committed in plain text.
- Avoid N+1 (`includes`/`preload`; Bullet or `strict_loading` if present);
  paginate lists.
- Tests: RSpec or Minitest; request specs for new endpoints covering guest,
  forbidden and allowed. Run rubocop, brakeman and the test suite before
  pushing.

## Quality gates
- Run the lint/format check `bundle exec rubocop` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
