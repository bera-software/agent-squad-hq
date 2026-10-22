# Web dashboard

`apps/dashboard` is a Next.js 15 (App Router, TypeScript strict) client of the server REST API v1 (see `docs/architecture/CONVENTIONS.md`). It gives approvers and operators a browser view of the squad:

| Page | What it shows | API calls |
|---|---|---|
| Overview | WIP per project, runs today (UTC), spend this month against the budget, open escalations | `/projects`, `/tickets?state=open`, `/runs`, `/costs`, `/approvals` |
| Pipeline | Tickets grouped by flow state (from `config/flows.yml`), filterable by project | `/tickets?project=` |
| Approvals | Merges, proposals, deploys and escalations waiting for a human. Approve or reject after a confirmation dialog; a rejection needs a comment. The UI updates at once and rolls back if the server refuses (for example 409 when someone already decided). | `/approvals`, `POST /approvals/{id}/decision` |
| Runs | Role, project, outcome, cost and duration, with a detail drawer. An Account column appears when the server attributes runs to a pool account (`data.account`, set by the server worker) | `/runs?project=&limit=` |
| Costs | Spend by project, role and model for a chosen month, as SVG bar charts with a data-table alternative | `/costs?month=YYYY-MM` |
| Accounts | Read-only status of the coding-agent account pool: each account's id, auth (API key or subscription), credential **name**, and whether it is available or limited (local time and "in N min"), in strategy order, plus the latest run per account when runs carry `data.account`. Without a pool it explains how to configure `providers.anthropic.pool` ([account pool](../configuration/squadhq-yml.md#account-pool)). No controls | `/capacity`, `/runs` |
| Audit log | Paged events (25 per page, `since` cursor), with the server's hash-chain verification and a per-row link check | `/audit?since=`, `/audit/verify` |
| Daily brief | The latest brief, rendered as sanitised Markdown | `/briefs/latest` |
| Settings | Sign in with an API token, or sign out | `POST/DELETE /api/session` (dashboard-local) |

The schemas, the typed client (with zod runtime validation) and the fixtures live in `apps/shared-api`, which the mobile app also uses. `apps/dashboard/lib/api.ts` re-exports them and adds the browser client.

## Setup

Requires Node 20 or newer (CI and the Docker image use Node 22).

```bash
cd apps
npm ci                                   # installs every workspace from apps/package-lock.json
cd dashboard
cp .env.example .env.local               # then edit
npm run dev                              # http://localhost:3000
```

Explore without a server:

```bash
npm run dev:mock                         # MOCK_API=1: in-memory fixtures, decisions persist until restart
```

Get a token from the server, and use the `approver` role if the person must decide approvals:

```bash
squadhq server create-token --role approver --name alice
```

Then paste it on **Settings**.

### Environment variables

All of these are read on the server only. Nothing is exposed to the browser, and none uses the `NEXT_PUBLIC_` prefix.

| Variable | Default | Meaning |
|---|---|---|
| `SQUADHQ_API_URL` | none | Server origin, for example `https://hq.example.com`. A trailing `/api/v1` is accepted and stripped. |
| `SQUADHQ_API_URL_ALLOWLIST` | empty | Other server origins a user may pick on Settings, comma-separated. Any URL not in this list or `SQUADHQ_API_URL` is refused (SSRF guard). |
| `SQUADHQ_SESSION_SECRET` | none | When set (16+ characters, 32 random bytes recommended), the token cookie is sealed with AES-256-GCM. **Set it in every deployment.** Rotating it signs everyone out. |
| `SQUADHQ_API_TOKEN` | none | Fallback token for visitors without a session. **Ignored unless `SQUADHQ_DASHBOARD_ANONYMOUS=true`.** |
| `SQUADHQ_DASHBOARD_ANONYMOUS` | `false` | Explicit opt-in to anonymous access: with it, anyone who can reach the dashboard acts with `SQUADHQ_API_TOKEN`'s role. Use it only behind an authenticating reverse proxy (SSO). The server logs a startup warning when it is on. Without it, visitors with no session are redirected to the sign-in page (`/settings`). |
| `MOCK_API` | `0` | `1` serves fixtures from `@squadhq/shared-api/fixtures` in place of the server. |
| `PORT`, `HOSTNAME` | `3000`, `0.0.0.0` | Standard Next.js standalone server settings. |

## Scripts

| Command | What it does |
|---|---|
| `npm run lint` | ESLint (next/core-web-vitals and next/typescript, `react/no-danger` set to error), zero warnings allowed |
| `npm run typecheck` | `tsc --noEmit` in strict mode with `noUncheckedIndexedAccess` |
| `npm test` | Vitest and Testing Library: components, the API client, session sealing, proxy policy, CSP |
| `npm run build` | Production build (`output: "standalone"`) |
| `npm start` | Runs the standalone server (`.next/standalone/dashboard/server.js`) after copying `.next/static` and `public/` next to it, as the Dockerfile does. `next start` is not used because it does not support `output: "standalone"`. Honours `PORT` and `HOSTNAME`. |
| `npm run start:mock` | The same, with `MOCK_API=1` |
| `npm run test:e2e` | Playwright smoke tests against the standalone server in mock mode. Run `npm run build` and `npx playwright install chromium` first. |

## Deploy

### Docker (standalone output)

The build context is the `apps/` workspace root, so the image includes `shared-api`:

```bash
docker build -f apps/dashboard/Dockerfile -t squadhq-dashboard apps/
docker run --rm -p 3000:3000 \
  -e SQUADHQ_API_URL=https://hq.example.com \
  -e SQUADHQ_SESSION_SECRET="$(openssl rand -base64 32)" \
  squadhq-dashboard
```

