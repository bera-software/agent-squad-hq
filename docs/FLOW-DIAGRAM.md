# Flow diagram

**Auto-generated from `config/flows.yml` by `squadhq.flow_diagram` — do not hand-edit this file.** Regenerate after any `config/flows.yml` change: `python -m squadhq.flow_diagram`. CI regenerates it automatically (see `.github/workflows/flow-definition.yml`) and fails the check if this file is out of date with the committed flows.yml.

## ticket-lifecycle

A ticket's life from a plain issue to a merged/closed PR, covering both the approver-authored and self-actuated (Originated-by:) paths.

```mermaid
stateDiagram-v2
    new
    needs_shaping
    shaped
    dispatched
    needs_design
    in_progress
    in_review
    needs_human
    closed
    new --> shaped : product-owner: issues.opened, PO classifies SHAPED
    new --> needs_shaping : product-owner: issues.opened, PO classifies NEEDS SHAPING
    new --> new : approver OR stuck-sweeper: issues.labeled agent:shape (manual, or stuck-sweeper re-f...
    needs_shaping --> shaped : team-thread (final agreement step) OR po-continue: team-thread CONSENSUS, or approver approval/override reply
    shaped --> dispatched : stuck-sweeper: hourly deterministic dispatch: WIP slot free, ready (tria...
    shaped --> needs_design : chief-of-staff: CoS triage duty, size L / architectural
    needs_design --> dispatched : chief-of-staff: design PR merged, gate cleared (see design-gate flow)
    dispatched --> in_progress : implement-job: issues.labeled agent:implement
    in_progress --> in_review : implement-job: PR opened, Closes #N
    needs_design --> dispatched : stuck-sweeper: design PR (Gate-for: #N) merged — reconciled hourly by th...
    in_review --> dispatched : stuck-sweeper: design PR (Gate-for: #N) merged — reconciled hourly by th...
    dispatched --> needs_human : stuck-sweeper: the ticket's PR (implementation or design) was CLOSED wit...
    in_progress --> needs_human : stuck-sweeper: the ticket's PR (implementation or design) was CLOSED wit...
    in_review --> needs_human : stuck-sweeper: the ticket's PR (implementation or design) was CLOSED wit...
    in_review --> closed : approver: approver merges PR (review-loop flow's approved state) — ...
```

## review-loop

A PR's life from open to merged, including the bounded 2-round automated fix loop.

```mermaid
stateDiagram-v2
    pr_opened
    classified
    ci_running
    review_pending
    changes_requested
    fix_round
    needs_human
    approved
    merged
    pr_opened --> classified : detect-changes: pull_request or push
    classified --> ci_running : backend-ci or frontend-ci: pull_request, not draft
    classified --> review_pending : reviewer: pull_request, not draft, needs_review == 'true'
    review_pending --> approved : reviewer: Reviewer APPROVE
    review_pending --> changes_requested : reviewer: Reviewer REQUEST CHANGES
    changes_requested --> fix_round : engineer: issues.labeled review:changes-requested (round < 2)
    fix_round --> review_pending : engineer: push to PR branch re-triggers pull_request synchronize
    fix_round --> needs_human : review-fix-job: round cap (2) reached
    approved --> merged : auto-merge job, self-merge job, or approver: pull_request_review state=='approved' by non-Bot OWNER/ME...
    approved --> approved : qa-preview: deployment_status success, environment contains 'Preview'
    approved --> approved : update-prs (on push) / self-merge + auto-merge (on approval): base branch pushed while this PR is merge-bound, or the P...
```

## team-thread

Collaborative assessment round for a NEEDS SHAPING proposal — Architect + Engineer always, Legal/Finance/Growth conditional on the PO's own Team-input-scope: line. Never runs for a SHAPED ticket (proportional, not a blanket tax).

```mermaid
stateDiagram-v2
    proposal_posted
    team_input_requested
    architect_assessed
    engineer_assessed
    legal_assessed
    finance_assessed
    growth_assessed
    agreement_classified
    proposal_posted --> team_input_requested : product-owner: PO posts a comment containing @team-thread with a Team-in...
    team_input_requested --> architect_assessed : architect: issue_comment.created, contains(comment.body,'@team-threa...
    architect_assessed --> engineer_assessed : engineer
    engineer_assessed --> legal_assessed : legal
    engineer_assessed --> finance_assessed : finance
    engineer_assessed --> growth_assessed : growth
    legal_assessed --> agreement_classified : product-owner: all in-scope assessments posted
    finance_assessed --> agreement_classified : product-owner: all in-scope assessments posted
    growth_assessed --> agreement_classified : product-owner: all in-scope assessments posted
    engineer_assessed --> agreement_classified : product-owner: all in-scope assessments posted
```

