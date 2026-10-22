# Go — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- Follow the existing layout (commonly `cmd/` for binaries, `internal/` for
  non-exported packages). Small packages with clear responsibilities; accept
  interfaces, return concrete types; define interfaces where they are used.
- Always handle errors. Wrap with context (`fmt.Errorf("...: %w", err)`);
  compare with `errors.Is`/`errors.As`. No `_ =` on errors that matter, no
  `panic` for expected failures.
- `context.Context` is the first parameter for anything doing I/O; respect
  cancellation; never store a context in a struct.
- HTTP servers: set `ReadHeaderTimeout`, `ReadTimeout`, `WriteTimeout`,
  `IdleTimeout`; limit request bodies (`http.MaxBytesReader`). HTTP clients:
  never the zero-timeout default client for outbound calls.
- SQL with placeholders only (`db.QueryContext(ctx, q, args...)`); close
  rows; use transactions for multi-statement writes. Schema changes through
  the project's migration tool, reviewed as high risk.
- Concurrency: every goroutine has an owner and an exit path; protect shared
  state with mutexes or channels; run tests with `-race`.
- Validate input at the handler boundary; authorization checks in the
  service layer for user-owned resources.
- Use `crypto/rand` for tokens, `golang.org/x/crypto/bcrypt` or argon2 for
  passwords, `crypto/subtle` for constant-time comparisons.
- Structured logging (`log/slog`) without secrets or personal data.
- Table-driven tests; `httptest` for handlers. Run `gofmt`, `go vet`,
  `go test -race ./...` before pushing; keep `go.mod`/`go.sum` tidy.

## Quality gates
- Before pushing, run the architecture check `go run github.com/arch-go/arch-go@v1.7.0 --color no` (the `arch` gate runs it
  whenever `arch-go.yml` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0 run ./...` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
