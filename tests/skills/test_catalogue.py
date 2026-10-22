import os

from squadhq.skills import catalogue, loader
from tests.skills.helpers import make_hub, write_skill


def test_render_markdown(tmp_path):
    hub = make_hub(str(tmp_path))
    write_skill(hub, "alpha", legacy=True, summary="Pipe | in summary.",
                outputs=[{"type": "artifact", "path": "docs/x/{issue}.md"},
                         {"type": "label", "labels": ["needs-human"]}, {"type": "comment"}],
                gate={"check": "squadhq/x", "blocking_label": "needs-design", "verdict": "blocking: yes",
                      "condition": "c"},
                trigger=[{"label": "needs-design"}, {"command": "alpha"}])
    write_skill(hub, "beta", phase="verify", tier="pro", default_enabled=False, requires=["alpha"])
    md = catalogue.render_markdown(loader.load_catalogue(os.path.join(hub, "skills")))
    assert md.startswith("# Skill catalogue")
    assert "2 skills: 1 core, 1 pro, 0 enterprise. 1 existed before the catalogue" in md
    assert "**Alpha** ★<br>`alpha`" in md
    assert "`docs/x/{issue}.md`, label `needs-human`, comment" in md
    assert "check `squadhq/x`; blocks on `needs-design`; verdict `blocking: yes`" in md
    assert "label needs-design<br>command @squad alpha" in md
    assert "Pipe \\| in summary." in md
    assert "Requires: `alpha`." in md
    assert "## Verify and quality" in md and "## Build" not in md