## debt-intake

Weekly tech-debt scan -> dedup-or-propose -> CoS accept/decline. Lives almost entirely OUTSIDE router.yml — tech-debt-weekly.yml + CoS duty 3. Debt-proposal issues deliberately carry no Originated-by: line so po-shape's widened bot-exclusion condition does not pull them into PO intake (see ticket-lifecycle's `new` state — this flow bypasses it).

```mermaid
stateDiagram-v2
    repo_scanned
    finding_dedup_checked
    existing_issue_commented
    debt_proposal_created
    cos_impact_checked
    accepted
    declined
    repo_scanned --> finding_dedup_checked : debt-scanner: cron Sun 03:00 UTC, matrix over every project repo
    finding_dedup_checked --> existing_issue_commented : debt-scanner
    finding_dedup_checked --> debt_proposal_created : debt-scanner
    debt_proposal_created --> cos_impact_checked : chief-of-staff
    cos_impact_checked --> declined : chief-of-staff
    cos_impact_checked --> accepted : chief-of-staff
```

## design-gate

needs-design -> Architect's design PR (Gate-for: #N) -> merge -> CoS clears the gate -> agent:implement. Mostly CoS duty 6, not router.yml, once the design PR itself is open.

```mermaid
stateDiagram-v2
    needs_design_labeled
    branch_cut
    design_pr_opened
    design_pr_merged
    gate_cleared
    needs_design_labeled --> branch_cut : architect: issues.labeled needs-design
    branch_cut --> design_pr_opened : architect
    design_pr_opened --> design_pr_merged : approver: approver merges (via review-loop flow, same as any PR)
    design_pr_merged --> gate_cleared : chief-of-staff: next CoS run reads recent_merged, finds Gate-for: #N
```

## content-flow

Growth/SEO content -> project-repo ticket carrying channel:blog or channel:external + Originated-by: -> po-shape validates (positioning, claims, duplicate posts) -> blog: normal ticket-lifecycle (Engineer PR adds the post); external: awaiting-approval, approver posts by hand and closes.

```mermaid
stateDiagram-v2
    content_ticket_opened
    blog_ready
    external_awaiting_approver
    declined_or_backlogged
    content_ticket_opened --> blog_ready : product-owner: issues.opened (Originated-by: in body) -> po-shape
    content_ticket_opened --> external_awaiting_approver : product-owner: issues.opened (Originated-by: in body) -> po-shape
    content_ticket_opened --> declined_or_backlogged : product-owner
```

## override-flow

Approver overrides a PO verdict, or approves/changes a NEEDS SHAPING proposal, outside the team-thread CONSENSUS path.

```mermaid
stateDiagram-v2
    proposal_pending
    tickets_cut
    revised_proposal
    parked
    overridden
    dispatched
    proposal_pending --> parked : product-owner: PO VERDICT = BACKLOG or DECLINE
    proposal_pending --> tickets_cut : po-continue: issue_comment by non-Bot OWNER/MEMBER, approval or approv...
    proposal_pending --> revised_proposal : po-continue: issue_comment, change request
    parked --> overridden : po-continue: issue_comment by non-Bot OWNER/MEMBER on a verdict:backlo...
    overridden --> dispatched : po-continue: same run as the override
```

## Label states — issues

Which labels an open issue carries in each state. Only these state labels count: `proposal:pending`, `triaged:ready`, `debt-proposal`, `debt`, `needs-design`, `agent:implement`, `status:in-progress`, `status:in-review`, `needs-human`. Everything else (dept, priority, verdict, channel, risk) is metadata. **Any combination not listed is illegal** — the stuck sweeper repairs it if a rule below covers it, otherwise escalates it with `needs-human`.

| State labels | Flow state | Meaning |
|---|---|---|
| *(none)* | `ticket-lifecycle.new` | unshaped ticket, or not a ticket at all (report, content draft) |
| `proposal:pending` | `ticket-lifecycle.needs_shaping` | PO proposal awaiting team-thread / approver |
| `triaged:ready` | `ticket-lifecycle.shaped` | shaped; the sweeper dispatches it into a free WIP slot |
| `debt-proposal` | `debt-intake.debt_proposal_created` | debt finding awaiting CoS intake |
| `debt` | `debt-intake.accepted` | accepted debt; dispatched like triaged:ready |
| `needs-design` | `ticket-lifecycle.needs_design` | Architect owes a design PR |
| `needs-design` + `status:in-progress` | `ticket-lifecycle.in_progress` | Architect writing the design |
| `needs-design` + `status:in-review` | `ticket-lifecycle.in_review` | design PR open |
| `agent:implement` | `ticket-lifecycle.dispatched` | Engineer dispatched, not started |
| `agent:implement` + `status:in-progress` | `ticket-lifecycle.in_progress` | Engineer working |
| `agent:implement` + `status:in-review` | `ticket-lifecycle.in_review` | PR open (or report delivered) |
| `status:in-review` | `ticket-lifecycle.in_review` | report ticket delivered, waiting for the approver to close it |
| `needs-human` | `ticket-lifecycle.needs_human` | approver's call |

