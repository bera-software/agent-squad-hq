"""CLI: ``python -m squadhq.notify send --targets ... --title ... --body-file ...``.

``--targets`` is repeatable and accepts ``,`` or ``;`` separated lists; email
recipient lists keep their commas (``email:a@b.com,c@d.com``). ``--body-file -``
reads stdin. Exit status: 0 if at least one target succeeded, 1 if all failed,
2 on usage errors.
"""
from __future__ import annotations

import argparse
import sys

from squadhq.interfaces import Notification
from squadhq.notify import fan_out, parse_targets


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m squadhq.notify")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="send a notification to one or more targets")
    s.add_argument("--targets", action="append", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--body-file", required=True, help="markdown body file, or - for stdin")
    s.add_argument("--severity", choices=["info", "warning", "critical"], default="info")
    s.add_argument("--kind", choices=["escalation", "digest", "approval", "incident", "report"], default="report")
    s.add_argument("--url")
    s.add_argument("--dedupe-key")
    s.add_argument("--project")
    args = p.parse_args(argv)

    if args.body_file == "-":
        body = sys.stdin.read()
    else:
        with open(args.body_file, encoding="utf-8") as f:
            body = f.read()
    targets = parse_targets(args.targets)
    if not targets:
        print("no targets given", file=sys.stderr)
        return 2
    n = Notification(title=args.title, body_markdown=body, severity=args.severity, kind=args.kind,
                     url=args.url, dedupe_key=args.dedupe_key, project=args.project)
    results = fan_out(n, targets)
    for target, err in results.items():
        print(f"{target}: {'ok' if err is None else 'FAILED ' + err}")
    return 0 if any(e is None for e in results.values()) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
