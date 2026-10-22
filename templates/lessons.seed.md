# Lessons seed: generic starter lessons

These lessons are distilled from running an agent-operated software team. They are generic and carry no citations.
They are **not** read by agents from this file. To adopt one, copy it into `org/lessons.md` and
replace `[seed]` with today's date (`- [YYYY-MM-DD] [scope] <rule>`). After that, the Lessons
Harvester only adds lessons that cite a real source in your own history.

Adopt only the lessons you agree with. Every line in `org/lessons.md` is read on every agent run.

## Agents and runners
- [seed] [agents] Never spawn background subagents on an ephemeral runner. It is destroyed when the session ends, and background children die silently with their work. Disable background tasks in the environment as well as asking in the prompt.
- [seed] [agents] Prefer a structural cap to a prompt instruction. A limit enforced in code, such as truncating a list or rejecting extra actions, holds; a limit that is only requested in a prompt does not.
- [seed] [agents] A repo-level tool settings file can deny what a run-level allowlist grants, and deny usually beats allow. When a permitted command is refused, audit the target repo's deny rules first.
- [seed] [agents] Telling an agent to read a file it has no tool to read is inert text. Inject the data into its context instead.
- [seed] [agents] For structured-output model calls, disable or cap any reasoning mode that shares the output token budget. Otherwise the call can spend the whole budget before it emits any answer.
- [seed] [agents] A successful, billable response with no text in it is its own failure mode. Classify it separately from API errors, and always log the stop reason and token usage.
- [seed] [agents] Parse model-produced JSON leniently about control characters. Long markdown fields often contain literal newlines even when the intent is clear.
- [seed] [agents] The author of an agent-posted comment may differ depending on which tool path posted it. Never key a gate on a single expected author identity.
- [seed] [agents] Verify identity-matching rules, such as bot allowlists and login comparisons, against the exact string the tool compares. Suffixes and display forms often differ.

## Delivery and process
- [seed] [process] Pushing is part of the deliverable. Verify that the push succeeded and that the commit appears on the PR before calling work done. On an ephemeral runner an unpushed commit is lost.
- [seed] [process] Retry a transient failure before escalating, and tag exhausted retries distinctly. Escalating on the first hiccup trains people to ignore escalations.
- [seed] [process] Labels never imply state. "Ready to merge" means awaiting approval, not merged. Assert a merge only from the actual merge record.
- [seed] [process] Make gate-clearing idempotent. A merged design can stay in "recently merged" data for several cycles, so check that the follow-up label isn't already applied before you apply it again.
- [seed] [process] Register every label in the label registry before any agent uses it. Unregistered labels work only where someone created them by hand, and fail silently everywhere else.
- [seed] [process] A merged design or ADR does not mean the work it describes has landed. Check the code itself before treating a design as executed.
- [seed] [process] A generic "continue" or "looks good" does not answer a specific open question. Implement on a question only when a human reply names the missing value.
- [seed] [process] Before claiming the next sequential document number, such as an ADR, check open PRs as well as the integration branch, and check again just before merge.
- [seed] [process] When two sources of rules for the same mechanism conflict, follow the more specific and more recently maintained one, and report the gap. Don't block on it silently.

## Review and risk
- [seed] [review] Leave verdict labels, such as changes-requested and ready-to-merge, to the review step alone. An implementer must not add or remove them on its own judgment.
- [seed] [review] Cap a PR's risk at the worse of its diff risk and any unverified or fabricated claim in the PR or its design. A governance red flag outweighs how safe the diff looks.
- [seed] [review] When unsure of a risk level, round up. A wrong "low" is the one expensive mistake.

## CI and git
- [seed] [ci] Build required checks around one always-run gate job that computes pass or fail from the relevant jobs. A skipped required check never counts as passed.
- [seed] [ci] When CI cannot compute a diff (merge commits, force pushes, first commits), fail open on job selection: run the job rather than skip it.
- [seed] [ci] Fail closed on an unreadable policy or budget file. A false pause is cheaper than a surprise bill or an unreviewed change.
- [seed] [ci] During a rebase, "ours" is the upstream side, not your branch. Git inverts ours and theirs between merge and rebase, so verify before you automate conflict resolution.
- [seed] [ci] After automated conflict resolution, run the build and push with --force-with-lease, never --force.
- [seed] [ci] A green build after automated conflict resolution proves only that the code compiles. Read the resolved prose (docs, changelogs, lessons) by eye, because it can be interleaved into nonsense.
- [seed] [ci] Default workflow tokens are often blocked from writing workflow files or capped to read access. Use a scoped App token, and explicitly decide which jobs (if any) may push.

## Docs and templates
- [seed] [docs] A file:line citation rots as soon as the file changes. Cite by symbol or stable anchor, and never assume an old citation is still true.
- [seed] [docs] Assert that no template placeholder survives substitution. A missing substitution otherwise ships silently into every generated file.
