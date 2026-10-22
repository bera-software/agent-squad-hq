# Terraform / OpenTofu — Engineer rules

Every infrastructure change is at least medium risk; the risk floor for this
pack's paths is high. Project CLAUDE.md and ADRs override these defaults.

## Hard limits
- Never run `terraform apply`, `destroy`, `import`, `state` subcommands or
  `plan` against real backends. You have no cloud credentials and must not
  look for any. You may run `fmt`, `validate`, `init -backend=false`,
  `tflint` and static scanners.
- Applying is human-only, through the project's reviewed pipeline.
- Never commit state files, `.terraform/`, plan files or real `*.tfvars`
  with secrets.

## Writing changes
- Pin provider versions (`required_providers` with `~>` constraints) and the
  Terraform version (`required_version`); commit `.terraform.lock.hcl`.
- Reuse existing modules; new modules get `variables.tf`, `outputs.tf`,
  descriptions and types for every variable, and `validation` blocks for
  constrained inputs.
- Mark secret variables and outputs `sensitive = true`; read secrets from a
  secret manager data source, never literals.
- Tag resources consistently (owner, environment, cost centre) per project
  convention.
- Prefer `for_each` over `count` for collections that change; avoid changes
  that force replacement of stateful resources (databases, volumes) — if
  unavoidable, say so prominently and add `lifecycle { prevent_destroy = true }`
  where the project uses it.
- In the PR body, list resources expected to be created, changed and
  destroyed, and the rollback approach.

## Quality gates
- Run the lint/format check `terraform fmt -check -recursive` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
