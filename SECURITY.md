# Security policy

Agent Squad HQ runs language-model agents with write access to your repositories. We treat security reports, including prompt-injection reports, as our highest priority.

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub's private vulnerability reporting:

1. Go to <https://github.com/bera-software/agent-squad-hq/security/advisories/new> (the repository's **Security** tab, then **Report a vulnerability**).
2. Describe the issue, the affected version or commit, and how to reproduce it. A minimal `squadhq.yml`, the event payload or issue text that triggers it, and the workflow run log help most.
3. Tell us how you would like to be credited, or whether you prefer to stay anonymous.

GitHub Security Advisories are the only channel we use for security reports. We do not accept reports by email or social media.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement of your report | within 3 working days |
| Initial assessment (accepted, needs info, or out of scope) | within 10 working days |
| Fix or mitigation for critical and high severity issues | as fast as possible, normally within 30 days |
| Coordinated public disclosure | within 90 days of the report, or earlier once a fix is released |

We follow a **90-day coordinated disclosure** policy. If a fix needs more time we will agree a new date with you. If an issue is being exploited in the wild we may disclose sooner, with a mitigation. We request CVE identifiers through GitHub Security Advisories for confirmed vulnerabilities.

We do not run a paid bug bounty at this time.

## Supported versions

The project is in alpha. Security fixes go to:

| Version | Supported |
|---|---|
| `main` | yes |
| latest `0.1.x` release (once published) | yes |
| anything older | no; upgrade to the latest release |

Each project stub pins the hub by commit SHA (see [docs/getting-started/upgrading.md](docs/getting-started/upgrading.md)), so you must pick up a fixed release yourself, normally by merging the stub-sync PR.

## Scope

In scope:

- **Prompt injection that changes behaviour across a trust boundary.** For example: text in an issue, PR, comment, diff, commit message, web page or tracker ticket from an untrusted author that makes an agent run a tool outside its profile, apply a label its role does not own, push outside its allowed branches, exfiltrate a secret or token, approve or merge a change, or trigger a job the author's association should not allow. Please include the exact text and the run.
- Bypasses of the tool allowlist (`squadhq.executors.tool_profiles`), the branch guard, the label guard, the risk floor (`squadhq.policy.risk`), the merge policy (`squadhq.merge_policy`) or the trigger gates in `router.yml`.
- Token scope problems: a job receiving more repositories or permissions than it needs, a token persisted where an agent can read it, or secrets reaching fork PR runs.
- Workflow injection (`${{ }}` expressions reaching a shell), unpinned actions or downloads, and supply-chain issues in our release process.
- Server mode: authentication or role bypass in the REST API, webhook signature bypass, audit-chain tampering that `GET /api/v1/audit/verify` does not detect, sandbox escapes in worker mode, and SSRF.
- The dashboard and mobile app (preview): session handling, proxy policy and approval actions.

Out of scope:

- Model output quality (an agent writing poor code or a wrong review) without a security impact.
- Problems that need a malicious approver, organisation owner or hub maintainer. They are trusted by design (see [docs/security/security-model.md](docs/security/security-model.md)).
- Running with settings the documentation marks unsafe, such as `policies.merge.mode: auto` without a protected default branch, or `NONE` in `policies.triggers.allowed_associations`, unless the docs understate the risk.
- Known residual risks listed in [docs/security/threat-model.md](docs/security/threat-model.md), unless you show a practical exploit that goes beyond what is listed.
- Vulnerabilities in third-party services or tools (GitHub, model providers, scanners) that are not caused by how we use them. Please report those upstream.
- Denial of service through high event volume, and findings from automated scanners without a demonstrated impact.

## Safe harbour

We will not take legal action against good-faith research that follows this policy: test only against organisations and repositories you own or have permission to test, do not access or keep other people's data, do not degrade other users' service, and give us reasonable time to fix the issue before you disclose it.

## Hardening guidance

For deployment hardening see [docs/deploy/security-hardening.md](docs/deploy/security-hardening.md) and [docs/security/security-model.md](docs/security/security-model.md).
