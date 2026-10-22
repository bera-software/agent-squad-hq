# Node.js API — review rules

Apply only to this pack's files. Formatting is linter-owned.

## REQUEST CHANGES
- A new route without authentication, or without resource-level authorization
  when it reads or changes user-owned data (IDOR).
- Request input used without runtime validation; request body spread into an
  ORM write (mass assignment); `ValidationPipe` without `whitelist`.
- String-built SQL, NoSQL operator injection, `eval`/`new Function`/`vm` on
  input, `child_process` with shell interpolation of input.
- Stack traces or internal errors returned to clients.
- Async route errors that are not caught or propagated.
- Secrets in code, config committed to the repo, or logs.

## Security checklist (OWASP ASVS-aligned)
- Authn: JWT verification checks signature, algorithm allowlist (no `none`),
  issuer, audience and expiry; sessions use secure cookie flags.
- CSRF protection for cookie-authenticated state-changing routes.
- CORS: no `origin: *` with credentials; no reflected origin.
- Path traversal: file paths from input normalised and confined to a base
  directory.
- SSRF: outbound requests to user-supplied URLs allowlisted; no following
  redirects to internal ranges.
- Prototype pollution: no deep-merge of untrusted objects into config or
  shared objects.
- Deserialization: no unsafe YAML load or `node-serialize` style libraries.
- Rate limits on login, password reset and other abuse-prone endpoints.
- Logging: redaction configured; no log injection from raw input.
- Dependencies: new packages justified; no install scripts from unknown
  publishers.

## Observations (non-blocking)
- N+1 queries, missing pagination, unbounded `findMany`, blocking sync calls
  (`fs.readFileSync`, heavy CPU) on the request path, missing timeouts on
  outbound calls.

## Quality gates
- A failing `arch` (dependency-cruiser) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `.dependency-cruiser.cjs` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
