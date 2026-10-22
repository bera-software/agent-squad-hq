# .NET / ASP.NET Core — review rules

Apply only to this pack's files. Formatting is owned by `dotnet format` and
analyzers: do not comment on it.

## REQUEST CHANGES
- Endpoint without `[Authorize]`/policy when the app has no secure fallback
  policy; `[AllowAnonymous]` added without justification; missing ownership
  check on user-owned resources (IDOR).
- Model binding straight to EF entities (over-posting).
- `FromSqlRaw`/`ExecuteSqlRaw` with concatenated input.
- `.Result`/`.Wait()` on tasks; `async void` outside event handlers.
- `BinaryFormatter`, `TypeNameHandling.All/Auto` on untrusted JSON.
- Secrets in `appsettings*.json` or source.
- Developer exception page or detailed errors enabled outside Development.

## Security checklist (OWASP ASVS-aligned)
- JWT bearer: `ValidateIssuer`, `ValidateAudience`, `ValidateLifetime`,
  `ValidateIssuerSigningKey` all true.
- Antiforgery on cookie-authenticated form posts; CORS not `AllowAnyOrigin`
  with credentials.
- Cookies `Secure`, `HttpOnly`, `SameSite`; HSTS and HTTPS redirection kept.
- Data Protection keys persisted securely; passwords via ASP.NET Identity
  hasher.
- SSRF and path traversal on user-supplied URLs/paths.
- Razor: no `Html.Raw` on user content.
- `dotnet list package --vulnerable` clean for new packages.

## Observations (non-blocking)
- N+1 queries (missing `Include`), missing pagination, tracking queries on
  read paths, missing `CancellationToken` propagation.

## Quality gates
- A failing `arch` (NetArchTest) or `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to `**/*ArchitectureTests.cs` or the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
