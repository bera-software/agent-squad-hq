# Mobile app (iOS and Android)

`apps/mobile` is an Expo (SDK 58, React Native 0.88, expo-router) client of the server REST API v1. It lets an approver:

- approve or reject merges, proposals and deploys, with biometric confirmation;
- acknowledge escalations and incidents;
- read the daily brief;
- browse recent agent runs;
- send a "build me…" request into the pipeline;
- receive push notifications for escalations and approvals.

API types, the client and the demo fixtures come from `apps/shared-api`, which the web dashboard also uses.

## Layout

```
apps/mobile/
  app/                      expo-router routes (thin wrappers)
    _layout.tsx             session provider + protected stacks
    sign-in.tsx
    (tabs)/                 index (approvals), escalations, brief, runs, request
    approval/[id].tsx       approval detail
  src/
    session.ts              expo-secure-store persistence + sign-in validation
    biometric.ts            expo-local-authentication gate
    decide.ts               biometric gate -> POST /approvals/{id}/decision
    push.ts                 expo-notifications registration -> POST /devices
    SessionContext.tsx      API client, lock/unlock, demo mode
    screens/                screen components (unit tested)
    components/             UI primitives, native Markdown renderer
  __tests__/                jest-expo + @testing-library/react-native
  app.json  eas.json  jest.config.js  jest.setup.ts  tsconfig.json
```

## Setup

You need Node `^22.13 || ^24.3 || >=26`, which is what React Native 0.88 requires. Other Node versions install with engine warnings only. You also need the Expo Go app or a development build on a device or simulator.

```bash
cd apps
npm install                  # one lockfile for shared-api, dashboard and mobile
cd mobile
npx expo start               # press i / a, or scan the QR code
npx tsc --noEmit             # type check (TypeScript strict)
npm test                     # jest-expo unit tests
npx expo-doctor              # config and dependency checks
```

## Configuration

The server URL and token are entered at sign-in. The app has no build-time environment variables, and no secret is bundled into the binary.

| Setting | Where | Notes |
|---|---|---|
| Server URL | Sign-in screen | Must be `https://`. Plain `http://` is accepted only for `localhost`, `127.0.0.1`, `10.0.2.2` (Android emulator) and `192.168.x.x`. A trailing `/api/v1` is optional. |
| API token | Sign-in screen | A server token with the `approver` role, created with `squadhq server create-token` or `POST /api/v1/tokens`. The app checks it with `GET /api/v1/whoami` before storing it. |
| Bundle id / package | `app.json` (`ios.bundleIdentifier`, `android.package`) | The placeholder is `com.example.squadhq`. Change it to an identifier you own before your first EAS build. |
| EAS project id | `app.json` (`expo.extra.eas.projectId`) | Filled in by `eas init`. Push registration is skipped while it is empty. |
| App Store Connect app id | `eas.json` (`submit.production.ios.ascAppId`) | Replace the `REPLACE_WITH_…` placeholder. |
| Play service account | `eas.json` (`submit.production.android.serviceAccountKeyPath`) | Points to `./secrets/play-service-account.json`, which is git-ignored. For CI, prefer an EAS secret file. |

## Demo mode

Tap **Try demo mode** on the sign-in screen. The app then uses `createMockFetch()` from `@squadhq/shared-api/fixtures`, an in-memory API v1 with fictional projects, approvals, runs and a brief. Nothing leaves the device.

Decisions you make are kept in memory until the app restarts. In demo mode, biometric confirmation is still requested when the device supports it, and skipped on simulators that have no enrolled biometrics.

## Security model

- **Token storage.** The URL and token live only in `expo-secure-store` (iOS Keychain, Android Keystore-backed storage), using `WHEN_UNLOCKED_THIS_DEVICE_ONLY`. They are excluded from backups and never synced to other devices. The token is never logged, rendered or sent anywhere except the configured server, as `Authorization: Bearer`.
- **App lock.** When the app relaunches with a stored session, it starts locked and asks for Face ID, Touch ID, fingerprint or the device passcode (`expo-local-authentication`). A device with no screen lock at all opens directly, because the keystore still protects the token at rest.
- **Approvals require on-device confirmation.** Approve, reject and acknowledge each run `confirmWithBiometrics()` first. If confirmation fails or is cancelled, no request is sent. With a server session, an unsecured device (no biometrics and no passcode) cannot approve at all. Rejections require a comment, and every decision ends up in the server's hash-chained audit log.
- **The server decides who may approve.** The device check is defence in depth. The server still enforces the token's role (`approver`) and returns 409 when another approver decided first.
- **Markdown is rendered without HTML.** The daily brief is parsed by `@squadhq/shared-api/markdown` into a data tree and rendered as native `<Text>`. There is no WebView or HTML, raw HTML stays literal text, and links open (through `Linking`) only for `https:`, `http:` and `mailto:` targets.
- **Input to agents is untrusted.** Requests from the intake screen are untrusted input. The server must fence them (`squadhq.untrusted.fence`) before any prompt uses them.
- **Permissions are minimal.** The app asks for biometrics and notifications. Microphone (`RECORD_AUDIO`) and overlay permissions are blocked on Android until voice intake ships.

## Push notifications

