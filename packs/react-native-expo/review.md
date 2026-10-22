# React Native (Expo) — review rules

Apply only to this pack's files. Formatting is linter-owned.

## REQUEST CHANGES
- Secrets in source, `EXPO_PUBLIC_*` variables or `app.config` `extra`.
- Tokens or personal data in AsyncStorage or logs.
- Deep link handlers acting on unvalidated parameters.
- ATS exceptions (`NSAllowsArbitraryLoads`) or Android cleartext enabled
  without justification.
- WebView (`react-native-webview`) loading untrusted origins with
  `javaScriptEnabled` plus `onMessage` bridges that trust the page.
- New native permission (camera, location, contacts...) without a usage
  string and a clear need.
- Native module versions not aligned with the Expo SDK.

## Security checklist (OWASP MASVS / ASVS-aligned)
- Storage: SecureStore for secrets; no sensitive data in screenshots of
  backgrounded app where the project requires it.
- Network: TLS only; certificate pinning not silently removed.
- Auth: OAuth via the system browser (`expo-auth-session`) with PKCE.
- OTA updates: code signing and channel config unchanged unless intentional.
- Third-party SDKs: privacy manifest / data collection reviewed.

## Observations (non-blocking)
- Re-render hotspots, large images not optimised, missing list
  virtualisation, accessibility gaps.

## Quality gates
- A failing `arch` (dependency-cruiser) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `.dependency-cruiser.cjs` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
