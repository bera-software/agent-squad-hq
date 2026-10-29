# FAQ

## Can an agent merge or deploy?

No. No tool profile grants `gh pr merge` or the merge API, and agents hold no deploy secrets. Merges are started by deterministic jobs after a policy check in code, and by default they need a human approval. Production deploys run only from a published release behind your `production` environment's reviewers. See the [security model](../security/security-model.md).

## Do I need a server?

No. Actions mode runs entirely in GitHub Actions. You need [server mode](../getting-started/quickstart-server-mode.md) for Jira, Linear, Trello or GitLab, for punctual time-zone-aware schedules, or for the dashboard and mobile app.

## Which models does it use?

Coding agents run on Claude Code in Actions mode. Hub loops use Anthropic models by default; adapters exist for OpenAI, Azure OpenAI, Gemini, Bedrock and OpenAI-compatible servers, with [current limitations](../concepts/providers.md#current-limitations) in Actions mode.

## Can I use a Claude subscription instead of an API key?

For coding agents, yes: `providers.anthropic.auth: subscription` with `CLAUDE_CODE_OAUTH_TOKEN`. Whether automated use is allowed is your responsibility under the provider's terms. Hub loops still need `ANTHROPIC_API_KEY`. The default is API-key auth.

## How much does it cost?

It depends on your volume and models. Every coding-agent run is recorded in the monthly cost-ledger issue, and hub loops stop at the monthly cap. Coding agents are not capped by that budget, so set a hard limit with your provider too. See [budgets and costs](../concepts/budgets-and-costs.md).

## Can people outside my organisation trigger the agents?

Not by default. Only `OWNER`, `MEMBER` and `COLLABORATOR` can start LLM jobs. Issues from others get `needs-triage` for a human, and fork PRs get CI and gates but no agent. Do not add `NONE` to the allowed associations on a public repository.

## Does it work on public repositories?

Yes, with care: keep transcripts off (logs of public repositories are public), turn on **Require approval for all outside collaborators** for fork workflows, and remember that agent comments are public.

## Which languages are supported?

Any, through the language-neutral prompts. The 17 [stack packs](../concepts/packs.md) add build commands, review rules and risk paths for common stacks.

## Can I turn agents off?

Yes. Department agents and catalogue skills switch off in `squadhq.yml` `agents:`. Core router jobs switch off through `SQUADHQ_DISABLED_AGENTS` with their job key. See [switching roles off](../concepts/roles-and-skills.md#switching-roles-and-skills-off).

## Does my code leave GitHub?

In Actions mode, code is processed on GitHub-hosted runners and sent to your model provider as part of prompts. In server `byo-compute` mode, coding jobs still run on GitHub-hosted runners. In `worker` mode they run on your host.

## Why do my scheduled jobs run an hour early in winter?

GitHub cron is UTC-only, so one offset is chosen per render. The default favours "never late". See [daylight saving time](../configuration/schedules-and-timezones.md#daylight-saving-time).

## Is there a hosted version?

Not yet. Cloud and Enterprise editions are planned. See [editions](../editions.md).

## How do I report a security issue?

Privately, through GitHub Security Advisories, as described in [SECURITY.md](https://github.com/bera-software/agent-squad-hq/blob/main/SECURITY.md). Prompt-injection bypasses are in scope.
