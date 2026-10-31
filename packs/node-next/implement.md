# Next.js / React — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

## TypeScript
- `strict` mode. No new `any`, `@ts-ignore` or `eslint-disable` without a
  comment explaining why.
- Validate data at every boundary (form input, route handler body, server
  action arguments, third-party responses) with a schema library the project
  already uses (e.g. zod). Types alone do not validate runtime input.

## App Router
- Default to Server Components. Add `"use client"` only where interactivity
  needs it, and keep client components small.
- Server Actions and Route Handlers are public HTTP endpoints. Every one
  authenticates the caller and checks authorization for the specific resource
  inside the action/handler itself; middleware alone is not enough.
- Never import server-only modules (DB clients, secrets) into client
  components; mark them with `import "server-only"` where available.
- Environment variables prefixed `NEXT_PUBLIC_` are shipped to the browser.
  Never put a secret behind that prefix.
- Be explicit about caching and revalidation for data that is per-user;
  never cache authenticated responses in a shared cache.

## UI quality
- Server state through the project's data layer (fetch in Server Components,
  or the existing client library such as TanStack Query/SWR). No ad-hoc
  `fetch` scattered through client components.
- Every async view has loading, error and empty states.
- Accessibility basics: labelled inputs, visible focus, keyboard operable
  controls, alt text, semantic elements before ARIA.
- Use `next/image` and `next/link`; avoid heavy imports for trivial use.

## Tests
- Component tests (Jest or Vitest + Testing Library) for logic-bearing
  components and hooks.
- When the ticket adds a user-visible flow, extend the Playwright smoke suite
  (`snippets/playwright.config.ts`, `snippets/smoke.spec.ts` are the seed).
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
