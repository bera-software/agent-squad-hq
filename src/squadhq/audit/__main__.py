"""``python -m squadhq.audit verify|export``.

* ``verify [--db URL | --file events.jsonl]`` exits 0 when the chain is intact, 2 when broken.
* ``export --db URL [--since SEQ] [--out FILE]`` writes JSONL (stdout by default).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from squadhq.audit.chain import export_jsonl, read_jsonl, verify_chain


def _db_url(arg: str | None) -> str:
    return arg or os.environ.get("SQUADHQ_DB_URL") or "sqlite:///squadhq.db"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m squadhq.audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify the hash chain")
    v.add_argument("--db", help="store URL (default $SQUADHQ_DB_URL)")
    v.add_argument("--file", help="verify an exported JSONL file instead of the database")
    e = sub.add_parser("export", help="export events as JSONL")
    e.add_argument("--db")
    e.add_argument("--since", type=int, default=0, help="only events with seq > SINCE")
    e.add_argument("--out", help="output file (default stdout)")
    args = ap.parse_args(argv)

    if args.cmd == "verify":
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                result = verify_chain(read_jsonl(fh))
        else:
            from squadhq.store import open_store

            store = open_store(_db_url(args.db))
            try:
                result = verify_chain(store.iter_audit())
            finally:
                store.close()
        print(json.dumps(result.to_dict()))
        return 0 if result.ok else 2

    from squadhq.store import open_store

    store = open_store(_db_url(args.db))
    try:
        events = store.iter_audit(since_seq=args.since)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                n = export_jsonl(events, fh)
        else:
            n = export_jsonl(events, sys.stdout)
    finally:
        store.close()
    print(f"exported {n} events", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
