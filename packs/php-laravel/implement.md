# PHP / Laravel — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- `declare(strict_types=1);` and typed properties/returns in new code.
- Controllers thin: validation in Form Requests, authorization in Policies
  (`$this->authorize()` / `can` middleware), logic in services or actions.
- Models declare `$fillable` explicitly (never `$guarded = []`); create and
  update with `$request->validated()`, not `$request->all()`.
- Eloquent or the query builder with bindings; `DB::raw`/`whereRaw` only with
  bound parameters.
- Every schema change is a migration with a working `down()` where feasible;
  migrations are reviewed as high risk. Never edit a migration that has run.
- Blade escapes with `{{ }}`; `{!! !!}` only for content sanitised on the way
  in, with a comment saying so.
- API resources (`JsonResource`) shape responses so hidden attributes never
  leak; use `$hidden` for secrets on models.
- Queues for slow work; jobs idempotent and retry-safe.
- Config via `config()` only (never `env()` outside config files), secrets in
  `.env`, never committed.
- Eager-load relations (`with()`) to avoid N+1; paginate lists.
- Tests: Pest or PHPUnit feature tests with `RefreshDatabase`, covering guest,
  forbidden and allowed cases. Run `php artisan test` before pushing.

## Quality gates
- Before pushing, run the architecture check `vendor/bin/deptrac analyse --config-file=deptrac.yaml` (the `arch` gate runs it
  whenever `deptrac.yaml` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `vendor/bin/pint --test` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
