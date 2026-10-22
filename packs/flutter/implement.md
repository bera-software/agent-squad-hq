# Flutter — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- Follow the project's state management (Riverpod, Bloc, Provider...). Do not
  add a second approach. Keep widgets small; business logic outside widgets.
- Null safety throughout; no `!` on values that can genuinely be null; no new
  `// ignore:` without a reason.
- Async: handle errors on every `Future`; cancel subscriptions in `dispose`;
  check `mounted` before using `context` after an `await`.
- Secrets are never compiled into the app (including `--dart-define`
  values, which are extractable). Tokens in `flutter_secure_storage`, not
  `shared_preferences`.
- HTTPS only; no `badCertificateCallback` returning true; no ATS/cleartext
  exceptions in platform config.
- Deep links and platform channel messages are untrusted input; validate.
- Accessibility: `Semantics` labels for custom controls, respect text scale,
  sufficient contrast.
- Performance: `const` constructors, `ListView.builder` for long lists, no
  heavy work in `build`.
- Tests: unit and widget tests (`flutter_test`); golden tests where the
  project uses them. Run `dart format`, `flutter analyze`, `flutter test`
  before pushing.
- Store builds, signing and release submissions are human-only.

## Quality gates
- Run the lint/format check `dart format --output=none --set-exit-if-changed lib test && flutter analyze` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
