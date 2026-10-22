# React Native (Expo) — debt scan

Findings from this pack use the label `dept:frontend` (mobile client).

Look for:
- Expo SDK version behind supported releases; `npx expo-doctor` and
  `npx expo install --check` mismatches.
- Vulnerable packages: `npm audit --omit=dev` (high/critical get
  `severity:high`); `npm outdated`.
- AsyncStorage token storage, secrets in config.
- `any`/`@ts-ignore`/`eslint-disable` clusters, dead code.
- Oversized screens, duplicated components, inline styles duplicated instead
  of shared theme.
- Untested churn hotspots (`git log --since="90 days ago" --name-only`
  versus `coverage/lcov.info`), TODO/FIXME clusters, ADR drift.
