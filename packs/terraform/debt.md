# Terraform / OpenTofu — debt scan

Findings from this pack use the label `dept:backend`.

Look for (static analysis only; never run plan/apply):
- Static scanner findings: `checkov -d . --quiet` or `trivy config .`;
  `tflint --recursive`. High/critical misconfigurations get `severity:high`.
- Unpinned or outdated providers and modules; deprecated resource arguments.
- Missing `.terraform.lock.hcl`; Terraform version constraint far behind.
- Public exposure, wildcard IAM, unencrypted storage, missing logging.
- Copy-pasted resource blocks across environments that should be a module.
- Hard-coded account IDs, regions, AMIs.
- TODO/FIXME clusters, ADR drift.
