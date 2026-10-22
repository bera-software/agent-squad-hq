# Java / Spring Boot — design guidance

- Default to a modular monolith with hexagonal modules: `domain` (model +
  ports, framework-free), `application` (use cases, transactions),
  `adapter.web`, `adapter.persistence`, `adapter.external`.
- Enforce module boundaries in code with ArchUnit (see
  `snippets/HexagonalArchitectureTest.java`) or Spring Modulith, not in prose.
- Recommend extracting a service only with a concrete trigger: independent
  scaling need, a divergent change rate visible in git history, or a genuinely
  separate domain with its own data. Otherwise say "stay monolith".
- Every new integration gets a port, so it can be faked in tests and replaced.
- Schema changes: describe the migration (expand, migrate, contract), how it is
  rolled back, and whether a backfill is needed. Mark them high risk.
- API changes: state whether they are breaking and how clients migrate;
  version or add fields rather than changing meaning.
