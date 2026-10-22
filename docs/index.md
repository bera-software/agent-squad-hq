# Agent Squad HQ

Agent Squad HQ is an AI software team that runs in your GitHub organisation. You describe what you want in an issue. Agents shape the request, design it, threat-model it, build it, review it and run the security gates. People approve at the gates they choose, and no agent can merge or deploy.

!!! warning "Alpha"
    Version 0.1.0 has not been released yet. Read [what is verified](#what-is-verified-today) before you depend on a feature.

## Two ways to run it

| | Actions mode | Server mode |
|---|---|---|
| Where the control plane runs | GitHub Actions workflows in your hub repository | A container service you host (API, worker, scheduler, Postgres) |
| Trackers | GitHub Issues | GitHub, Jira Cloud, Linear, Trello, GitLab Issues (through webhooks) |
| Where coding agents run | GitHub-hosted runners in each project | `byo-compute`: a workflow dispatched in the project; `worker`: a sandboxed container on your host |
| Scheduling | GitHub cron (UTC, best effort) | Timezone-aware scheduler with real daylight-saving handling |
| Start here | [Quickstart (Actions mode)](getting-started/quickstart-actions-mode.md) | [Quickstart (server mode)](getting-started/quickstart-server-mode.md) |

## Where to go next

- **New here?** Check the [requirements](getting-started/requirements.md), then follow the [Actions-mode quickstart](getting-started/quickstart-actions-mode.md).
- **Want to know how it works?** Read the [architecture](concepts/architecture.md), then [flows and labels](concepts/flows-and-labels.md) and [roles and skills](concepts/roles-and-skills.md).
- **Configuring it?** The [squadhq.yml reference](configuration/squadhq-yml.md) and [variables and secrets](configuration/variables-and-secrets.md) list every setting.
- **Assessing the risk?** Read the [security model](security/security-model.md), the [threat model](security/threat-model.md) and the [SSDLC](security/ssdlc.md).
- **Something broke?** See [troubleshooting](operations/troubleshooting.md) and run `squadhq doctor`.

## What is verified today

| Area | Status |
|---|---|
| CLI (`init`, `validate`, `render`, `doctor`, `onboard`, `drill`) | Implemented with unit tests. Hub CI runs `squadhq render --check`, pack, skill and eval validation |
| Actions-mode pipeline, SSDLC gates, merge policy, sweeper | Implemented. Carried over from an earlier private deployment and hardened since; workflows pass actionlint and zizmor in CI |
| Claude Code executor in Actions mode | Wired and used by every agent job |
| Codex CLI, Gemini CLI, aider executors | Output normalisers and tool profiles exist; **not runnable in Actions mode** |
| Hub-loop provider adapters | Tested against recorded API responses. Only Anthropic has had sustained live use |
| Server mode | Implemented and tested in-process. Compose, Helm and Terraform examples are linted and validated, **not applied to real accounts** |
| Jira, Linear, Trello and GitLab trackers | Tested against fixtures. Used by server mode only |
| Dashboard and mobile app | **Preview**. Mobile native builds have not been run |
| Cloud and Enterprise editions | **Planned**. See [editions](editions.md) and the [roadmap](ROADMAP.md) |

Some `squadhq.yml` keys are accepted and rendered but not yet enforced. The [configuration reference](configuration/squadhq-yml.md) marks each one.
