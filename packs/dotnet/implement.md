# .NET / ASP.NET Core — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- Nullable reference types enabled; treat warnings as errors in new projects.
  No new `#pragma warning disable` or `!` null-forgiving without a reason.
- Constructor injection through the built-in container; register lifetimes
  with care (no scoped services captured by singletons).
- Controllers / minimal API endpoints stay thin; logic in application
  services. Use `async`/`await` end to end with `CancellationToken`; never
  `.Result`/`.Wait()`.
- Request DTOs (records) validated with data annotations or FluentValidation.
  Never bind directly to EF entities (over-posting); map explicitly.
- Errors as `ProblemDetails` via the exception handler middleware; no stack
  traces outside Development.
- EF Core: LINQ or `FromSqlInterpolated`/parameters only, never
  `FromSqlRaw` with concatenated input. Schema changes as migrations
  (`dotnet ef migrations add`), reviewed as high risk. `AsNoTracking` for reads.
- Authorization: `[Authorize]` (or a fallback policy requiring authenticated
  users) by default; resource-based authorization for user-owned data.
- Secrets from user-secrets/environment/Key Vault, never `appsettings.json`.
- `IHttpClientFactory` for outbound calls with timeouts; allowlist
  user-supplied URLs.
- Structured logging via `ILogger<T>` with message templates; no secrets or
  personal data.
- Tests: xUnit/NUnit; `WebApplicationFactory` for endpoint tests including
  401/403/400. Run `dotnet build` and `dotnet test` before pushing.

## Quality gates
- Before pushing, run the architecture check `dotnet test --filter "Category=Architecture"` (the `arch` gate runs it
  whenever `**/*ArchitectureTests.cs` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `dotnet format --verify-no-changes` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
