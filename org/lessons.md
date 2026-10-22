# Lessons — append-only. Every agent reads this file on every run.
# Format, one line per lesson:  - [YYYY-MM-DD] [scope] <imperative rule>
#
# Humans write it; the Lessons Harvester (agents/lessons-harvester/prompt.md)
# proposes sourced additions weekly as a PR, and humans merge what they agree
# with. Keep entries short, general and actionable. No ticket numbers or
# file:line references; put the source link in the PR that adds the line.
#
# Starting point: templates/lessons.seed.md holds generic lessons from running
# this system. Copy the ones you agree with here, replacing [seed] with
# today's date.
#
# Entries made redundant by structural enforcement (code, config, CI) move to
# org/lessons-archive.md (created on first use) with a note on what replaced
# them.
#
# Scopes in use: ci, agents, process, review, security, docs, <pack id>.

