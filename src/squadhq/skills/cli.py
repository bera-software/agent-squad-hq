"""``python -m squadhq.skills`` command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from squadhq.skills import catalogue, enabled, loader, render
from squadhq.skills.schema import PHASES, TIERS, trigger_summary


def _root(args: argparse.Namespace) -> str:
    return os.path.abspath(args.root or loader.skills_root())


def _hub(args: argparse.Namespace) -> str:
    return os.path.dirname(_root(args))


def cmd_validate(args: argparse.Namespace) -> int:
    report = loader.validate_catalogue(_root(args))
    if args.json:
        print(json.dumps({"skills": report.skills, "eval_cases": report.eval_cases,
                          "errors": report.errors, "warnings": report.warnings}, indent=2))
    else:
        for e in report.errors:
            print(f"ERROR   {e}")
        for w in report.warnings:
            print(f"WARNING {w}")
        status = "OK" if report.ok(args.strict) else "FAILED"
        print(f"{status}: {report.skills} skills, {report.eval_cases} eval cases, "
              f"{len(report.errors)} errors, {len(report.warnings)} warnings")
    return 0 if report.ok(args.strict) else 1


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    out += [fmt.format(*r) for r in rows]
    return "\n".join(line.rstrip() for line in out)


def cmd_list(args: argparse.Namespace) -> int:
    skills = loader.load_catalogue(_root(args))
    selected = {sid: s for sid, s in skills.items()
                if (not args.tier or s.tier == args.tier) and (not args.phase or s.phase == args.phase)}
    resolutions: dict[str, enabled.Resolution] | None = None
    if args.enabled_for:
        cfg = enabled.load_config(args.enabled_for)
        resolutions = enabled.resolve(skills, cfg, project=args.project)
    records: list[dict[str, Any]] = []
    for sid, s in sorted(selected.items(), key=lambda kv: (PHASES.index(kv[1].phase), kv[0])):
        rec = s.to_dict()
        if resolutions is not None:
            res = resolutions[sid]
            rec["enabled"] = res.enabled
            rec["enabled_reason"] = res.reason
            rec["missing_requires"] = res.missing_requires
        else:
            rec["enabled"] = s.default_enabled
        if args.only_enabled and not rec["enabled"]:
            continue
        records.append(rec)
    if args.format == "json":
        print(json.dumps(records, indent=2))
        return 0
    headers = ["id", "phase", "tier", "enabled", "tools", "model", "trigger"]
    rows = []
    for r in records:
        flag = "yes" if r["enabled"] else "no"
        if r.get("missing_requires"):
            flag += " (needs " + ",".join(r["missing_requires"]) + ")"
        rows.append([r["id"], r["phase"], r["tier"], flag, r["tool_profile"], r["model_tier"],
                     "; ".join(r["triggers"])])
    print(_table(rows, headers))
    print(f"\n{len(records)} skills")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    skill = loader.get_skill(args.skill_id, _root(args))
    if skill.deterministic:
        print(f"{skill.id} is deterministic ({skill.entrypoint}); it has no prompt", file=sys.stderr)
        return 2
    context = render.load_context_file(args.context_file) if args.context_file else {}
    rendered = render.render_prompt(skill, context, hub=_hub(args))
    if args.json:
        print(json.dumps({"skill": skill.id, "system": rendered.system, "user": rendered.user}, indent=2))
    else:
        sys.stdout.write(rendered.as_text())
    return 0


def cmd_catalogue(args: argparse.Namespace) -> int:
    skills = loader.load_catalogue(_root(args))
    text = catalogue.render_markdown(skills)
    output = args.output or os.path.join(_hub(args), "docs", "skills", "CATALOGUE.md")
    if args.check:
        try:
            with open(output, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        if current != text:
            print(f"{output} is out of date; run python -m squadhq.skills catalogue-md", file=sys.stderr)
            return 1
        print(f"{output} is up to date")
        return 0
    if output == "-":
        sys.stdout.write(text)
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {output} ({len(skills)} skills)")
    return 0


def build_parser(prog: str = "python -m squadhq.skills") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Skill catalogue tools")
    p.add_argument("--root", help="skills directory (default: <hub>/skills)")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate every skill manifest, prompt and eval file")
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_validate)

    ls = sub.add_parser("list", help="list skills")
    ls.add_argument("--tier", choices=TIERS)
    ls.add_argument("--phase", choices=PHASES)
    ls.add_argument("--enabled-for", metavar="SQUADHQ_YML", help="resolve enablement against this config")
    ls.add_argument("--project", help="apply this project's overrides (with --enabled-for)")
    ls.add_argument("--only-enabled", action="store_true")
    ls.add_argument("--format", choices=("table", "json"), default="table")
    ls.set_defaults(func=cmd_list)

    r = sub.add_parser("render-prompt", help="assemble the full prompt for a skill")
    r.add_argument("skill_id")
    r.add_argument("--context-file", help="JSON/YAML context (an eval case file works too)")
    r.add_argument("--json", action="store_true", help="print {system, user} as JSON")
    r.set_defaults(func=cmd_render)

    c = sub.add_parser("catalogue-md", help="generate docs/skills/CATALOGUE.md")
    c.add_argument("--output", help="output path, or - for stdout")
    c.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    c.set_defaults(func=cmd_catalogue)
    return p


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = (build_parser(prog) if prog else build_parser()).parse_args(argv)
    try:
        return int(args.func(args))
    except (loader.SkillError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = ["main", "build_parser", "trigger_summary"]
