# Postmortem: <incident title>

Blameless. This document asks what in the system allowed the incident to happen. It does not ask who is to blame.

- Incident record: <link>
- Severity: <SEVn>
- Date: <YYYY-MM-DD>
- Authors: <names>
- Status: draft | reviewed | actions complete

## Summary
Two or three sentences: what happened, its impact, and how it was resolved.

## Impact
- Duration: <start → end, total minutes>
- Users / requests affected: <numbers>
- Data, financial or contractual impact: <...>

## Timeline (UTC)
| Time | Event |
|---|---|
| | |

## Root cause and contributing factors
- Root cause(s): <...>
- Contributing factors: <process, tooling, test gaps, alerting, docs>

### Five whys
1. Why did <symptom> happen? Because <...>
2. Why <...>? Because <...>
3. Why <...>? Because <...>
4. Why <...>? Because <...>
5. Why <...>? Because <...>

## Detection
How the incident was detected, how long that took, and what would have caught it sooner.

## What went well
- 

## What went poorly
- 

## Where we got lucky
- 

## Action items
| # | Action | Type (prevent / detect / mitigate / process) | Owner | Due | Ticket |
|---|---|---|---|---|---|
| 1 | | | | | #<n> |

Prefer structural fixes, such as code, config, CI checks and alerts, over reminders written in prose.

## Lessons
Add any durable rule to `docs/lessons.md`, or to the hub's `org/lessons.md` if it applies to every project, as one line:

`- [YYYY-MM-DD] [scope] <imperative rule>`
