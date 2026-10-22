# Python / Django — review rules

Apply only to this pack's files. Formatting is linter-owned.

## REQUEST CHANGES
- A view or API endpoint without authentication/permission classes, or a
  queryset not scoped to the caller for user-owned objects (IDOR).
- DRF serializer with `fields = "__all__"` or writable privileged fields
  (mass assignment); `ModelForm` with `exclude` instead of `fields`.
- `mark_safe`/`|safe`/`format_html` misuse on user content.
- Raw SQL / `extra()` / `RawSQL` with interpolated input.
- `@csrf_exempt` on a cookie-authenticated view without justification.
- Model changes without a migration, or an edited applied migration.
- `DEBUG=True`, `ALLOWED_HOSTS=["*"]`, or a hard-coded `SECRET_KEY` reachable
  in production settings.

## Security checklist (OWASP ASVS-aligned)
- `manage.py check --deploy` warnings not introduced: `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT`.
- Password validators and hashers not weakened.
- File uploads: size/type limits, stored outside code paths, served safely.
- Open redirects: `url_has_allowed_host_and_scheme` on `next` parameters.
- SSRF on outbound requests with user URLs; timeouts on `requests` calls.
- `pickle`/unsafe YAML on untrusted data; signed data via `django.core.signing`.
- Admin: not exposing sensitive fields; restricted to staff.
- Logging: no personal data or secrets.

## Observations (non-blocking)
- N+1 queries, missing pagination, missing DB indexes for new filters,
  logic in views that belongs in models/services.

## Quality gates
- A failing `arch` (import-linter) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `.importlinter` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
