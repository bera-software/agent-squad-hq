# Getting help

Agent Squad HQ is community-supported. There is no paid support or SLA for the open-source edition.

## Before you ask

1. Run `squadhq doctor`. It checks the configuration, the rendered files, secrets, variables, the GitHub App installation and the project rulesets, and prints the fix for each failure.
2. Run `squadhq validate --explain` to see which model and UTC schedule each job resolves to.
3. Read [Troubleshooting](docs/operations/troubleshooting.md) and the [FAQ](docs/operations/faq.md).
4. Look at the failed run in the project's **Actions** tab, and at the `escalation` issues the hub opened.

## Where to ask

| You have | Go to |
|---|---|
| A question about setup or usage | [GitHub Discussions](https://github.com/berasoftware/agent-squad-hq/discussions), Q&A category |
| A reproducible bug | [Open a bug report](https://github.com/berasoftware/agent-squad-hq/issues/new?template=bug_report.yml) |
| An idea or feature request | [Open a feature request](https://github.com/berasoftware/agent-squad-hq/issues/new?template=feature_request.yml) |
| A security problem, including prompt injection | **Never a public issue.** Follow [SECURITY.md](SECURITY.md) |

When you report a problem, include the `squadhq version` output, the relevant part of your `squadhq.yml` (remove secrets and private names), the `squadhq doctor` output and a link to or excerpt of the failed run. Do not paste tokens, private keys or full agent transcripts from private repositories.
