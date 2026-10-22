# iOS (Swift) — review rules

Apply only to this pack's files. Style is owned by SwiftLint/SwiftFormat.

## REQUEST CHANGES
- Secrets in source, plists or xcconfig.
- Tokens or personal data in `UserDefaults`, files without data protection,
  or logs (`print`, `os_log` with `.public` on sensitive values).
- ATS exceptions; URLSession delegates that accept any server trust.
- Deep link / universal link / push payload handling without validation.
- Force unwraps or `try!` on input-dependent paths; UI work off the main
  actor; `@unchecked Sendable` added to silence warnings.
- New entitlements or permissions without justification and usage strings;
  privacy manifest not updated for new required-reason APIs or SDKs.
- WKWebView with JavaScript bridges (`WKScriptMessageHandler`) exposed to
  untrusted origins.

## Security checklist (OWASP MASVS / ASVS-aligned)
- Keychain accessibility classes appropriate; biometric protection via
  `LAContext` bound to Keychain access control where it guards secrets.
- File protection (`.completeFileProtection`) for sensitive files.
- Pasteboard and screenshots: sensitive fields not exposed where required.
- OAuth via `ASWebAuthenticationSession` with PKCE.
- Dependencies (SwiftPM/CocoaPods): new packages justified; versions pinned in
  `Package.resolved`.

## Observations (non-blocking)
- Massive view controllers / views, retain cycles in closures (missing
  `[weak self]`), accessibility gaps.
