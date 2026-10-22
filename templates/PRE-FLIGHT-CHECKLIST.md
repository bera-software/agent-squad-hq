# Pre-flight checklist

Complete this before agents run unattended. The onboarding audit (`squadhq onboard --mode audit`, or the Onboard Project workflow) checks the items it can see and posts a pass/fail report. The checklist below also covers what the audit cannot verify.

## A. Hub, one-time setup
- [ ] The hub repo exists. The default name is `agent-squad-hq`; if you use another name, set `vars.SQUADHQ_HUB_REPO`.
- [ ] The GitHub App is installed **only on the repos it needs**, with least-privilege permissions. Set the org or repo variable `SQUADHQ_APP_CLIENT_ID` and the secret `SQUADHQ_APP_PRIVATE_KEY`. Each workflow mints a short-lived token from this App per run. Never use a personal access token.
- [ ] The bot login is set in `vars.SQUADHQ_BOT_LOGIN` if your App slug is not `squadhq-ops`.
- [ ] The model provider API key is stored as a secret. It is the default auth path for coding agents (`config/auth.json` → `"coding_agents_auth": "api"`).
- [ ] Optional: a subscription OAuth token for coding agents. **Using it is your responsibility**: check your provider's terms, record the token's expiry, and set a reminder to renew it. Nothing in the hub tracks that renewal. Without a valid token, jobs configured for subscription auth fail to authenticate.
- [ ] A **hard monthly spend cap** is set in the provider console, at about twice your configured monthly budget.
- [ ] `squadhq.yml` has been reviewed: org, approvers, timezone (`org.timezone`; schedules are rendered to UTC from it), budget, merge mode, and projects with their packs.
- [ ] You have made `org/CLAUDE-org.md`, `org/strategy/okrs.md` and `org/lessons.md` your own. Optionally, adopt lessons from `templates/lessons.seed.md`.
- [ ] Notifications are set: watch the hub repo, and get email on failed workflows.

## B. Per project
- [ ] The onboarding PR has been reviewed and merged. It adds the stub workflow, `CLAUDE.md`, `docs/adr/` and `docs/lessons.md`.
- [ ] The "Project specifics" and "Branching" sections of `CLAUDE.md` are filled in: glossary, do-not-touch areas, environments and the integration branch.
- [ ] The stub workflow passes **only the named secrets it needs**. It never uses `secrets: inherit`.
- [ ] The stub pins the hub to a **release tag or commit SHA**, never a moving branch.
- [ ] Any build integration snippets from the selected packs (coverage, architecture tests, e2e smoke) are merged.
- [ ] Branch protection on the integration branch:
  - Require **exactly one** status check: `hub / ci-gate`. Don't list individual CI jobs. They are skipped on PRs they don't apply to, and a skipped check never satisfies a required one. `ci-gate` always runs and reports on whichever jobs were relevant.
  - Require a pull request; no direct pushes; no bypass actors.
- [ ] Preview deploys per PR are on, if you have a frontend. The production deploy stays **off** until you are ready, with environment approvals and a rollback path.
- [ ] Optional: a codebase knowledge graph, with pinned tool versions and only the generated artefacts committed.
- [ ] Re-run the audit until it passes, or consciously accept each remaining failure.

## C. First-run drill
1. Open a trivial ticket yourself and dispatch it. Watch the PR open, the review verdict arrive, and CI and coverage report.
2. Confirm that a failing check blocks the merge.
3. Run the Chief of Staff and the Daily Brief manually. Check that the brief appears and is split correctly per project.
4. Open an intentionally ambiguous ticket. The agent should ask questions and apply `needs-human`, not guess.
5. Only then enable the scheduled runs.

## D. Standing safety properties (check you haven't weakened them)
- Agents never deploy.
- Merges need an approver. The only exception is risk-based auto-merge, which you must enable explicitly, and then only with SAST, SCA and secret scanning as required checks and the path-based risk floor in place. A model can raise risk but never lower it below the floor.
- Tickets that came from web research or external authors always need a human merge.
- WIP limits per project and action caps per run are enforced in code. The budget kill switch halts the hub loops.
- Growth and SEO output is drafts only. Nothing is posted or published without a human.
- Legal output is always marked "not legal advice".
- Agents never edit `.github/workflows/`, never handle secrets, and have no merge or deploy capability.
