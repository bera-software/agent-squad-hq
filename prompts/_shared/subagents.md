# Subagent rules

You may use the Task tool to run subagents for parallel work, **foreground
only**: block until every subagent returns. Never spawn background agents.
The runner is destroyed when your session ends and background children die
silently with their work.

After the subagents return, you (the parent) collect their results and
produce the deliverable yourself. Never end a session with "waiting for
agents". Run at most `flood-guards.subagent_cap_default` (in
`config/flows.yml`) subagents per run. Subagents inherit these rules and
`prompts/_untrusted-input.md`.
