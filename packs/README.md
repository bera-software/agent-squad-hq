# Stack packs

Core prompts in `prompts/` are language-neutral. Everything stack-specific
(build and test commands, coverage format, review rules, debt checks, risky
paths, extra tool permissions) lives in a pack.

| Pack | Name | Detected by | Coverage |
|---|---|---|---|
| `java-spring` | Java / Spring Boot | `pom.xml`, `build.gradle(.kts)` | jacoco |
| `kotlin-android` | Android (Kotlin, Gradle) | `AndroidManifest.xml`, `proguard-rules.pro` | jacoco (Kover) |
| `node-next` | Next.js / React | `next.config.{js,mjs,ts}` | lcov |
| `react-vite` | React SPA (Vite) | `vite.config.*` | lcov |
| `node-api` | Node.js API (Express / NestJS / Fastify) | `nest-cli.json`, `server.{ts,js}`, `app.{ts,js}` | lcov |
| `python-fastapi` | Python / FastAPI | `pyproject.toml`, `requirements.txt`, `uv.lock`, `poetry.lock` | cobertura |
| `python-django` | Python / Django | `manage.py` | cobertura |
| `go` | Go | `go.mod` | cobertura (converted) |
| `dotnet` | .NET / ASP.NET Core | `*.sln`, `*.slnx`, `*.csproj`, `global.json` | cobertura |
| `rust` | Rust | `Cargo.toml` | lcov |
| `php-laravel` | PHP / Laravel | `artisan` | cobertura |
| `ruby-rails` | Ruby on Rails | `config.ru` | cobertura |
| `react-native-expo` | React Native (Expo) | `app.json`, `app.config.*`, `eas.json` | lcov |
| `flutter` | Flutter (Dart) | `pubspec.yaml` | lcov |
| `swift-ios` | iOS (Swift) | `Package.swift`, `Project.swift`, `project.yml`, `Podfile` | none (macOS runner) |
| `terraform` | Terraform / OpenTofu | `*.tf`, `.terraform.lock.hcl` | none |
| `docker-k8s` | Docker / Kubernetes / Helm | `Dockerfile`, compose files, `Chart.yaml`, `kustomization.yaml` | none |

Architecture and lint gates per pack (tool versions, starter configs in
`snippets/`, how `projects[].quality` switches them) are listed in
[`docs/concepts/quality-gates.md`](../docs/concepts/quality-gates.md).

Detection only proposes packs during `init`/`onboard`; you confirm the
selection. Some detectors are broad (for example any `pyproject.toml`
proposes `python-fastapi`), so remove packs that do not apply.

## Selecting packs for a project

In `config/projects.json` (rendered from `squadhq.yml`):

```json
{
  "name": "Example",
  "repo": "your-org/example-app",
  "packs": ["java-spring", "node-next"],
  "pack_roots": {"java-spring": "backend", "node-next": "frontend"}
}
```

`pack_roots` overrides a pack's default `root` (the working directory for
its CI commands and the prefix for its change-detection globs).

## Pack layout

```
packs/<pack-id>/
  pack.yml       # manifest
  implement.md   # extra Engineer rules
  review.md      # extra review rules and security checklist
  debt.md        # what the debt scanner looks for
  design.md      # optional
  snippets/      # optional: starter arch configs, default lint configs, build-integration files
```

The `pack.yml` schema is defined in
[`docs/architecture/CONVENTIONS.md`](../docs/architecture/CONVENTIONS.md#packs-stack-specific-behaviour)
and validated by `python -m squadhq.packs validate`.

## Adding a pack

1. Copy the pack closest to your stack and change `id` (must equal the
   directory name), `name`, `detect.files` (file-name patterns at the
   checkout root or one level below) and `paths`.
2. Use realistic `ci` commands that run on a clean runner without cloud
   credentials. Never put deploy, apply or publish commands in a pack.
3. Keep `tools` narrow (`Bash(<command> *)` entries). `Bash(*)` is never
   accepted. They must cover the `arch.local` and `ci.lint.local` commands
   agents run before pushing (`tests/test_packs.py` checks this).
4. Optional gates: `ci.lint` (keep linting out of `build`) and `arch` with
   a pinned, widely used tool and a starter config in `snippets/` marked
   `TODO(owner)` where it needs the project's names.
5. List paths that must force `risk:high` under `risk_paths`.
6. Write `review.md` with a clear REQUEST CHANGES list and a security
   checklist, and leave formatting to the stack's linters.
7. Run `python -m squadhq.packs validate`.
