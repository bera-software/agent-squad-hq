# React Native (Expo) — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- TypeScript `strict`; no new `any` or suppressions without a reason.
- Use Expo SDK modules and `npx expo install` for native packages so
  versions match the SDK. Prefer config plugins over editing `ios/`/`android/`
  directly in managed or CNG projects.
- Everything in the JS bundle is public: `EXPO_PUBLIC_*` variables,
  `app.config` `extra` and source code. Never ship secrets; call your backend.
- Tokens in `expo-secure-store`, not AsyncStorage.
- Deep links / universal links: treat every URL parameter as untrusted;
  validate before navigating or acting.
- Network: HTTPS only; do not add ATS exceptions or cleartext permissions.
- Every async screen has loading, error and empty states, and handles
  offline.
- Accessibility: `accessibilityLabel`/`accessibilityRole` on interactive
  elements; respect font scaling; touch targets at least 44pt.
- Performance: `FlatList`/`FlashList` for long lists, memoise expensive
  rows, avoid heavy work on the JS thread.
- Tests: Jest + React Native Testing Library for components/hooks with logic.
  Run lint, typecheck and tests before pushing.
- EAS builds, OTA updates to production channels, signing credentials and
  store submissions are human-only.

## Quality gates
- Before pushing, run the architecture check `npx --no-install depcruise --config .dependency-cruiser.cjs .` (the `arch` gate runs it
  whenever `.dependency-cruiser.cjs` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `npm run lint && npx --no-install prettier --check .` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
