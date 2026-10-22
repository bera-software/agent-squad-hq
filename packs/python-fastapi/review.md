# Python / FastAPI — review rules

Apply only to this pack's files. Formatting and import order are owned by
ruff/black: do not comment on them.

## REQUEST CHANGES
- An endpoint returning ORM objects or dicts without a `response_model`
  where internal or sensitive fields could leak.
- A request schema accepting privileged fields (`id`, `owner_id`, `role`,
  `is_admin`) from clients (mass assignment).
- f-string or `%`-built SQL, `text()` with interpolated input.
- New endpoint without the auth dependency, or without an ownership check on
  user-owned resources (IDOR).
- Blocking I/O inside `async def` request handlers.
- `pickle`, `yaml.load` (non-safe), `eval`/`exec`, `subprocess` with
  `shell=True` on untrusted input.
- Secrets hard-coded or committed; missing timeouts on outbound calls.

## Security checklist (OWASP ASVS-aligned)
- JWT: algorithm allowlist, `aud`/`iss`/`exp` verified; no `verify=False`.
- Password hashing with argon2/bcrypt; constant-time comparison for tokens.
- CORS not `*` with credentials; `TrustedHostMiddleware` where relevant.
- SSRF: outbound requests to user URLs allowlisted; no redirects to private
  ranges.
- Path traversal on file downloads/uploads; upload size and type limits.
- Logging: no tokens, passwords or personal data; no `print` debugging left.
- Dependencies pinned via the lock file; new packages justified.

## Observations (non-blocking)
- N+1 queries (missing `selectinload`), missing pagination, missing type
  hints, overly broad `except Exception`.

## Quality gates
- A failing `arch` (import-linter) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `.importlinter` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
