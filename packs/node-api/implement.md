# Node.js API — Engineer rules

Covers Express, NestJS and Fastify services in TypeScript. Project CLAUDE.md
and ADRs override these defaults.

## Structure
- Keep transport (routes/controllers) thin. Business logic lives in services
  that do not depend on `req`/`res` objects, so it can be unit tested.
- NestJS: one module per bounded area; inject dependencies via constructors;
  guards for auth, pipes for validation, filters for errors.
- Express/Fastify: one error-handling middleware / error handler; async
  handlers must propagate rejections (no unhandled promise rejections).

## Input and output
- Validate every request body, query and path parameter at runtime with the
  project's schema tool (zod, class-validator with `whitelist: true` and
  `forbidNonWhitelisted: true`, Fastify JSON schema, joi).
- Never pass a request body straight into an ORM create/update call; pick
  allowed fields explicitly (mass-assignment).
- Errors return a consistent shape (e.g. RFC 9457 problem details) and never
  include stack traces or internal messages in production.

## Data
- Parameterised queries or the ORM's query builder only. No string-built SQL.
  With MongoDB, reject operator injection (`$where`, `$gt` in user input).
- Schema changes only through the project's migration tool (Prisma, TypeORM,
  Knex, Drizzle). Migrations are reviewed as high risk.
- Explicit transactions for multi-write operations.

## Security
- Authentication middleware/guard applied by default; public routes are the
  explicit exception. Resource-level authorization in the service.
- `helmet` (or equivalent) headers, CORS restricted to known origins, request
  body size limits, rate limiting on auth endpoints.
- Secrets from environment/secret manager only. Structured logging (pino,
  winston) with redaction of tokens, passwords and personal data.

## Tests
- Unit tests for services; HTTP-level tests (supertest, Nest testing module,
  Fastify `inject`) for routes, including 401/403/400 cases.
- Run lint, typecheck and tests before pushing.

## Quality gates
- Before pushing, run the architecture check `npx --no-install depcruise --config .dependency-cruiser.cjs .` (the `arch` gate runs it
  whenever `.dependency-cruiser.cjs` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `npm run lint && npx --no-install prettier --check .` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
