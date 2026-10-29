# Quickstart: Actions mode

This guide takes a GitHub organisation from nothing to a first agent-written pull request. It takes about 15 minutes, most of it waiting for workflows. The example organisation is `acme`, the hub is `acme/agent-squad-hq` and the first project is `acme/storefront`.

Check the [requirements](requirements.md) first.

## 1. Create your hub repository

The hub is a copy of this repository. It holds `squadhq.yml`, the prompts, the packs and the reusable workflows. Any name works.

```bash
git clone https://github.com/berasoftware/agent-squad-hq agent-squad-hq
cd agent-squad-hq
gh repo create acme/agent-squad-hq --private --source . --push
```

Keep the hub private if your `squadhq.yml`, briefs or reports are confidential. The hub's own issues receive daily briefs, rollups, escalations and the cost ledger.

## 2. Install the CLI

=== "From source (today)"

    ```bash
    python -m pip install -e .        # inside the hub checkout
    squadhq version
    ```

=== "From PyPI (from v0.1.0)"

    ```bash
    pipx install agent-squad-hq
    squadhq version
    ```

    The release workflow publishes to PyPI through trusted publishing when a `v*` tag is pushed. Until the first release exists, install from source.

## 3. Run `squadhq init`

`init` asks a few questions (owner, hub repository, time zone, working hours, approvers, auth mode, monthly budget, project repositories and their packs), writes `squadhq.yml`, and plans the GitHub setup. Without `--apply` it only prints what it would do.

```bash
squadhq init                        # interview + dry run
squadhq init --create-app --apply   # write squadhq.yml, create the App, apply the GitHub steps
```

Non-interactive form:

```bash
squadhq init --non-interactive --apply --create-app \
  --owner acme --hub-repo acme/agent-squad-hq --timezone Europe/London \
  --approver @acme/leads --auth api --budget 100 \
  --project acme/storefront --detect-packs
```

What `init --apply` does:

