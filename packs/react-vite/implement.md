# React SPA (Vite) — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- TypeScript `strict`. No new `any`, `@ts-ignore` or `eslint-disable` without
  a reason in a comment.
- A single-page app has no server-side secrets: everything in the bundle,
  including every `VITE_*` variable, is public. Never add an API key or secret
  to client code or `.env` files read by Vite.
- Authorization is enforced by the backend. Route guards in the SPA are UX
  only; never treat a hidden button as access control.
- Server state through the project's data layer (e.g. TanStack Query, SWR,
  RTK Query) with one typed API client. Validate untrusted responses where the
  shape matters (e.g. zod).
- Store tokens the way the project already does; prefer HttpOnly cookies set
  by the backend over `localStorage`.
- Every async view has loading, error and empty states.
- Accessibility basics: labelled inputs, visible focus, keyboard operable
  controls, semantic HTML before ARIA.
- Code-split routes with `React.lazy` for large pages; avoid importing whole
  libraries for one helper.
- Tests: Vitest + Testing Library for components and hooks with logic;
  Playwright (see `packs/node-next/snippets/` for a seed config and smoke spec,
  change `baseURL` to the Vite port) when adding a user-visible flow.
- Run lint, typecheck, tests and `npm run build` before pushing.

## Quality gates
- Before pushing, run the architecture check `npx --no-install depcruise --config .dependency-cruiser.cjs .` (the `arch` gate runs it
  whenever `.dependency-cruiser.cjs` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `npm run lint && npx --no-install prettier --check .` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
- Dependency versions: change them only with
  `python -P -m squadhq.pin_install [--dir <package dir>] [--dev] <name>@<exact version> ...`
  (install scripts off, exact version saved, lockfile updated). Plain `npm install`,
  ranges and tags are refused. Look versions up with `npm view <name> versions --json` or
  `npm view <name>@<range> version`. Name the old and new version in the PR body, and
  check the changelog of a major-version jump before using it.
