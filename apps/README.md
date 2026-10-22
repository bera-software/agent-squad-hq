# Client apps

These are npm workspaces, with one lockfile at `apps/package-lock.json`. Both apps are clients of the server REST API v1 described in `docs/architecture/CONVENTIONS.md`.

| Workspace | What it is | Docs |
|---|---|---|
| `shared-api/` | The zod schemas, typed API client, board/flow helpers, a safe Markdown parser and fixtures with an in-memory mock `fetch`. Both apps consume it as TypeScript source, with no build step. | this file |
| `dashboard/` | The Next.js 15 web dashboard | [docs/apps/dashboard.md](../docs/apps/dashboard.md) |
| `mobile/` | The Expo (SDK 58) iOS and Android app | [docs/apps/mobile.md](../docs/apps/mobile.md) |

```bash
cd apps
npm ci
npm test          # every workspace
npm run typecheck
npm run lint
```

`npm ci` needs Node 20 or newer; the mobile toolchain needs Node 22.13+ or 24.3+.

To run either app without a server, start the dashboard with `MOCK_API=1 npm run dev`, or choose **Try demo mode** on the mobile sign-in screen. Both serve `shared-api` fixtures.

When the server contract changes, update `shared-api/src/schemas.ts` first. Its tests and both apps' type checks then show every place that needs to change.
