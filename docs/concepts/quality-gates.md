# Quality gates

Besides the build, the tests and the SSDLC scanners, stack CI enforces three
quality gates on every pull request: **architecture**, **lint/format** and
**changed-lines coverage**. Each is a sub-check of the one required status,
`hub / ci-gate`, and each one is set per project in `squadhq.yml`:

```yaml
projects:
  - name: Storefront
    repo: acme/storefront
    packs: [node-next, python-fastapi]
    quality:
      arch: auto                 # auto | required | off
      lint: auto                 # auto | default | off
      changed_coverage: report   # report | enforce | off
```

`squadhq render` writes the block into `config/projects.json`, and
`python -m squadhq.packs ci-plan` reads it from there.

| Gate | ci-gate sub-check | Runs when | Fails when |
|---|---|---|---|
| Architecture | `arch` (a step of each `stack-ci` leg) | the pack has an `arch` block and its config exists in the repo (`auto`), or always (`required`) | the tool reports a violation, or `required` and the config is missing |
| Lint / format | `lint` (a step of each `stack-ci` leg) | the pack has `ci.lint` and a linter config exists, the linter needs none, or `default` supplies the pack's default config | the linter or formatter check fails |
| Changed-lines coverage | `changed-coverage` (its own job) | the stack produces a coverage report and the mode is not `off` | `enforce`, the pack's `min_changed` is above 0, a report exists, and covered / measurable changed lines is below `min_changed` |

When a gate does not apply, the stack-ci summary and annotations say why
(`arch skipped: no .dependency-cruiser.cjs under web (quality.arch: auto)`),
and ci-gate lists it as skipped rather than failed.

## Architecture (`arch`)

