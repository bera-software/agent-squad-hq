# Flutter — review rules

Apply only to this pack's files. Formatting is owned by `dart format` and
lints by `flutter analyze`.

## REQUEST CHANGES
- Secrets in source, assets or `--dart-define` config.
- Tokens or personal data in `shared_preferences`, files or logs (`print`).
- `badCertificateCallback` accepting all certificates; cleartext/ATS
  exceptions.
- Using `BuildContext` across async gaps without a `mounted` check; leaked
  controllers/subscriptions.
- Deep link or platform channel input used without validation.
- New platform permissions without usage strings and a clear need.
- WebView with JavaScript enabled on untrusted origins plus JS channels.

## Security checklist (OWASP MASVS / ASVS-aligned)
- Storage: secure storage for secrets; backups exclude sensitive data.
- Network: TLS only; pinning not silently removed.
- Auth: OAuth through the system browser with PKCE.
- Obfuscation (`--obfuscate`) settings unchanged for release.
- Dependencies: new pub packages justified (publisher, maintenance, native
  code they add).

## Observations (non-blocking)
- Rebuild hotspots, missing `const`, oversized widgets, accessibility gaps.

## Quality gates
- A failing `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
