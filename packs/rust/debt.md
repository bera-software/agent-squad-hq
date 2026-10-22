# Rust — debt scan

Findings from this pack use the label `dept:backend` (or `dept:frontend` for
a WASM UI crate).

Look for:
- Vulnerable or unmaintained crates: `cargo audit` (RustSec advisories get
  `severity:high`); duplicate versions via `cargo tree -d`; outdated via
  `cargo outdated` if installed.
- `unsafe` blocks without `SAFETY` comments; `unwrap()` clusters in
  non-test code; `#[allow(...)]` clusters.
- Blocking calls in async code; missing timeouts.
- Oversized modules, crates that should be split, feature-flag sprawl.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  `lcov.info`), TODO/FIXME clusters, ADR drift.
