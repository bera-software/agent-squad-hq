# Python / FastAPI — Engineer rules

Detection is by file only, so confirm the project actually uses FastAPI
(`fastapi` in dependencies) before applying FastAPI-specific rules; the
general Python rules apply to any Python service. Project CLAUDE.md and ADRs
override these defaults.

## Structure
- Routers stay thin; business logic in service functions/classes that do not
  depend on `Request`. Database access behind a repository or the session
  dependency.
- Use dependency injection (`Depends`) for the DB session, the current user
  and settings. Settings via `pydantic-settings`, read from the environment.

## Input and output
- Pydantic models for every request body and response (`response_model`), so
  internal fields are never serialised by accident. Separate create/update/read
  schemas; never accept fields like `id`, `owner_id`, `is_admin` from clients.
- Constrain sizes (`Field(max_length=...)`, list lengths, upload size).
- Errors via `HTTPException` or registered exception handlers; never return
  tracebacks.

## Data
- SQLAlchemy/SQLModel ORM or bound parameters only; no f-string SQL.
- Schema changes through Alembic migrations, reviewed as high risk. Keep a
  single head (`alembic heads`).
- Async code: do not call blocking I/O inside `async def` endpoints; use
  `def` endpoints or a thread pool for blocking libraries.

## Security
- Auth through a shared dependency applied at router level; resource-level
  ownership checks in the service.
- Passwords hashed with argon2 or bcrypt (passlib/pwdlib); JWTs verified with
  an explicit algorithm list, audience and expiry.
- CORS middleware restricted to known origins.
- Outbound HTTP with timeouts (httpx `timeout=`), and allowlists for
  user-supplied URLs.

## Tests and tooling
- pytest with `TestClient`/`httpx.AsyncClient`; cover 401/403/422 paths.
- Type hints on new code; ruff clean; mypy where the project runs it.
- Run `uv run ruff check .` and `uv run pytest` (or the project's equivalents) before pushing.

## Quality gates
- Before pushing, run the architecture check `uv run lint-imports --config .importlinter` (the `arch` gate runs it
  whenever `.importlinter` exists) and fix violations in the code. Never loosen
  the rules or add a suppression to get it green; that needs an approved design.
- Run the lint/format check `uv run ruff check . && uv run ruff format --check .` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