1. Writes `squadhq.yml` (refuses to overwrite an existing one without `--force`) after validating it.
2. With `--create-app`: creates **two** GitHub Apps through GitHub's "create App from manifest" page, one after the other in your browser:
    - the **ops** App ([`app-manifest.json`](https://github.com/berasoftware/agent-squad-hq/blob/main/deploy/github-app/app-manifest.json), default slug `<owner>-squad`): routing, labels, state, and the policy approval that lets a PR merge;
    - the **agent** App ([`app-manifest-agent.json`](https://github.com/berasoftware/agent-squad-hq/blob/main/deploy/github-app/app-manifest-agent.json), default `<owner>-squad-agent`): what the coding agents run as. It can push branches and open PRs, but cannot edit workflows, approve its own push or merge.

    Each private key is saved as `<slug>.private-key.pem` (mode 0600) in `--pem-dir` (default: the hub directory, where `*.pem` is git-ignored).
3. Stores `SQUADHQ_APP_PRIVATE_KEY`, `SQUADHQ_AGENT_APP_PRIVATE_KEY` and the `SQUADHQ_*` variables (including both client ids), by default as **organisation** secrets and variables visible to all repositories (`--scope org`). `squadhq render` prints the same `gh variable set` / `gh secret set` lines.
4. Creates the labels from `config/flows.yml` in the hub and in each project.
5. Creates or updates a ruleset named `squadhq` on each project's default branch: no deletion, no force push, changes through a PR with **one approval given after the last push** (stale approvals dismissed), the required status check `hub / ci-gate`, and no bypass for any App.

!!! warning "Single-app mode"
    `--single-app` creates only the ops App and the agents run as it. Agent output is then indistinguishable from the ops identity, so every unattended merge (`auto`, `risk_based`, trivial lanes) is disabled and an approver merges everything. `squadhq doctor` warns until you add the agent App.

!!! note "Fork pull requests"
    Fork PRs never reach an agent and never auto-merge. Turn on **Require approval for all outside collaborators** under Settings > Actions > General (organisation or repository) so fork workflows wait for a maintainer.

!!! note "Personal accounts"
    With `--scope repo` the secrets and variables go to the hub repository only. Project workflows read `secrets.SQUADHQ_APP_PRIVATE_KEY` and `vars.SQUADHQ_*` from the **project** repository, so set them there too (the `gh` commands `squadhq render` prints work with `-R owner/project`).

The ops App asks for: contents, pull requests, issues, workflows, actions and Actions variables (write); checks and statuses (read); members (read); and organisation projects (write, optional, for board sync). The agent App asks only for contents, pull requests and issues (write) and checks (read). Each manifest documents why each permission is needed. Neither asks for administration, secrets, environments, deployments or security events.

After `init`, delete the local `.pem` file or move it to a password manager.

!!! tip "Token-broker mode: no App keys in your organisation"
    If a token broker holds the Apps for you (a hosted Squad HQ, or your platform team's service), skip the App creation and run `init` with `--token-broker` instead. See [Token-broker mode](#token-broker-mode) below.

## 4. Install the Apps

Install **both** Apps on the hub and every project repository (`squadhq doctor --check agent-app` verifies the agent App).

Open the App's installation page (`init` prints the link) and install it on the hub and on every project repository.

### Token-broker mode

In token-broker mode ([ADR-0011](../adr/0011-token-broker-mode.md)) your organisation stores **no App private key**. Each job that needs a GitHub App token presents its GitHub Actions OIDC token to a broker you trust, and the broker answers with a short-lived installation token for the ops or the agent App. The broker owns both Apps; you install them on the hub and the project repositories as in step 4.

```bash
squadhq init --non-interactive --apply \
  --owner acme --hub-repo acme/agent-squad-hq --approver @acme/leads \
  --token-broker https://api.squadhq.example \
  --app-slug agent-squad-hq   # the broker's ops App; the agent App defaults to <slug>-agent
```

This writes `hub.token_broker` to `squadhq.yml` and sets the organisation variable `SQUADHQ_TOKEN_BROKER` instead of creating Apps and storing keys (`--create-app` is ignored). Bot logins still come from `hub.bot` and `hub.agent_bot`. For the hosted Squad HQ they are `agent-squad-hq` and `agent-squad-hq-agent`:

```yaml
hub:
  repo: acme/agent-squad-hq
  bot: { app: agent-squad-hq }
  agent_bot: { app: agent-squad-hq-agent }
  single_app: false
  token_broker: https://api.squadhq.example      # or { url: ..., audience: ... }
```

If you only want the admin settings, without writing `squadhq.yml` (for example a hosted install where the service keeps the configuration), apply them with your own `gh` login (admin on the hub and the projects):

```bash
squadhq repo-settings --hub acme/agent-squad-hq --project acme/storefront --project acme/api           # dry run: prints the plan
squadhq repo-settings --hub acme/agent-squad-hq --project acme/storefront --project acme/api --apply
```

It sets three things and nothing else (no Apps, secrets, variables, labels or files):

- the hub's **Actions access level**, so repositories of the same user (`user`) or organisation (`organization`) can call its reusable workflows (skipped for a public hub);
- on each project, **Allow auto-merge** and **Allow squash merging**;
- on each project, the same `squadhq` ruleset `squadhq init --apply` creates: PR required, one approval given after the last push, `hub / ci-gate` required, no force push or deletion.

Settings already in place are left alone, and an existing `squadhq` ruleset is updated rather than duplicated, so re-running is safe. On the GitHub Free plan, rulesets are not available on private repositories: that step is reported as a warning (upgrade, or make the repository public) and the other steps still apply.

How it works and what to check:

- Workflows switch to the broker as soon as `vars.SQUADHQ_TOKEN_BROKER` is set; delete the variable to go back to App keys. `SQUADHQ_TOKEN_BROKER_AUDIENCE` overrides the OIDC audience (default: the broker URL), when your broker expects another one.
- Only the prep, post and deterministic jobs that mint tokens get `id-token: write`. Agent jobs never do, so code an agent runs cannot ask the broker for anything. Project stubs carry `id-token: write` in their permission ceiling: run stub-sync (or re-onboard) before switching a project to broker mode.
- Each job asks the broker for the same narrowed permissions it asks for with App keys, and revokes its tokens when it ends. Agent tokens cover the calling repository, or, from the hub, the hub plus the registered projects (at most 25), as with App keys.
- Post jobs report each agent run's outcome, cost and token counts to the broker (`/v1/broker/usage`). The report is best effort and never fails a run.
- `squadhq doctor` stops asking for `SQUADHQ_APP_PRIVATE_KEY` / `SQUADHQ_AGENT_APP_PRIVATE_KEY`, checks that `SQUADHQ_TOKEN_BROKER` is an https URL and warns when the broker does not answer (`squadhq doctor --check token-broker`).
- The broker can mint tokens for your repositories. Use one you trust as much as you would trust someone holding your App keys; see the [threat model](../security/threat-model.md#token-broker-mode).

## 5. Add the model credential

Never put credentials in `squadhq.yml`.

```bash
gh secret set ANTHROPIC_API_KEY --org acme --visibility all
```

If you chose `providers.anthropic.auth: subscription`, also run `claude setup-token | gh secret set CLAUDE_CODE_OAUTH_TOKEN --org acme --visibility all`. Hub loops still use `ANTHROPIC_API_KEY`. See [variables and secrets](../configuration/variables-and-secrets.md) for the complete list.

## 6. Render and commit

```bash
squadhq validate --explain     # model per job and UTC schedules
squadhq render                 # writes config/*.json and workflow cron lines
git add squadhq.yml config .github
git commit -m "chore: configure the squad"
git push
```

`render` also prints the `gh variable set` commands for every `SQUADHQ_*` variable. `init --apply` has already set them; re-run those commands whenever you change a value that is rendered into a variable.

## 7. Onboard the project

Onboarding opens a PR in the project with the `agents.yml` stub (pinned to the hub commit), adds any missing memory files (`CLAUDE.md`, `AGENTS.md`, `docs/adr/0000-template.md`, `docs/adr/0001-record-architecture-decisions.md`, `docs/lessons.md`), creates labels and posts a readiness report as an issue in the hub.

Run it from the hub's **Actions** tab (**Onboard Project**, `mode: apply`), which uses the App token, or locally with your own `gh` login:

```bash
squadhq onboard acme/storefront --apply    # without --apply: read-only audit
```

Review and merge the onboarding PR in `acme/storefront`. Its body is a checklist:

- **`CLAUDE.md` is a starter.** It is pre-filled from the detected stack packs (commands, coding standards, the review red lines as a "Do not" list, how layering is enforced) and leaves architecture overview, layering rules and test policy as `TODO(owner)`. Agents treat this file as binding, so replace the TODOs and delete defaults that do not apply before merging.
- **Architecture and lint starters.** For each detected pack that ships one, the PR adds the starter architecture config (for example `.dependency-cruiser.cjs` or `.importlinter`) and, when the repo has no linter config, the pack's default (for example `ruff.toml`). The [quality gates](../concepts/quality-gates.md) run as soon as these files exist, so adjust their `TODO(owner)` paths in the same PR, or delete a file to opt out. Nothing is ever overwritten.

## 8. Check everything

```bash
squadhq doctor
```

`doctor` runs 13 checks: the configuration, rendered files, schedule markers, the daylight-saving caveat, model ids, the `gh` login, required secrets, token age, variables, the App installation, and the rulesets. Each failure prints the exact fix. `--offline` skips the GitHub checks and `--check <id>` runs one (`squadhq doctor --list-checks`).

## 9. Run the drill

```bash
squadhq drill --repo acme/storefront --apply
```

The drill opens an issue that mentions the trigger phrase (`@squad` by default), waits up to 15 minutes for the bot to reply, then closes the issue. It passes when the bot answers.

## 10. Your first ticket

Open an issue in `acme/storefront` as a member of the organisation, for example:

> **Add a /health endpoint**
> Return `{"status": "ok"}` with HTTP 200. Add a test.

What happens next:

1. The **Product Owner** classifies the issue. A clear, small ask is labelled shaped and queued. An unclear one gets a proposal and a short team thread first; reply to it to continue.
2. The **stuck sweeper** (hourly during working hours) or the Chief of Staff dispatches the ticket by adding `agent:implement` when a WIP slot is free. You can add `agent:implement` yourself to start at once.
3. The **Engineer** works on an `agent/*` branch and opens a PR that says `Closes #<n>`.
4. CI, the SSDLC gates and the **Reviewer** run. The Reviewer either requests changes (the Engineer gets up to two automatic fix rounds) or approves and applies `ready-to-merge`.
5. With the default `approvers` merge mode, the PR waits for you. Approving it as an organisation owner or member arms GitHub auto-merge, which completes when `hub / ci-gate` is green.

Issues opened by people outside `OWNER`, `MEMBER` and `COLLABORATOR` are not shaped by an agent. They get a `needs-triage` label for a human.

## Next steps

- Tune [`squadhq.yml`](../configuration/squadhq-yml.md): schedules, packs, budgets, which agents run.
- Read the [security model](../security/security-model.md) before you widen any trigger or merge setting.
- When something does not run, see [troubleshooting](../operations/troubleshooting.md).