**Modifiers** — ride along on any state above without changing it:

- `needs-human` — parked on the approver; the underlying state is kept for when it is lifted
- `debt` — accepted tech debt — a kind of ticket, so it rides along once dispatched

**Automatic repairs** (first match wins; every group needs one label present):

| When | Unless | Action | Why |
|---|---|---|---|
| (`verdict:backlog` or `verdict:decline` or `P3`) AND (`agent:implement` or `needs-design` or `triaged:ready`) | `verdict:overridden` | escalate (`needs-human`) | parked (backlog/decline/P3) AND dispatched — either the park or the dispatch is wrong, and that is the approver's call. |
| (`debt-proposal`) AND (`triaged:ready`) | `verdict:backlog`, `verdict:decline`, `P3` | remove `debt-proposal`, `triaged:ready`; add `debt` | a `debt-proposal` marked `triaged:ready` is accepted debt — relabelled to `debt` so it can dispatch. |
| (`debt-proposal`) AND (`debt`) | — | remove `debt-proposal` | already accepted as `debt` — the proposal label is stale. |
| (`proposal:pending`) AND (`triaged:ready`) | `agent:implement`, `needs-design` | escalate (`needs-human`) | both `proposal:pending` (needs shaping) and `triaged:ready` (shaped) — the PO has to settle which. |
| (`triaged:ready` or `debt-proposal`) AND (`agent:implement` or `needs-design`) | — | remove `triaged:ready`, `debt-proposal` | already dispatched — `triaged:ready` is the step before dispatch. |
| (`proposal:pending`) AND (`agent:implement` or `needs-design`) | — | remove `proposal:pending` | already dispatched — the proposal stage is over. |

**Fact-dependent repairs** (which label wins depends on PR state; `squadhq.stuck_sweeper` normalize_issue):

- `agent:implement` + `needs-design`: design PR merged -> remove `needs-design`; otherwise remove `agent:implement` (design first)
- `status:in-progress` + `status:in-review`: PR open -> remove `status:in-progress`; otherwise remove `status:in-review`

## Label states — PRs

Which labels an open PR carries in each state. Only these state labels count: `ready-to-merge`, `review:changes-requested`, `needs-approver-merge`, `needs-human`, `status:in-progress`, `status:in-review`. Everything else (dept, priority, verdict, channel, risk) is metadata. **Any combination not listed is illegal** — the stuck sweeper repairs it if a rule below covers it, otherwise escalates it with `needs-human`.

| State labels | Flow state | Meaning |
|---|---|---|
| *(none)* | `review-loop.review_pending` | opened — CI and review pending (or review in progress) |
| `review:changes-requested` | `review-loop.changes_requested` | Reviewer asked for changes; the fix round runs |
| `ready-to-merge` | `review-loop.approved` | Reviewer approved; self-merge deciding, or auto-merge armed and waiting on ci-gate |
| `ready-to-merge` + `needs-approver-merge` | `review-loop.approved` | approved but not auto-mergeable (risk above low, human-authored, failed check) — waiting on the approver's click |
| `needs-human` | `review-loop.needs_human` | approver's call |

**Exclusive** — metadata labels of which at most one may be present:

- `risk:low` / `risk:medium` / `risk:high`

**Modifiers** — ride along on any state above without changing it:

- `needs-human` — parked on the approver (fix-round cap hit, stuck re-fires); the review state is kept

**Automatic repairs** (first match wins; every group needs one label present):

| When | Unless | Action | Why |
|---|---|---|---|
| (`status:in-progress` or `status:in-review`) | — | remove `status:in-progress`, `status:in-review` | `status:*` labels track the ISSUE; on a PR they are noise the board and readers misread. |
| (`needs-approver-merge`) | `ready-to-merge` | remove `needs-approver-merge` | `needs-approver-merge` without an approval (`ready-to-merge`) is stale. |

**Fact-dependent repairs** (which label wins depends on PR state; `squadhq.stuck_sweeper` normalize_pr):

- `ready-to-merge` + `review:changes-requested`: latest verdict wins (label events): newer REQUEST CHANGES -> remove `ready-to-merge` and `needs-approver-merge`; newer APPROVE -> remove `review:changes-requested`
- two or more `risk:*`: keep the most recently applied risk label (the Reviewer's latest verdict), remove the rest
