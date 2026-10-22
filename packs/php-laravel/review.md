# PHP / Laravel — review rules

Apply only to this pack's files. Formatting is owned by Pint/PHP-CS-Fixer;
static analysis by PHPStan/Larastan.

## REQUEST CHANGES
- `$guarded = []`, missing `$fillable`, or `create($request->all())`
  (mass assignment).
- Route or controller action without auth middleware or a Policy check on
  user-owned resources (IDOR).
- `whereRaw`/`DB::raw`/`DB::select` with interpolated input.
- `{!! !!}` on user content; `unserialize()` on untrusted data.
- Route excluded from CSRF (`VerifyCsrfToken::$except`) without
  justification.
- `env()` calls outside `config/`; secrets committed.
- Model changes without a migration, or an edited migration that already ran.

## Security checklist (OWASP ASVS-aligned)
- `APP_DEBUG=false` and `APP_ENV=production` assumptions not broken.
- Password hashing via `Hash::make`; rate limiting (`throttle`) on login and
  reset routes.
- File uploads: validated mime/size, stored on a non-public disk unless meant
  to be public, no user-controlled paths.
- Open redirects on `redirect()->to($input)`.
- SSRF on `Http::get($userUrl)`; timeouts set.
- Signed URLs for sensitive links; encrypted casts for sensitive columns.
- `composer audit` clean for new packages.

## Observations (non-blocking)
- N+1 queries, missing pagination, logic in controllers or Blade, missing
  indexes on new filter columns.

## Quality gates
- A failing `arch` (deptrac) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `deptrac.yaml` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
