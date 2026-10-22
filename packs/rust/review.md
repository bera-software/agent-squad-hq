# Rust — review rules

Apply only to this pack's files. Formatting is owned by rustfmt and lint by
clippy: do not comment on style clippy already enforces.

## REQUEST CHANGES
- New `unsafe` without a `// SAFETY:` justification, or unsound use.
- `unwrap()`/`expect()`/indexing that can panic on untrusted input in
  non-test code.
- Blocking calls inside async contexts.
- SQL built with `format!` from input.
- Secrets that can be printed through `Debug` or logs.
- `#[allow(clippy::...)]` added without a reason.
- New endpoint without authentication, or user-owned data without an
  ownership check.

## Security checklist (OWASP ASVS-aligned)
- Input size limits on bodies, decompression, and recursive parsers
  (DoS via deep JSON / zip bombs).
- Deserialization: `serde` with `deny_unknown_fields` where strictness
  matters; no untrusted `bincode`/`pickle`-like formats without limits.
- Path traversal on file paths from input; command execution via
  `std::process::Command` without a shell and with validated arguments.
- TLS: no `danger_accept_invalid_certs`.
- SSRF allowlists for user-supplied URLs.
- `cargo audit` clean for new dependencies; avoid unmaintained crates.

## Observations (non-blocking)
- Excess cloning, needless `Arc<Mutex<_>>`, overly generic APIs, missing docs
  on public items.

## Quality gates
- A failing `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
