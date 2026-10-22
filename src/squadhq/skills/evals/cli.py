"""``python -m squadhq.skills.evals`` command line."""
from __future__ import annotations

import argparse
import json
import os
import sys

from squadhq import paths
from squadhq.skills.evals import harness
from squadhq.skills.evals.cases import CaseError


def _hub(args: argparse.Namespace) -> str:
    return os.path.abspath(args.hub or paths.hub_root())


def cmd_run(args: argparse.Namespace) -> int:
    hub = _hub(args)
    provider_ref = args.provider_ref or os.environ.get("SQUADHQ_EVAL_PROVIDER_REF") or harness.DEFAULT_PROVIDER_REF
    light_model = None
    if args.light_provider_ref:
        light_name, light_model = harness.parse_provider_ref(args.light_provider_ref)
        if light_name != harness.parse_provider_ref(provider_ref)[0]:
            print("error: --light-provider-ref must use the same provider as --provider-ref", file=sys.stderr)
            return 2
    if args.dry_run:
        cases = harness.collect_cases(hub=hub, suite=args.suite, skill_id=args.skill, kind=args.kind)
        provider: harness.Completer = harness.FakeProvider(
            {c.id: c.reference_response for c in cases if c.reference_response})
    else:
        name, _ = harness.parse_provider_ref(provider_ref)
        provider = harness.load_provider(name)
    summary = harness.run(provider=provider, provider_ref=provider_ref, hub=hub, suite=args.suite,
                          skill_id=args.skill, kind=args.kind, dry_run=args.dry_run, light_model=light_model,
                          max_tokens=args.max_tokens, limit=args.limit)
    thresholds = harness.load_thresholds(args.thresholds or os.path.join(hub, "evals", "thresholds.yml"))
    baseline = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as fh:
            baseline = json.load(fh)
    summary.gate_failures = harness.apply_thresholds(summary, thresholds, baseline)
    if not summary.scored:
        summary.gate_failures.append("no cases were scored")
    if args.no_write:
        sys.stdout.write(harness.markdown_summary(summary))
    else:
        json_path, md_path = harness.write_results(summary, args.out_dir or os.path.join(hub, "evals", "results"))
        sys.stdout.write(harness.markdown_summary(summary))
        print(f"\nwrote {json_path}\nwrote {md_path}")
    return 1 if summary.gate_failures else 0


def cmd_list(args: argparse.Namespace) -> int:
    cases = harness.collect_cases(hub=_hub(args), suite=args.suite, skill_id=args.skill, kind=args.kind)
    for c in cases:
        ref = "ref" if c.reference_response else "   "
        print(f"{c.kind:<9} {ref} {c.skill:<28} {c.id}")
    print(f"\n{len(cases)} cases")
    return 0


def build_parser(prog: str = "python -m squadhq.skills.evals") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Agent eval harness")
    p.add_argument("--hub", help="hub root (default: squadhq.paths.hub_root())")
    sub = p.add_subparsers(dest="command", required=True)

    def filters(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--skill", help="only cases for this skill id")
        sp.add_argument("--suite", choices=("all", "skills", "golden"), default="all")
        sp.add_argument("--kind", choices=("standard", "injection"))

    r = sub.add_parser("run", help="run evals and score them")
    filters(r)
    r.add_argument("--provider-ref", help=f"provider/model (default {harness.DEFAULT_PROVIDER_REF}, "
                                          "or $SQUADHQ_EVAL_PROVIDER_REF)")
    r.add_argument("--light-provider-ref", help="provider/model for model_tier: light skills")
    r.add_argument("--dry-run", action="store_true", help="no model calls: score reference responses only")
    r.add_argument("--thresholds", help="thresholds file (default evals/thresholds.yml)")
    r.add_argument("--baseline", help="previous results JSON to compare pass rate against")
    r.add_argument("--out-dir", help="results directory (default evals/results)")
    r.add_argument("--no-write", action="store_true", help="print the summary without writing files")
    r.add_argument("--max-tokens", type=int, default=harness.DEFAULT_MAX_TOKENS)
    r.add_argument("--limit", type=int, help="run at most N cases")
    r.set_defaults(func=cmd_run)

    ls = sub.add_parser("list", help="list eval cases")
    filters(ls)
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = (build_parser(prog) if prog else build_parser()).parse_args(argv)
    try:
        return int(args.func(args))
    except (CaseError, ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
