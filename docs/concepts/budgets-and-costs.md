# Budgets and costs

Agent Squad HQ spends money in three places: model calls by coding agents, model calls by hub loops, and GitHub Actions minutes. This page explains what is measured, what is capped, and what is not capped yet.

## What is enforced today

| Control | Applies to | How |
|---|---|---|
| **Monthly budget cap** | Hub loops (Chief of Staff, Daily Brief, Weekly Rollup, Growth, SEO, Legal, Finance) | `squadhq.agent_runner` keeps a running estimate in `state/usage.json` in the hub (updated with a compare-and-set write). Before each run it compares the month's estimated cost with `SQUADHQ_MONTHLY_BUDGET_USD`. At or above the cap, it opens one `BUDGET CAP HIT: $<cap> for <month>` escalation issue and exits. Hub loops stay paused until you raise the cap or the month changes |
| **Capacity breaker** | Coding agents on subscription auth | `squadhq.capacity_gate` detects a subscription capacity trip, pauses coding jobs for 60 minutes (`state/capacity.json`) and opens an escalation. With `providers.fallback_on_capacity_trip: true` jobs switch to metered API auth instead of pausing (default `false`). With `providers.anthropic.pool` the breaker is per account: a trip pauses that account only, and the pause or fallback applies once every account is tripped. Ledger lines of pooled runs carry `account` |
| **Bounded loops** | All agents | Review-fix rounds (2), Chief of Staff actions per run (20), self-actuated tickets per run (3), subagents per run (4) and other caps in `config/flows.yml` `flood-guards` |
| **WIP limit** | Dispatch | At most 5 open agent PRs per project before the sweeper stops dispatching (`flood-guards.wip_limit_open_prs_per_project`) |
| **Retry caps** | Failures | Transient failures retry at most twice before escalating; hub-loop provider calls make at most 3 attempts with one fallback |

`squadhq render` sets `SQUADHQ_MONTHLY_BUDGET_USD` from `budgets.monthly_usd` (default 100). If the variable is missing, the hub workflows fall back to **50**.

!!! warning "Coding agents are not capped"
    The monthly cap is checked by hub loops only. Coding-agent runs (implement, review, design, skills) are **recorded** in the cost ledger but are not stopped when the cap is reached. Set a hard spending limit in your model provider's console as well.

## What is measured

- **Cost ledger (coding agents).** After every coding-agent run, `squadhq.cost_ledger` appends one JSON line to a pinned hub issue titled `Cost ledger — <YYYY-MM>` (label `dept:finance`). Each line records the repository, job, model, cost in USD, duration, turns, auth mode, whether a fallback model was used, and for merge jobs whether the PR merged without a human click. The Finance agent and Weekly Rollup read it.
- **Usage file (hub loops).** `state/usage.json` holds the month's input and output tokens and the estimated cost.
- **Prices** come from `src/squadhq/providers/prices.yml` (USD per million tokens, with cache and long-context pricing). Ecosystem Watch is meant to keep it current; verify prices against your provider before relying on them. Unknown models price as `None` with a warning.
- **Server mode** exposes `GET /api/v1/costs?month=YYYY-MM` (spend by project, role and model) from the runs recorded in its database.

## Validated but not enforced yet

These `squadhq.yml` keys are accepted and rendered, but no runtime code enforces them yet:

| Key | Today |
|---|---|
| `budgets.per_project_usd` | Rendered into `config/projects.json` as `budget_usd`; server mode reports it at `GET /api/v1/schedules`. Not enforced |
| `budgets.per_run_usd` | Validated against known job names. Not enforced |
| `budgets.on_exceed` (`pause`, `downgrade`, `notify`) | Validated. The hub-loop cap always pauses |
| `projects[].budget_usd` | As `per_project_usd` |

They are part of the [roadmap](../ROADMAP.md).

## Keeping costs down

- Use the `light` tier for high-frequency jobs (`providers.jobs`). `squadhq validate --explain` shows each job's model.
- Switch off department agents you do not need (`agents.growth.enabled: false`, and so on).
- Lower the WIP limit pressure by keeping tickets small: the Product Owner splits large asks.
- Leave `security.show_agent_transcripts: false`; transcripts do not change cost but add log volume.
- Private repositories pay for Actions minutes. Jobs that do not apply are skipped by their `if:` conditions, and the `run-map` debug job is off unless `SQUADHQ_RUN_MAP=true`.
