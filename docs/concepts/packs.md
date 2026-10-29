# Stack packs

The core prompts in `prompts/` are language-neutral. Everything specific to a stack lives in a **pack** under `packs/<id>/`: build and test commands, coverage settings, review rules, debt checks, paths that force high risk, and the extra commands an agent may run.

## Available packs

`python -m squadhq.packs list` prints them. The repository ships 17:

| Pack | Stack | Pack | Stack |
|---|---|---|---|
| `java-spring` | Java / Spring Boot | `python-fastapi` | Python / FastAPI |
| `kotlin-android` | Android (Kotlin, Gradle) | `python-django` | Python / Django |
| `node-next` | Next.js / React (TypeScript) | `go` | Go |
| `react-vite` | React SPA (Vite) | `dotnet` | .NET / ASP.NET Core |
| `node-api` | Node.js API (Express, NestJS, Fastify) | `rust` | Rust |
| `react-native-expo` | React Native (Expo) | `php-laravel` | PHP / Laravel |
| `flutter` | Flutter (Dart) | `ruby-rails` | Ruby on Rails |
| `swift-ios` | iOS (Swift) | `terraform` | Terraform / OpenTofu |
| `docker-k8s` | Docker, Kubernetes, Helm | | |

The detection rules and coverage formats are listed in [`packs/README.md`](https://github.com/bera-software/agent-squad-hq/blob/main/packs/README.md).

## Selecting packs

`squadhq init --detect-packs` and the interactive interview propose packs from each repository's files. Detection only proposes; you confirm. Some detectors are broad (any `pyproject.toml` proposes `python-fastapi`), so remove packs that do not apply. You can also run `python -m squadhq.packs detect <path>` on a local checkout.

Select packs per project in `squadhq.yml`:

```yaml
projects:
  - name: Storefront
    repo: acme/storefront
    packs: [node-next, python-fastapi]
    pack_roots:                # optional: where each pack's code lives
      node-next: web
      python-fastapi: api
```

`squadhq validate` warns when a pack id is not found under `packs/`, and errors when `pack_roots` names a pack the project does not list.

## What a pack changes

| Pack part | Used by | Effect |
|---|---|---|
| `ci` (`setup`, `install`, `build`, `test`, `coverage`) | `ci-plan` and `stack-ci` jobs | One CI job per pack whose root contains a changed file, run in that root, plus a coverage comment and changed-lines coverage |
| `ci.lint` | `stack-ci` step `lint` | The lint/format gate, separate from `build` (see [Quality gates](quality-gates.md)) |
| `arch` | `stack-ci` step `arch`, onboarding | The architecture gate: the pack's tool runs when its config exists; the starter config in `snippets/` is proposed at onboarding |
| `tools` | `run-agent` (edit profiles) | Extra allowlist entries such as `Bash(npm run *)`. `Bash(*)` and other forbidden shapes are rejected |
| `risk_paths` | `squadhq.policy.risk`, `squadhq.merge_policy` | Globs that force the risk floor to `high` |
| `implement.md`, `review.md`, `debt.md`, `design.md` | Prompt assembly | Appended to the Engineer, Reviewer, debt-scanner and Architect prompts |
| `snippets/` | Onboarding, `quality.lint: default` | Starter architecture configs, default lint configs and other build-integration files |

When a project has no pack, edit profiles fall back to detecting a few common build tools (Gradle, Maven, npm/pnpm/yarn, pytest, Go) from the checkout.

## Querying packs

```bash
python -m squadhq.packs list
python -m squadhq.packs validate
python -m squadhq.packs files --repo acme/storefront --role review   # prompt files appended for a role
python -m squadhq.packs ci-plan --repo acme/storefront               # CI matrix as JSON
python -m squadhq.packs tools --repo acme/storefront                 # extra tool allowlist entries
python -m squadhq.packs risk-globs --repo acme/storefront            # extra high-risk globs
python -m squadhq.packs gates --repo acme/storefront                 # arch and lint commands agents run locally
```

`squadhq packs ...` is the same command.

## Writing a pack

The `pack.yml` schema is in [CONVENTIONS](../architecture/CONVENTIONS.md#packs-stack-specific-behaviour). The steps are in [CONTRIBUTING.md](https://github.com/bera-software/agent-squad-hq/blob/main/CONTRIBUTING.md#adding-a-stack-pack). Never put deploy, apply or publish commands in a pack: packs run on pull requests.
