"""Post a comment, issue or PR whose body is a file the agent wrote.

Agents pass ``gh`` bodies inline, and their shell refuses ``$`` and backticks
(no expansion of secrets), so any body quoting code or prices could not be
posted. ``--body-file`` stays denied (it would read any local file, e.g.
``/proc/self/environ``, into a comment). This command is the one narrow way
round: the body must be a regular file inside ``.squadhq-out/`` of the
current checkout (where the agent's Write tool may create it), never a
symlink, at most 60 000 characters; it is handed to ``gh`` on stdin.

    python -P -m squadhq.post_body comment <number> --file .squadhq-out/note.md [--repo owner/name]
    python -P -m squadhq.post_body issue --title "..." --file .squadhq-out/issue.md [--label L ...] [--repo R]
    python -P -m squadhq.post_body pr --title "..." --head BRANCH [--base main] --file .squadhq-out/pr.md [--repo R]
    python -P -m squadhq.post_body edit-issue <number> --file .squadhq-out/body.md [--repo R]
"""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys

HANDOFF_DIR = ".squadhq-out"
MAX_CHARS = 60_000


class BodyError(ValueError):
    pass


def read_body(path: str, cwd: str | None = None) -> str:
    """The body text, only from a regular file directly or nested in ``.squadhq-out/``."""
    base = os.path.realpath(os.path.join(cwd or os.getcwd(), HANDOFF_DIR))
    full = os.path.join(cwd or os.getcwd(), path)
    if os.path.islink(full) or os.path.islink(base):
        raise BodyError("the body file may not be a symlink")
    real = os.path.realpath(full)
    if os.path.commonpath([real, base]) != base or real == base:
        raise BodyError(f"the body file must be inside {HANDOFF_DIR}/ (got {path!r})")
    try:
        st = os.lstat(real)
    except OSError as e:
        raise BodyError(f"cannot read {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise BodyError(f"{path} is not a regular file")
    with open(real, encoding="utf-8", errors="replace") as f:
        text = f.read(MAX_CHARS + 1)
    if len(text) > MAX_CHARS:
        raise BodyError(f"{path} is longer than {MAX_CHARS} characters")
    if not text.strip():
        raise BodyError(f"{path} is empty")
    return text


def gh_args(a: argparse.Namespace) -> list[str]:
    repo = ["-R", a.repo] if a.repo else []
    if a.cmd == "comment":
        return ["issue", "comment", str(a.number), *repo, "--body-file", "-"]
    if a.cmd == "edit-issue":
        return ["issue", "edit", str(a.number), *repo, "--body-file", "-"]
    if a.cmd == "issue":
        labels = [x for lab in a.label or [] for x in ("--label", lab)]
        return ["issue", "create", *repo, "--title", a.title, *labels, "--body-file", "-"]
    return ["pr", "create", *repo, "--title", a.title, "--head", a.head, "--base", a.base, "--body-file", "-"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -P -m squadhq.post_body", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("comment", "edit-issue"):
        p = sub.add_parser(name)
        p.add_argument("number", type=int)
    p = sub.add_parser("issue")
    p.add_argument("--title", required=True)
    p.add_argument("--label", action="append")
    p = sub.add_parser("pr")
    p.add_argument("--title", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--base", default="main")
    for p in sub.choices.values():
        p.add_argument("--file", required=True, help=f"a file inside {HANDOFF_DIR}/")
        p.add_argument("--repo", default=None)
    a = ap.parse_args(argv)
    try:
        body = read_body(a.file)
    except BodyError as e:
        print(f"post_body: {e}", file=sys.stderr)
        return 2
    r = subprocess.run(["gh", *gh_args(a)], input=body, text=True, capture_output=True, timeout=120)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