About the image:

- It runs as a non-root user.
- Its health check is `GET /api/health`.
- It only needs to make outbound connections to `SQUADHQ_API_URL`.
- Put it behind TLS. The cookies get the `Secure` flag when the request arrives over HTTPS (directly or with `X-Forwarded-Proto: https`).

### Vercel

1. Import the repository. Set **Root Directory** to `apps/dashboard`. Vercel detects the npm workspace from `apps/package-lock.json`.
2. Set `SQUADHQ_API_URL` and `SQUADHQ_SESSION_SECRET` (and `SQUADHQ_API_URL_ALLOWLIST` if needed) for Production and Preview. Point Preview at a staging server, or set `MOCK_API=1` there.
3. The server must be reachable from Vercel's network. The dashboard calls it server-side, so the API needs no CORS configuration.
4. Enable `SQUADHQ_DASHBOARD_ANONYMOUS` only if Vercel Deployment Protection or SSO guards the deployment.

### Dependency audit

`npm audit` in `apps/` also reports dashboard findings:

- `next` 15.5.x (moderate), plus the `postcss` copy that Next bundles;
- `braces`, `micromatch` and `fast-glob`, which come in through `eslint-config-next`.

No patched 15.x release exists for any of them. The eslint chain is lint-time only. The `next` and bundled-`postcss` findings go away with the move to Next 16, which is a major upgrade (the async request APIs and middleware become `proxy.ts`). Track it as a separate change, not a forced fix.

## Security notes

- **The browser never sees the bearer token.**
  - Settings posts the token once to `POST /api/session`.
  - That route checks it with `GET /api/v1/whoami`, then stores it in an `httpOnly`, `SameSite=Strict` cookie (`Secure` over HTTPS). The cookie is sealed when `SQUADHQ_SESSION_SECRET` is set.
  - Pages fetch data in Server Components. Browser code calls only `/api/proxy/*`, which adds the token server-side.
- **The proxy is an allowlist.** Through `/api/proxy/*` the browser can make only these calls:
  - GET `whoami`, `projects`, `tickets`, `runs`, `approvals`, `approvals/{id}`, `costs`, `capacity`, `audit`, `audit/verify`, `briefs/latest`;
  - POST `approvals/{id}/decision`.

  Admin endpoints (`tokens`, `jobs`, `runs` POST) are refused even with an admin token. Request bodies are capped at 16 KiB. Upstream redirects are not followed, and only JSON responses are passed back.
- **CSRF protection.** State-changing routes require a same-origin `Origin` / `Sec-Fetch-Site` header, in addition to `SameSite=Strict`.
- **SSRF protection.** The server URL can only be one of the operator-configured origins.
- **Headers.**
  - Pages get a per-request nonce CSP from `middleware.ts`:

    ```
    script-src 'self' 'nonce-…' 'strict-dynamic'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
    ```

  - `next.config.ts` sets these headers on every response: `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, a restrictive `Permissions-Policy`, COOP/CORP `same-origin` and HSTS.
  - API routes get `default-src 'none'` and `Cache-Control: no-store`.
- **The brief is untrusted text** (it is written by an LLM from tracker content), and it is never injected as HTML:
  - `react-markdown` runs with `skipHtml`, `rehype-sanitize` (GitHub schema, no images) and a URL filter that allows only http(s), mailto and relative links.
  - Links get `rel="noopener noreferrer nofollow"`.
  - `dangerouslySetInnerHTML` is banned by lint.
- **No anonymous access by default.** A visitor without a session cookie is redirected to the sign-in page, and the proxy answers 401. `SQUADHQ_API_TOKEN` is used for session-less visitors only with the explicit `SQUADHQ_DASHBOARD_ANONYMOUS=true` opt-in. `instrumentation.ts` logs startup warnings when the opt-in is on, when the token is set but ignored, and when `SQUADHQ_SESSION_SECRET` is missing.
- **Roles.**
  - A viewer token can browse but gets no decision buttons. The server enforces roles in any case.
  - Hash-chain verification needs an admin token; other roles see "verification unavailable".

## Accessibility

The target is WCAG 2.2 AA:

- **Structure:** semantic landmarks with a skip link, one `h1` per page, and brief headings shifted below it.
- **Navigation and focus:** `aria-current` marks the active nav link. Focus outlines are visible (3px). Dialogs use native `<dialog>`, which traps focus and closes on Escape, and focus returns to the trigger when the run drawer closes.
- **Labels and announcements:** form fields have labels and the reject comment shows a validation error. Live regions announce decision results. Action buttons have accessible names that include the item.
- **Charts and colour:** charts have `role="img"` with a title and description, values are labelled directly, and each chart has a data-table alternative. Badges and status never rely on colour alone.
- **Display:** light and dark themes each have their own colour tokens (`prefers-color-scheme`). Layouts are responsive down to 320px, and `forced-colors` and `prefers-reduced-motion` are honoured.

## Endpoints and fields requested from the server

- **`GET /api/v1/costs`.** Add `budget_usd: number | null` (from `budgets.monthly_usd`) and `budget_by_project: {[project]: number}` (from `budgets.per_project_usd`, with `default` expanded per registered project). The UI already reads both and shows "no budget reported" until they exist.
- **`GET /api/v1/capacity`** (available). Per-account breaker state of the coding-agent account pool: `configured`, `strategy`, `available`, `all_tripped_until` and `accounts[]` (`id`, `auth`, secret name, `state`, `tripped_until`). No secret values. The Accounts page reads it.
- **The mobile endpoints** `POST /api/v1/requests` and `POST /api/v1/devices`. See `docs/apps/mobile.md`.
