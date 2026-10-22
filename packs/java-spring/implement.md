# Java / Spring Boot — Engineer rules

These are this pack's defaults. If the project's CLAUDE.md or an ADR says
otherwise, the project wins.

## Architecture (hexagonal / ports and adapters)
- New use cases enter through a port (an interface in the domain or
  application layer). Adapters (web, persistence, external clients) implement
  or call ports. The domain never imports Spring, JPA, Jackson or servlet types.
- A new external integration is a new port plus a new adapter, never a direct
  HTTP client call from a service.
- If an ArchUnit test exists (e.g. `HexagonalArchitectureTest`), keep it green.
  Never add a suppression or widen a rule to make your change pass.

## Web layer
- Bean Validation (`@Valid`, `jakarta.validation` constraints) on every request
  DTO. Never expose JPA entities over HTTP; map to response DTOs.
- Errors as RFC 9457 / 7807 problem details from one central
  `@RestControllerAdvice`. Never leak stack traces or exception messages that
  contain internal data.
- Every new endpoint gets OpenAPI annotations and an explicit authorization
  rule (method security or the security filter chain), deny by default.

## Persistence and transactions
- Explicit transaction boundaries on application services
  (`@Transactional` at the use-case level, read-only where possible).
  No lazy-loading outside the transaction (no open-session-in-view reliance).
- Schema changes only through the project's migration tool (Flyway or
  Liquibase). Migrations are forward-only and reviewed as high risk; never
  edit an applied migration.
- Use parameterised queries / Spring Data methods. No string-built JPQL/SQL.

## Code standards (defaults)
- Dependency injection: constructor injection with `private final` fields.
  With Lombok, `@RequiredArgsConstructor`. Never `@Autowired` on fields or
  setters.
- Prefer `record` for DTOs, value objects, request/response types,
  configuration properties and event payloads. Keep classes for JPA entities,
  inheritance, or mutable builders (say why in a comment when non-obvious).
- Logging via SLF4J (`@Slf4j` with Lombok). No `System.out`/`System.err`.
  Structured context, never secrets or personal data.
- No empty catch blocks, no raw generic types, no unused imports or parameters.
  Prefer `Optional` returns over `null` for "may be absent".
- Before writing a new helper, search for an existing one; extract shared code
  rather than copy it.

## Tests
- Domain: plain JUnit 5 unit tests, no Spring context.
- Adapters: slice tests (`@WebMvcTest`, `@DataJpaTest`, `@JsonTest`).
- Testcontainers only when the behaviour genuinely depends on the real
  database or broker.
- Security-relevant endpoints: at least one test for the unauthorised and the
  forbidden case.
- Run `./gradlew build` (or `mvn -B verify`) before pushing. Do not lower the
  JaCoCo thresholds.

## Quality gates
- Before pushing, run the architecture check `./gradlew test --tests '*ArchitectureTest' (Gradle) or mvn -B test -Dtest='*ArchitectureTest' (Maven)` (the `arch` gate runs it
  whenever `**/*ArchitectureTest.java` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `./gradlew spotlessCheck checkstyleMain (Gradle) or mvn -B spotless:check checkstyle:check (Maven), for the plugins the build configures` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
