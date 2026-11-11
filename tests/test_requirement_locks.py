"""Each requirements/*.in pin is what its hashed .lock installs. Dependabot edits only the
.in file, so without this a version bump merges green and changes nothing."""
import glob
import os
import re

import pytest

from squadhq.paths import _SOURCE_ROOT as ROOT

PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\;]+)", re.M)
SOURCES = sorted(glob.glob(os.path.join(ROOT, "requirements", "*.in")))


def _pins(path):
    with open(path, encoding="utf-8") as fh:
        return {name.lower().replace("_", "-"): version for name, version in PIN.findall(fh.read())}


def test_there_are_sources():
    assert SOURCES


@pytest.mark.parametrize("source", SOURCES, ids=os.path.basename)
def test_lock_installs_what_the_source_pins(source):
    lock = source[:-3] + ".lock"
    assert os.path.exists(lock), f"{os.path.basename(source)} has no .lock beside it"
    wanted, locked = _pins(source), _pins(lock)
    stale = {name: (version, locked.get(name)) for name, version in wanted.items() if locked.get(name) != version}
    assert not stale, (f"{os.path.basename(lock)} is behind {os.path.basename(source)}: {stale}. "
                       "Regenerate it with the command in its second line.")
