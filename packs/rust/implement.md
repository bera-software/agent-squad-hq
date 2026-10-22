# Rust — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- Errors: `Result` everywhere fallible; libraries define error types
  (`thiserror`), binaries may use `anyhow` with context. No `unwrap()` /
  `expect()` on input-dependent paths outside tests (an `expect` with a
  message proving the invariant is acceptable).
- No new `unsafe` unless unavoidable; every `unsafe` block has a
  `// SAFETY:` comment stating the invariants, and gets a test.
- Async (tokio): never block the runtime (`std::thread::sleep`, blocking I/O,
  heavy CPU) — use `spawn_blocking`. Set timeouts on network calls.
- Web services (axum/actix): extract and validate input with typed
  extractors and validation (e.g. `validator`); body size limits; auth as
  middleware/extractor, resource-level checks in handlers or services.
- SQL via `sqlx`/`diesel` with bound parameters; `sqlx` compile-time checked
  queries where the project uses them. Migrations reviewed as high risk.
- Secrets via environment; wrap them in a type that does not implement
  `Debug`/`Display` (e.g. `secrecy::SecretString`).
- Use `rand::rngs::OsRng`/`getrandom` for tokens; vetted crates for crypto
  (no hand-rolled primitives).
- Keep `Cargo.lock` committed for binaries; justify each new dependency and
  prefer minimal feature flags.
- Tests: unit tests in-module, integration tests in `tests/`; property tests
  where parsing is involved. Run `cargo fmt`, `cargo clippy -D warnings`,
  `cargo test` before pushing.

## Quality gates
- Run the lint/format check `cargo fmt --all -- --check && cargo clippy --all-targets --all-features -- -D warnings` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
