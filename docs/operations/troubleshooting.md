# Troubleshooting

Start with:

```bash
squadhq doctor              # every installation check, with the fix for each failure
squadhq validate --explain  # model per job and UTC cron per schedule
```

Then look at the failed run in the project's **Actions** tab and at the hub's open `escalation` issues.

## Nothing happens when I open an issue

| Check | Fix |
|---|---|
| Is the author trusted? | Only `OWNER`, `MEMBER` and `COLLABORATOR` (`SQUADHQ_TRUSTED_ASSOCIATIONS`) start the Product Owner. Others get `needs-triage`. Organisation members with private membership can show as `CONTRIBUTOR` or `NONE` to the API; make membership public or add them as collaborators |
| Is the stub there and merged? | The project needs `.github/workflows/agents.yml` on its default branch. Re-run onboarding |
| Are Actions enabled for the project? | Repository Settings > Actions > General |
| Can the project read the secrets and variables? | Org-level secrets and variables must be visible to the project (`--visibility all` or selected repositories). `squadhq doctor --check secrets` and `--check variables` |
| Is the App installed on the project **and** the hub? | `squadhq doctor --check app` |
| Is the job disabled? | Look for `po-shape` (or the job key) in `SQUADHQ_DISABLED_AGENTS` |

## The drill times out

`squadhq drill` waits 15 minutes for the bot to answer the trigger phrase. If it fails, open the Actions tab of the drill repository and find the `mention` job. Common causes: `SQUADHQ_APP_CLIENT_ID` or `SQUADHQ_APP_PRIVATE_KEY` missing or not visible to the project, the App not installed on the hub, or no model credential.

## Agent jobs fail authentication

- With `auth: api`, check `ANTHROPIC_API_KEY` is set and visible to the project.
- With `auth: subscription`, check `CLAUDE_CODE_OAUTH_TOKEN`. `squadhq doctor --check token-expiry` warns as it nears its one-year lifetime. Hub loops still need `ANTHROPIC_API_KEY`.
- A model id the provider does not know: `squadhq doctor --check models` warns about unknown ids.

## "Paused: capacity" comments

On subscription auth, a capacity trip pauses coding jobs for 60 minutes and opens an escalation (`state/capacity.json` in the hub). Wait, or set `providers.fallback_on_capacity_trip: true` to fall back to metered API auth.

## `BUDGET CAP HIT` issue

Hub loops stopped because the month's estimated spend reached `SQUADHQ_MONTHLY_BUDGET_USD`. Raise `budgets.monthly_usd`, run `squadhq render`, update the variable, and close the issue. Loops resume on their next schedule. See [budgets and costs](../concepts/budgets-and-costs.md).

## The PR never merges

| Symptom | Cause |
|---|---|
| `needs-approver-merge` label | Expected in the default `approvers` mode. Approve the PR as an owner or member |
| Approved, but nothing happens | Turn on **Allow auto-merge** and **Allow squash merging** in the repository. The `auto-merge` job's summary says so if the request failed |
| Auto-merge armed but waiting | `hub / ci-gate` has not passed. Open its summary: it lists which checks applied and which failed |
| `hub / ci-gate` is not a required check | `squadhq doctor --check rulesets`; re-run `squadhq init --apply` to create the ruleset |
| Review loop stopped with `needs-human` | The review-fix round cap (2) was reached. Read the `[fix-round STOP]` digest and decide |

## A gate fails

The gate job's step summary lists each finding. Tune with the [gate variables](../configuration/variables-and-secrets.md#gate-tuning): `SQUADHQ_SAST_SEVERITY`, `SQUADHQ_SCA_MODE`, `SQUADHQ_LICENSE_DENYLIST`, `SQUADHQ_IAC_SEVERITY`, or remove a gate from `security.gates`.

## Schedules run at the wrong time

Crons are UTC, rendered for one daylight-saving offset (`org.dst`). With the default `daylight`, jobs run one hour earlier in winter. GitHub cron is also best effort and can start late. See [schedules and time zones](../configuration/schedules-and-timezones.md).

## `squadhq render --check` fails in CI

Someone edited `squadhq.yml` without rendering, or edited a generated file by hand. Run `squadhq render` and commit the result. Generated files say "Do not edit by hand".

## Validation errors

Every error has a dotted path and a hint:

```text
error: projects[0].packs[1]: ...
    hint: ...
```

Unknown keys are errors (typos get a "did you mean" hint). Quote times: YAML 1.1 reads an unquoted `07:30` as a number.

## Tickets look stuck

The stuck sweeper re-fires waiting work hourly during working hours. Run it now from the hub's Actions tab (**Stuck Sweeper**, `workflow_dispatch`), or locally with `python -m squadhq.stuck_sweeper --dry-run` to see what it would do.

## Server mode

See the [Compose troubleshooting table](../deploy/docker-compose.md#troubleshooting): webhooks returning 401, jobs stuck `queued` or `dead`, and a scheduler that never fires.

## Still stuck?

Ask in [Discussions](https://github.com/bera-software/agent-squad-hq/discussions) with the output of `squadhq version`, `squadhq doctor` and the relevant run log. See [SUPPORT.md](https://github.com/bera-software/agent-squad-hq/blob/main/SUPPORT.md).