| Pack | Tool | Config the gate looks for | Command |
|---|---|---|---|
| `node-next`, `node-api`, `react-vite`, `react-native-expo` | [dependency-cruiser](https://github.com/sverweij/dependency-cruiser) 18.5.0 (project devDependency) | `.dependency-cruiser.cjs` | `npx --no-install depcruise --config .dependency-cruiser.cjs .` |
| `python-fastapi`, `python-django` | [import-linter](https://import-linter.readthedocs.io/) 2.15 (installed by the gate) | `.importlinter` | `lint-imports --config .importlinter` |
| `go` | [arch-go](https://github.com/arch-go/arch-go) v1.7.0 (`go run`, checksum-verified) | `arch-go.yml` | `go run github.com/arch-go/arch-go@v1.7.0` |
| `java-spring` | [ArchUnit](https://www.archunit.org/) JUnit tests | `**/*ArchitectureTest.java` | the build tool runs only the `*ArchitectureTest` tests |
| `dotnet` | [NetArchTest.Rules](https://github.com/BenMorris/NetArchTest) 1.3.2 xUnit tests | `**/*ArchitectureTests.cs` | `dotnet test --filter "Category=Architecture"` |
| `php-laravel` | [deptrac](https://github.com/deptrac/deptrac) 4.7.2 (project dev dependency) | `deptrac.yaml` | `vendor/bin/deptrac analyse --config-file=deptrac.yaml` |

Each pack ships a starter config in `packs/<id>/snippets/` with layer rules
for its stack (no circular dependencies, UI never imports server or database
code, production code never imports tests; API → services → domain with a
framework-free domain; and so on). Every starter is marked `TODO(owner)`
where it needs the project's real paths or package names.

No architecture tool is configured for `rust`, `ruby-rails`, `flutter`,
`kotlin-android`, `swift-ios`, `terraform` or `docker-k8s`: either no widely
used tool fits a generic default (packwerk, Konsist and cargo-modules all
need per-project setup) or layering does not apply. For those stacks the
reviewer checks the layering rules written in `CLAUDE.md` and the ADRs.

`quality.arch`:

- `auto` (default): the gate runs as soon as the config file exists, so a
  project opts in by committing it. Onboarding proposes it.
- `required`: a missing config fails ci-gate, with a pointer to the starter.
- `off`: never runs.

## Lint / format (`lint`)

Lint is its own step, separate from `build`; packs that used to lint inside
`build` moved those commands to `ci.lint`, so nothing runs twice.

| Pack | Command | Runs without a config? | Default config (`lint: default`) |
|---|---|---|---|
| Node packs | the `lint` script (or ESLint), then `prettier --check` when Prettier is configured | no | — |
| `python-fastapi` | `ruff check .`, plus `ruff format --check .` when a `[format]` section exists | yes (Ruff defaults, as before) | `ruff.toml` |
| `python-django` | same | no | `ruff.toml` |
| `go` | golangci-lint (v2.14.0 for v2 configs, v1.64.8 otherwise) | no | `.golangci.yml` |
| `dotnet` | `dotnet format --verify-no-changes --no-restore` | no | `.editorconfig` |
| `java-spring` | Spotless / Checkstyle checks, for the plugins the build configures | no | — |
| `kotlin-android` | `./gradlew lintDebug` | yes | — |
| `php-laravel` | Pint `--test`, PHP-CS-Fixer `--dry-run` when installed | no | — |
| `ruby-rails` | RuboCop when it is in the bundle | no | — |
| `rust` | `cargo fmt --check` and `cargo clippy -D warnings` | yes | — |
| `flutter` | `dart format --set-exit-if-changed` and `flutter analyze` | yes | — |
| `terraform` | `terraform fmt -check -recursive` | yes | — |

`quality.lint`:

- `auto` (default): runs when a linter config exists, or the linter needs
  none.
- `default`: as `auto`, and when the repo has no config but the pack ships
  one, CI uses the pack's default (copied into the job's checkout, never
  committed).
- `off`: never runs.

## Changed-lines coverage (`changed-coverage`)

The `changed-coverage` job computes, per stack, the share of the PR's added
or modified lines that the tests executed. It reads the coverage report each
stack-ci leg uploaded (lcov, Cobertura or JaCoCo) and diffs the merge commit
against its first parent, so the line numbers match the report. Lines with
no coverage data (blank lines, comments, declarations) are not counted. The
per-file breakdown, with uncovered line numbers, goes to the job summary.

`quality.changed_coverage`:

- `report` (default): the summary only; nothing blocks.
- `enforce`: a required ci-gate check, failing below the pack's
  `min_changed`. It degrades to report-only when `min_changed` is 0 or no
  report was produced (the overall coverage gate in stack-ci already fails
  on a missing report).
- `off`: not computed.

Known limit: a changed file the test run never loaded may be missing from
the report (for example Jest without `collectCoverageFrom`), and then its
lines are not counted.

## Security model

- `arch` and `lint` run inside the tokenless `stack-ci` job, after the
  install step, like the build and the tests: read-only `GITHUB_TOKEN`, no
  secrets, no persisted credentials.
- `changed-coverage` never executes PR code. It holds only `contents: read`,
  runs the hub's own parser (`squadhq.quality.coverage`, standard library
  only) from the `squadhq-quality-kit` artifact that `ci-plan` uploads before
  any PR code runs, and treats the PR-built reports as data: regular files
  only (no symlinks), 64 MB per report, no XML entity declarations, capped
  record counts, `git diff` without external diff drivers or textconv. The
  isolation lint (`.github/lint/isolation.py`) checks it like `stack-ci`.
- ci-gate reads the `arch` and `lint` step conclusions from the Actions API
  with its own read-only token, the same way it reads job results.

## What agents do

The implement and review prompts list the project's gate commands under
`QUALITY GATES` (`python -m squadhq.packs gates --repo <owner/name>`). The
Engineer runs them before pushing, and fixes the code, never the rule; the
packs grant the needed commands (`Bash(npx --no-install depcruise *)`,
`Bash(lint-imports*)`, `Bash(ruff *)` ...) through their `tools` lists,
under the same deny rules as every other command. The Reviewer reads the
checks (`gh pr checks`), cites failing `arch`, `lint` and `changed-coverage`
output in its findings, and treats a PR that loosens an architecture or
linter config to get green as REQUEST CHANGES unless a design approved it.

## Onboarding

`squadhq onboard <owner/repo> --apply` (and the Onboard Project workflow)
proposes, never over an existing file: a starter `CLAUDE.md` pre-filled from
the detected packs, ADR 0001, each pack's starter architecture config, and,
when the repo has no linter config, the pack's default lint config. The PR
body is a checklist of what the owner must finish before merging. See
[Quickstart, step 7](../getting-started/quickstart-actions-mode.md#7-onboard-the-project).
