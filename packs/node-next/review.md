# Next.js / React — review rules

Apply only to this pack's files. Formatting, import order and most style
rules are linter-owned (ESLint/Prettier): do not comment on them.

## REQUEST CHANGES
- A Server Action or Route Handler without an authentication and
  resource-level authorization check inside it.
- A secret or server-only value read through a `NEXT_PUBLIC_` variable, or a
  server-only module imported into a `"use client"` file.
- `dangerouslySetInnerHTML` (or a markdown renderer with raw HTML enabled) on
  content that is not sanitised with a vetted sanitiser.
- Request bodies, search params or action arguments used without runtime
  validation.
- New `any`, `@ts-ignore` or `eslint-disable` without a justification comment.
- Per-user data served from a shared cache (`force-cache`, `revalidate` on an
  authenticated route) or leaked via static generation.
- Missing loading/error state on a new async view.

## Security checklist (OWASP ASVS-aligned)
- Authn/z: sessions/cookies `HttpOnly`, `Secure`, `SameSite`; middleware
  matchers not accidentally excluding protected routes; IDOR checks on IDs
  from params.
- CSRF: Server Actions get Next's origin check; custom Route Handlers using
  cookies must verify origin or a CSRF token for state-changing methods.
- XSS: no unsanitised HTML, no `javascript:` URLs from user data in `href`,
  Content-Security-Policy not weakened.
- Open redirects: `redirect()` / `router.push()` targets from user input are
  allowlisted.
- SSRF: server-side `fetch` to user-supplied URLs is allowlisted;
  `next.config` `images.remotePatterns` not widened to `**`.
- Secrets/PII: nothing sensitive in client bundles, logs, analytics events or
  error messages returned to the browser.
- Headers: security headers (CSP, HSTS, X-Content-Type-Options,
  frame-ancestors) not removed.
- Dependencies: new packages justified, maintained, and not pulled in for
  trivial use; no install scripts from unknown publishers.

## Observations (non-blocking)
- Client components that could be Server Components; large client bundles.
- Waterfall data fetching that could run in parallel.
- Missing `key` stability, unnecessary re-renders, effects used for derived
  state.
- Accessibility gaps beyond the basics.

## Quality gates
- A failing `arch` (dependency-cruiser) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `.dependency-cruiser.cjs` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
