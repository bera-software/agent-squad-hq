# Flutter — debt scan

Findings from this pack use the label `dept:frontend` (mobile client).

Look for:
- Outdated packages: `dart pub outdated`; discontinued packages; Flutter/Dart
  SDK constraints behind stable. Packages with known advisories get
  `severity:high`.
- `// ignore:` and `ignore_for_file` clusters; analyzer warnings.
- `!` null-assertion clusters, `print` statements, plain-text token storage.
- Oversized widgets/files, business logic in widgets, duplicated widgets.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  `coverage/lcov.info`), TODO/FIXME clusters, ADR drift.
