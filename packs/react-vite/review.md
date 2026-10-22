# React SPA (Vite) — review rules

Apply only to this pack's files. Formatting and import order are
linter-owned: do not comment on them.

## REQUEST CHANGES
- A secret, private API key or internal URL in client code or a `VITE_*`
  variable.
- `dangerouslySetInnerHTML`, `innerHTML`, or markdown rendering with raw HTML
  on content not sanitised by a vetted sanitiser (e.g. DOMPurify).
- Authorization decided only in the client (route guard or hidden UI with no
  backend check referenced).
- New `any` / `@ts-ignore` / `eslint-disable` without justification.
- Missing loading or error handling on a new async view.

## Security checklist (OWASP ASVS-aligned)
- XSS: no unsanitised HTML; no user-controlled `href`/`src` with
  `javascript:` or `data:` schemes; CSP not weakened in `index.html`.
- Tokens: not in `localStorage` unless the project has explicitly accepted
  that; cleared on logout; never logged.
- Open redirects: post-login `redirect`/`returnTo` parameters allowlisted to
  same-origin paths.
- postMessage: `origin` checked on receive, explicit target origin on send.
- Third-party scripts: loaded with SRI or from the bundle, not arbitrary CDNs.
- PII: not sent to analytics or error tracking without the project's consent
  mechanism.
- Dependencies: new packages justified and maintained.

## Observations (non-blocking)
- Large bundles, missing route code-splitting, unnecessary re-renders,
  effects used for derived state, accessibility gaps beyond the basics.

## Quality gates
- A failing `arch` (dependency-cruiser) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `.dependency-cruiser.cjs` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
