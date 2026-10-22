#!/usr/bin/env python3
"""
Generate docs/FLOW-DIAGRAM.md — a Mermaid stateDiagram-v2 for every flow in
config/flows.yml — so the visual representation can never drift from the
definition (it's regenerated from flows.yml every time, never hand-edited).

Usage: python -m squadhq.flow_diagram [flows.yml path] [output .md path]
Defaults (hub checkout, SQUADHQ_HOME): config/flows.yml -> docs/FLOW-DIAGRAM.md

No network, no LLM — pure Python + PyYAML (via flow_reader).
"""
import os
import re
import sys

from squadhq.flow_reader import default_path, load
from squadhq.paths import hub_path

OUT_DEFAULT = "docs/FLOW-DIAGRAM.md"


def _mermaid_id(state):
    """Mermaid state IDs can't contain spaces or most punctuation — states
    here are already snake_case, but sanitize defensively anyway."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(state))


def _edge_label(t):
    parts = []
    actor = t.get("actor")
    if actor:
        parts.append(str(actor))
    trigger = t.get("trigger")
    if trigger:
        # Mermaid edge labels can't contain raw newlines or unescaped
        # quotes/pipes — collapse to one line and strip characters that
        # break the `-->|label|` syntax.
        trigger = " ".join(str(trigger).split())
        trigger = trigger.replace('"', "'").replace("|", "/")
        if len(trigger) > 60:
            trigger = trigger[:57] + "..."
        parts.append(trigger)
    return ": ".join(parts) if parts else ""


def flow_to_mermaid(flow_name, flow):
    lines = ["stateDiagram-v2"]
    states = flow.get("states", [])
    for s in states:
        lines.append(f"    {_mermaid_id(s)}")
    for t in flow.get("transitions", []):
        froms = t.get("from")
        froms = froms if isinstance(froms, list) else [froms]
        to = t.get("to")
        if to is None:
            continue
        label = _edge_label(t)
        for f in froms:
            if f is None:
                continue
            edge = f"    {_mermaid_id(f)} --> {_mermaid_id(to)}"
            if label:
                edge += f" : {label}"
            lines.append(edge)
    return "\n".join(lines)


def generate(flows_path=None):
    data = load(flows_path)
    flows = data.get("flows", {})

    out = []
    out.append("# Flow diagram")
    out.append("")
    out.append(
        "**Auto-generated from `config/flows.yml` by `squadhq.flow_diagram` "
        "— do not hand-edit this file.** Regenerate after any `config/flows.yml` "
        "change: `python -m squadhq.flow_diagram`. CI regenerates it "
        "automatically (see `.github/workflows/flow-definition.yml`) and fails the "
        "check if this file is out of date with the committed flows.yml."
    )
    out.append("")

    for name in flows:
        flow = flows[name]
        out.append(f"## {name}")
        out.append("")
        desc = flow.get("description")
        if desc:
            out.append(" ".join(str(desc).split()))
            out.append("")
        out.append("```mermaid")
        out.append(flow_to_mermaid(name, flow))
        out.append("```")
        out.append("")

    out += label_states_markdown(data)
    return "\n".join(out).rstrip() + "\n"


def _codes(labels):
    return " + ".join(f"`{l}`" for l in labels) or "*(none)*"


def label_states_markdown(data):
    """Tables for config/flows.yml label_states: which label set is which
    state, the modifiers, the automatic repairs."""
    out = []
    for kind, cfg in (data.get("label_states") or {}).items():
        noun = {"pr": "PR"}.get(kind, kind)
        out += [f"## Label states — {noun}s", "",
                "Which labels an open " + noun + " carries in each state. Only these state labels "
                "count: " + ", ".join(f"`{l}`" for l in cfg.get("state_labels") or []) + ". Everything "
                "else (dept, priority, verdict, channel, risk) is metadata. **Any combination not "
                "listed is illegal** — the stuck sweeper repairs it if a rule below covers it, "
                "otherwise escalates it with `needs-human`.", "",
                "| State labels | Flow state | Meaning |", "|---|---|---|"]
        for c in cfg.get("combos") or []:
            out.append(f"| {_codes(c['labels'])} | `{c['state']}` | {c.get('meaning', '')} |")
        excl = cfg.get("exclusive") or []
        if excl:
            out += ["", "**Exclusive** — metadata labels of which at most one may be present:", ""]
            out += ["- " + " / ".join(f"`{l}`" for l in g) for g in excl]
        mods = cfg.get("modifiers") or []
        if mods:
            out += ["", "**Modifiers** — ride along on any state above without changing it:", ""]
            out += [f"- `{m['label']}` — {m.get('meaning', '')}" for m in mods]
        reps = cfg.get("repairs") or []
        if reps:
            out += ["", "**Automatic repairs** (first match wins; every group needs one label present):", "",
                    "| When | Unless | Action | Why |", "|---|---|---|---|"]
            for r in reps:
                when = " AND ".join("(" + " or ".join(f"`{l}`" for l in g) + ")" for g in r["groups"])
                unless = ", ".join(f"`{l}`" for l in r.get("unless") or []) or "—"
                if r.get("escalate"):
                    act = "escalate (`needs-human`)"
                else:
                    act = "; ".join(p for p in (
                        ("remove " + ", ".join(f"`{l}`" for l in r.get("remove") or [])) if r.get("remove") else "",
                        ("add " + ", ".join(f"`{l}`" for l in r.get("add") or [])) if r.get("add") else "") if p)
                out.append(f"| {when} | {unless} | {act} | {' '.join(str(r['reason']).split())} |")
        facts = cfg.get("fact_repairs") or []
        if facts:
            out += ["", "**Fact-dependent repairs** (which label wins depends on PR state; "
                    f"`squadhq.stuck_sweeper` normalize_{kind}):", ""]
            out += [f"- {f['when']}: {f['action']}" for f in facts]
        out.append("")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flows_path = argv[0] if argv else default_path()
    out_path = argv[1] if len(argv) > 1 else hub_path(OUT_DEFAULT)
    content = generate(flows_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(content)
    print(f"flow_diagram: wrote {out_path} ({len(content)} bytes, {len(load(flows_path).get('flows', {}))} flows)")


if __name__ == "__main__":
    main()
