# iOS (Swift) — Engineer rules

CI for this pack needs a macOS runner with Xcode. Project CLAUDE.md and ADRs
override these defaults.

- Follow the existing architecture (SwiftUI + observable view models, TCA,
  or UIKit + MVVM/coordinators). Do not introduce a second pattern.
- Swift concurrency: `async`/`await` with structured tasks; UI updates on
  `@MainActor`; cancel tasks when views disappear; respect Swift 6 strict
  concurrency warnings rather than silencing them with `@unchecked Sendable`.
- No force unwraps (`!`) or `try!` on values that depend on input or the
  network.
- Security:
  - Secrets are never shipped in the app (Info.plist, xcconfig, source);
    the backend holds them.
  - Tokens and credentials in the Keychain with an appropriate
    accessibility class (e.g. `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`),
    never `UserDefaults`.
  - App Transport Security stays on; no `NSAllowsArbitraryLoads`.
  - URL schemes / universal links and push payloads are untrusted input.
  - Request only needed permissions with clear usage strings; keep the
    privacy manifest (`PrivacyInfo.xcprivacy`) accurate when adding SDKs or
    required-reason APIs.
- Accessibility: labels/traits for custom controls, Dynamic Type, sufficient
  contrast, VoiceOver order.
- Tests: XCTest or Swift Testing for view models and services; UI tests for
  critical flows. Run the build and tests before pushing.
- Signing, provisioning, TestFlight and App Store submissions are human-only;
  never edit signing settings or certificates.
