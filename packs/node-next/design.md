# Next.js / React — design guidance

- State where each piece of data is fetched (Server Component, Route Handler,
  client cache) and how it is cached and revalidated, per user or shared.
- Draw the server/client boundary explicitly: which components are client
  components and why.
- Every mutation path (Server Action or Route Handler) lists its auth check,
  validation schema and error handling.
- Prefer progressive enhancement for forms; keep critical flows working
  without client JS where practical.
- Name the accessibility and performance budgets the feature must meet (e.g.
  WCAG 2.2 AA, LCP target) and how they are tested.
