# Java / Spring Boot — debt scan

Findings from this pack use the label `dept:backend`.

Look for, and report clusters rather than single instances:
- Hexagonal boundary violations: domain packages importing adapter or
  framework classes; ArchUnit suppressions or `@ArchIgnore`.
- `@Autowired` usage (field, setter or constructor). More than 5 classes:
  one umbrella ticket proposing constructor injection
  (`@RequiredArgsConstructor` + `private final` where Lombok is used).
- Hand-written DI constructors that Lombok could replace (same umbrella rule).
- Pure-data classes that could be `record`s (umbrella ticket if many).
- Duplicated logic blocks (more than 5 lines, over ~80% similar): one ticket
  per duplication cluster, citing every location.
- Sonar-style patterns: empty catch blocks, raw types, `System.out`, unused
  imports. One umbrella ticket if more than 10 instances.
- Outdated or vulnerable dependencies:
  `./gradlew dependencyUpdates` (if the versions plugin is present) or
  `mvn -B versions:display-dependency-updates`; check the OWASP
  dependency-check or OSV report if the build produces one. Known CVEs get
  `severity:high`.
- End-of-life Spring Boot or Java versions.
- Low coverage on churn hotspots: compare
  `git log --since="90 days ago" --name-only` against the JaCoCo report.
- Oversized classes (services over ~500 lines, controllers with many
  unrelated endpoints), slow or flaky tests (`Thread.sleep`, ordering
  dependence), TODO/FIXME accumulation, drift from ADRs in `docs/adr/`.
