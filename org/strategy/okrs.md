# OKRs

Every reprioritisation decision references an objective. If a ticket maps to
nothing here, it waits. The owner (or approvers) write this file; agents only
measure against it.

> **Structure note (machine-readable — do not reflow).**
> - The current quarter is the first heading of the form `## Q<digit> <year>`
>   (for example `## Q1 2027`). Replace the `## Q<N> <year>` heading below
>   with the real quarter when you fill this in.
> - The OKR Tracker (`agents/okr-tracker/prompt.md`) rewrites only the
>   `Progress:` and `Evidence:` lines under each KR. Objectives, KR text and
>   Guardrails are never touched by an agent.
> - A KR whose target is still a placeholder (`<…>`, `$X`, `N%`, `TBD`) is
>   reported as "not yet measurable", never guessed against.
> - Text inside HTML comments is ignored by the agents.

<!--
Example of a filled-in quarter (delete or keep as a reference):

## Q1 2027 (January — March)

### O1: Ship a product users pay for
- **KR1:** 10 recorded discovery conversations with target users.
  Progress: not yet measured
  Evidence: —
- **KR2:** 3 paying customers.
  Progress: manual update needed — billing dashboard
  Evidence: —

### O2: Keep the code trustworthy
- **KR1:** Zero open `severity:high` issues older than 7 days.
  Progress: not yet measured
  Evidence: —
- **KR2:** Changed-file coverage ≥ 70% on merged PRs.
  Progress: not yet measured
  Evidence: —

### O3: Keep the machine cheap and healthy
- **KR1:** Agent + infrastructure spend < $200/month.
  Progress: not yet measured
  Evidence: —
- **KR2:** Human time on the board ≤ 1 hour/day (proxy: escalations + needs-human per day).
  Progress: not yet measured
  Evidence: —
-->

---

## Q<N> <year>

### O1: <objective — the outcome you want, in plain words>

<one or two sentences: why this matters now>

- **KR1:** <measurable result, e.g. "N active users per week">
  Progress: not yet measurable — KR has no concrete target
  Evidence: —
- **KR2:** <measurable result>
  Progress: not yet measurable — KR has no concrete target
  Evidence: —

### O2: <objective>

- **KR1:** <measurable result>
  Progress: not yet measurable — KR has no concrete target
  Evidence: —

### O3: <objective>

- **KR1:** <measurable result>
  Progress: not yet measurable — KR has no concrete target
  Evidence: —

---

## Guardrails

Standing policy, not tied to a quarter. These stay when a quarter is archived.
Replace these examples with your own.

- Every task is checked against: does this delay learning from users?
  Refactors, audits and polish default to "yes" unless they unblock that.
- No fabricated distribution: no bought upvotes, seeded comments, fake
  personas or paid link schemes.
- Security and privacy fixes outrank features at equal priority.
- <a positioning or scope line the product must never cross>

---

## Archive

Finished quarters move here (the `## Q<N> <year>` block only; Guardrails stay
in place). The OKR Tracker prompts for this at the start of a new quarter with
a hub issue `OKR refresh due — Q<N> <year>`, but never performs the move and
never writes the next quarter's objectives.

_(nothing archived yet)_