1. Run `eas init`, which writes `expo.extra.eas.projectId`.
2. Run `eas credentials` to set up push credentials: an APNs key for iOS and FCM v1 for Android. For Android you also need `google-services.json`, which is git-ignored. Provide it through an EAS secret file.
3. After sign-in, the app requests notification permission, calls `getExpoPushTokenAsync({ projectId })` and sends the token to `POST /api/v1/devices`. It skips registration on simulators, when permission is denied, and when there is no project id.
4. The server sends notifications through the Expo Push API (`https://exp.host/--/api/v2/push/send`). Notification payloads should carry ids only (for example `{ "approval_id": "…" }`) and no sensitive content.

## Build and release (EAS)

```bash
npm i -g eas-cli
eas login
eas init                                         # creates the project, writes projectId
eas build --profile development --platform ios   # dev client (simulator build)
eas build --profile preview --platform android   # internal APK
eas build --profile production --platform all    # store builds, auto-incremented
eas submit --profile production --platform ios       # -> TestFlight
eas submit --profile production --platform android   # -> Play "internal" track, draft
```

| Profile | Distribution | Use |
|---|---|---|
| `development` | internal, dev client, iOS simulator | Local development with native modules. |
| `preview` | internal (ad-hoc iOS, Android APK) | QA builds you share by link. |
| `production` | store | TestFlight and the Play internal track. Promote to production by hand in App Store Connect or the Play Console. |

Store releases stay human-gated. Agents may open PRs against `apps/mobile`, but only a person submits a build to TestFlight or the internal track and promotes it from there.

## Requested server endpoints

The app calls two endpoints that are not in the CONVENTIONS v1 table yet. Their schemas live in `apps/shared-api/src/schemas.ts`.

### `POST /api/v1/requests` (role: approver)

Request body:

```json
{ "project": "Storefront", "title": "Build me a referral page", "body": "Details…", "source": "mobile" }
```

- `project`: 1–200 characters; must be a registered project.
- `title`: 1–256 characters.
- `body`: up to 20,000 characters; defaults to `""`.
- `source`: one of `mobile`, `dashboard` or `voice`.

Response `202`:

```json
{ "id": "req_…", "project": "Storefront", "status": "queued", "ticket_key": null, "ticket_url": null, "created_at": "2026-10-21T08:30:00.000000Z" }
```

The server should:

- enqueue intake (create a tracker ticket and let the Product Owner shape it);
- write an audit event (`request.created`);
- treat `title` and `body` as untrusted input;
- return `422` for an unknown project.

### `POST /api/v1/devices` (role: viewer or higher)

Request body:

```json
{ "expo_push_token": "ExponentPushToken[xxxxxxxx]", "platform": "ios", "name": "Alice's iPhone", "topics": ["approvals", "escalations"] }
```

- `expo_push_token`: must match `^(ExponentPushToken|ExpoPushToken)\[[^\]]+\]$`.
- `platform`: `ios` or `android`.
- `name`: up to 128 characters.
- `topics`: any of `approvals`, `escalations` and `brief`.

Response `201`:

```json
{ "id": "dev_…", "created_at": "2026-10-21T08:30:00.000000Z" }
```

The server should:

- upsert by token, linking the device to the calling token's principal;
- write an audit event (`device.registered`);
- delete the device when the Expo push receipt reports `DeviceNotRegistered`.

A matching `DELETE /api/v1/devices/{id}` (called at sign-out) is also wanted but not yet used.

## Dependency audit (build-time tooling)

`npm audit` in `apps/` reports a few dozen moderate and high findings in the mobile toolchain. They sit in packages such as `@expo/cli`, `metro`, `node-forge`, `jest`, `babel-plugin-istanbul`, `js-yaml`/`argparse`/`sprintf-js`, `uuid` and `xcode`.

These are build-time only. They run on the developer's machine or the EAS builder while the app is bundled, signed or tested, and none of them ships in the app binary.

Do not run `npm audit fix --force`. Its suggested "fixes" are downgrades to unrelated majors (for example `expo@44`, `react-native@0.72` or `jest-expo@56`), which would break the SDK 58 alignment that `expo-doctor` checks.

On 2026-10-22 the SDK 58 patch releases were applied (`expo` 58.0.17, `expo-router` 58.0.25, `jest-expo` 58.0.12 and others), with `expo-doctor` still at 20/20 checks. That cut the count from 55 to 52, and the rest have no patch-level fix: `node-forge`, `braces` and `sprintf-js` have no patched version at all, and the others are fixed only by changing major version. These findings get resolved by upgrading the Expo SDK (`npx expo install expo@latest --fix`, then `npx expo-doctor`) once Expo ships updated tooling. Re-run `npm audit` after each SDK upgrade.

## Not verified yet

- No native iOS or Android build has been run. The EAS profiles and config plugins are checked by `expo-doctor` (20/20) but have not been built.
- Push delivery from end to end needs a real EAS project, credentials and the server endpoint.
- Biometric prompts have been exercised only through mocks in unit tests.

## Roadmap

- Voice intake with `expo-speech-recognition`, transcribed on-device and then edited as text before sending.
- Offline queue for decisions, with server-side idempotency keys.
- Deep links from push notifications to `approval/[id]`.
