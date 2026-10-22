# Java / Spring Boot — review rules

Apply these only to files of this pack (Java/Kotlin sources, build files,
Spring resources). They are defaults; a project's CLAUDE.md or ADR overrides
them. Formatting and import order are linter-owned: do not comment on them.

## REQUEST CHANGES (rule violations, weighted like bugs)
- Domain code importing adapter or framework types (`org.springframework..`,
  `jakarta.persistence..`, `jakarta.servlet..`, `com.fasterxml.jackson..`), or
  an adapter reaching another adapter instead of going through a port.
- A new or weakened ArchUnit suppression.
- `@Autowired` on a field, setter or constructor; hand-written DI constructors
  where `@RequiredArgsConstructor` + `private final` would do (when the project
  uses Lombok).
- A pure-data DTO / value object written as a mutable class where a `record`
  fits.
- Duplicated logic (more than ~5 substantially similar lines across files).
- `System.out`/`println`, empty catch blocks, raw types.
- JPA entities returned from controllers; request DTOs without validation.
- Editing an already-applied Flyway/Liquibase migration.

## Security checklist (OWASP ASVS-aligned)
- Authorization: every new endpoint has an explicit rule; method security
  (`@PreAuthorize`) checks ownership/tenant, not just "authenticated".
  Watch for IDOR: IDs from the path must be scoped to the caller.
- Spring Security config: no `permitAll()` widening, no `csrf().disable()` for
  cookie-session apps, CORS not `*` with credentials. Any change to a
  `SecurityFilterChain`, `JwtDecoder` or authentication provider is high risk;
  check audience, issuer and signature validation are still applied and that
  no new `@Primary` bean silently replaces a validating one.
- Input: Bean Validation on DTOs, size limits on collections and strings,
  no string-concatenated JPQL/SQL/native queries, no SpEL or template
  evaluation of user input.
- Deserialization: no Jackson default typing / polymorphic `@JsonTypeInfo`
  on untrusted input; no Java native deserialization.
- SSRF: outbound calls to URLs derived from user input need an allowlist.
- Secrets: none in `application*.yml`, tests or logs; read from environment.
- Logging: no tokens, passwords or personal data; no log injection from raw
  user strings.
- Actuator: only health/info exposed publicly.
- Dependencies: new libraries justified; no SNAPSHOT versions.

## Observations (non-blocking)
- N+1 queries, missing pagination, `FetchType.EAGER` on collections.
- Transactions wider than the use case; missing `readOnly = true`.
- Tests that only assert mocks were called rather than behaviour.

## Quality gates
- A failing `arch` (ArchUnit) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `**/*ArchitectureTest.java` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
