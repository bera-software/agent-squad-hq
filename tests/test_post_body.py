"""squadhq.post_body: bodies only from regular files inside .squadhq-out/."""
import os

import pytest

from squadhq import post_body as pb


def test_reads_a_body_from_squadhq_out(tmp_path):
    (tmp_path / ".squadhq-out").mkdir()
    (tmp_path / ".squadhq-out" / "c.md").write_text("Use `next/font/local`; costs $7.")
    assert pb.read_body(".squadhq-out/c.md", str(tmp_path)).startswith("Use `next/font/local`")


@pytest.mark.parametrize("path", ["README.md", "../x.md", ".squadhq-out/../README.md", "/etc/passwd", ".squadhq-out"])
def test_refuses_anything_outside_squadhq_out(tmp_path, path):
    (tmp_path / ".squadhq-out").mkdir()
    (tmp_path / "README.md").write_text("x")
    with pytest.raises(pb.BodyError):
        pb.read_body(path, str(tmp_path))


def test_refuses_symlinks_and_empty_files(tmp_path):
    out = tmp_path / ".squadhq-out"
    out.mkdir()
    (tmp_path / "secret").write_text("token")
    os.symlink(tmp_path / "secret", out / "link.md")
    with pytest.raises(pb.BodyError):
        pb.read_body(".squadhq-out/link.md", str(tmp_path))
    (out / "empty.md").write_text("  ")
    with pytest.raises(pb.BodyError):
        pb.read_body(".squadhq-out/empty.md", str(tmp_path))


def test_gh_arguments_take_the_body_on_stdin():
    a = pb.main.__globals__["argparse"].Namespace(cmd="comment", number=5, repo="acme/web")
    assert pb.gh_args(a) == ["issue", "comment", "5", "-R", "acme/web", "--body-file", "-"]
    a = pb.main.__globals__["argparse"].Namespace(cmd="issue", repo=None, title="T", label=["dept:frontend"])
    assert pb.gh_args(a) == ["issue", "create", "--title", "T", "--label", "dept:frontend", "--body-file", "-"]
