# Terraform / OpenTofu — review rules

Apply only to this pack's files. Formatting is owned by `terraform fmt` and
tflint. Every PR touching these files is at least `risk:high` by path floor;
never argue it lower.

## REQUEST CHANGES
- Storage made public (S3 ACL `public-read`, public access block disabled,
  GCS `allUsers`, Azure public blob access) without an explicit, documented
  need.
- Security groups / firewall rules opening `0.0.0.0/0` or `::/0` to anything
  other than intended public ports (80/443 on load balancers).
- IAM policies with `"Action": "*"` or `"Resource": "*"` on write actions,
  wildcard principals, or new admin roles.
- Encryption at rest or in transit disabled (unencrypted volumes, databases,
  buckets; TLS listeners removed).
- Secrets as literals, in `*.tfvars` committed to the repo, or outputs not
  marked `sensitive`.
- Unpinned providers/modules (no version constraint, module source on a
  branch instead of a tag/commit).
- A change that replaces or destroys a stateful resource without
  `prevent_destroy`, a migration plan and explicit call-out.
- Logging/audit (CloudTrail, flow logs, audit logs) disabled.

## Security checklist (CIS / ASVS-aligned)
- Databases not publicly accessible; deletion protection on.
- Key management: customer-managed keys where the project requires them;
  key rotation enabled.
- Network: private subnets for workloads; egress restricted where possible.
- Backups and retention configured for stateful services.
- Scanners: `checkov -d .` or `trivy config .` findings for changed files
  addressed or justified.

## Observations (non-blocking)
- Missing tags, duplicated resource blocks that should be a module, hard-coded
  regions/account IDs, missing variable descriptions.

## Quality gates
- A failing `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
