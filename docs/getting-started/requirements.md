# Requirements

## Actions mode

| You need | Why |
|---|---|
| A GitHub **organisation** (a personal account also works for testing) | The hub and project repositories live in it. The GitHub App is created and installed there |
| Organisation owner rights, at least during setup | `squadhq init` creates the GitHub App, org-level secrets and variables, and repository rulesets with **your** `gh` login |
| GitHub Actions enabled for the hub and project repositories | Every agent and gate runs as a workflow job on GitHub-hosted `ubuntu-latest` runners |
| Python 3.11 or newer on your machine | The `squadhq` CLI. Workflows install the package themselves (Python 3.12 on the runner) |
| The [`gh` CLI](https://cli.github.com/), logged in (`gh auth login`) | `init --apply`, `onboard`, `doctor` (online checks) and `drill` call `gh` |
| A browser on the same machine | `squadhq init --create-app` uses GitHub's App manifest flow with a `127.0.0.1` callback |
| An Anthropic API key (`ANTHROPIC_API_KEY`) | Coding agents run on Claude Code. Hub loops default to Anthropic models too |

Optional:

- `CLAUDE_CODE_OAUTH_TOKEN` if you choose `providers.anthropic.auth: subscription` for coding agents. Whether automated use of a subscription token is allowed is your responsibility under the provider's terms. Hub loops still need `ANTHROPIC_API_KEY`.
- `OPENAI_API_KEY` or `GEMINI_API_KEY` if hub loops use OpenAI or Gemini models. The workflows pass these two through; other providers need extra wiring in Actions mode (see [providers](../concepts/providers.md)).
- A GitHub Projects (v2) board, if you want board sync.

### Private repositories

- **Let projects call the hub.** In the hub repository go to Settings > Actions > General > Access and choose "Accessible from repositories owned by the user/organisation". Without it, private projects cannot use the hub's reusable workflows and every run fails at start-up.
- **Rulesets need a paid plan on private repositories.** GitHub enforces branch rulesets (the required approval and the `hub / ci-gate` check) on private repositories only with GitHub Pro (personal accounts) or GitHub Team and above (organisations). On the Free plan the agents still work and the default merge mode still leaves merging to a human, but GitHub does not block a merge without approval. `squadhq doctor` reports a missing or unenforced ruleset.
- **Personal accounts have no organisation-level secrets.** Use `squadhq init --scope repo` and set the secrets and variables in each project too (see the quickstart).

### Repository settings in each project

- **Allow auto-merge** and **Allow squash merging** must be on. The merge jobs call `gh pr merge --squash --auto`.
- A ruleset on the default branch that requires the `hub / ci-gate` check. `squadhq init --apply` creates it; `squadhq doctor` checks it.
- Required for public repositories: **Require approval for all outside collaborators** for fork pull request workflows (Settings > Actions > General).

### Cost

GitHub Actions minutes are billed by GitHub for private repositories. Model usage is billed by your provider. See [budgets and costs](../concepts/budgets-and-costs.md).

## Server mode

See the [Docker Compose guide](../deploy/docker-compose.md#requirements). In short: a Linux VM with 2 vCPU, 4 GB RAM and 20 GB disk, Docker Engine 24+ with the compose v2 plugin, `openssl`, a DNS name and a TLS-terminating reverse proxy. Kubernetes and managed-cloud options are in [Kubernetes (Helm)](../deploy/kubernetes.md) and [managed cloud runtimes](../deploy/cloud.md).

## Developing Agent Squad HQ itself

Python 3.11+ (CI uses 3.12) and, for the client apps, Node 20 or newer. See [CONTRIBUTING.md](https://github.com/berasoftware/agent-squad-hq/blob/main/CONTRIBUTING.md).
