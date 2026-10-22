# Go — review rules

Apply only to this pack's files. Formatting is owned by gofmt and the
project's linter (golangci-lint): do not comment on it.

## REQUEST CHANGES
- Ignored errors on I/O, DB, crypto or parsing calls; `panic` on expected
  failure paths.
- SQL built with `fmt.Sprintf` or string concatenation of input.
- `exec.Command("sh", "-c", ...)` with input; unchecked file paths from input
  (`filepath.Clean` plus base-dir confinement required).
- Goroutines without a cancellation/exit path; data races; unclosed
  `rows`/response bodies.
- HTTP server without timeouts or body limits; outbound `http.Get` with the
  default client on user-influenced URLs.
- `math/rand` for security tokens; `InsecureSkipVerify: true`.
- New handler without authn, or user-owned resource access without an
  ownership check (IDOR).

## Security checklist (OWASP ASVS-aligned)
- JWT/session verification: algorithm pinned, claims validated.
- `html/template` (not `text/template`) for HTML output.
- SSRF: outbound requests to user URLs allowlisted, redirects checked.
- Deserialization: size limits on JSON decoding (`MaxBytesReader`);
  `DisallowUnknownFields` where strictness matters.
- Secrets from environment/secret store, never logged.
- `govulncheck ./...` clean for new dependencies.

## Observations (non-blocking)
- Exported identifiers without doc comments, overly large interfaces,
  premature abstractions, N+1 queries, missing pagination.

## Quality gates
- A failing `arch` (arch-go) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `arch-go.yml` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
