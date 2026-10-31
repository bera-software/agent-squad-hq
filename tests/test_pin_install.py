"""squadhq.pin_install: exact versions only, inside the checkout, no install scripts."""
import pytest

from squadhq import pin_install as pi


@pytest.mark.parametrize("spec", ["next@16.2.11", "@types/node@22.15.3", "react-dom@19.0.0-rc.1"])
def test_exact_versions_pass(spec):
    assert pi.check_specs([spec]) == [spec]


@pytest.mark.parametrize("spec", ["next", "next@^16.2.0", "next@latest", "next@16", "--registry=http://x",
                                  "next@16.2.11 --foo", "git+https://x/y.git", "@types/node", "NEXT@1.0.0"])
def test_ranges_tags_urls_and_flags_are_refused(spec):
    with pytest.raises(pi.SpecError):
        pi.check_specs([spec])


def test_directory_must_be_inside_the_checkout_with_a_package_json(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    assert pi.project_dir("frontend", str(tmp_path)).endswith("frontend")
    for bad in ("../x", "/etc", "missing"):
        with pytest.raises(pi.SpecError):
            pi.project_dir(bad, str(tmp_path))


def test_the_project_package_manager_is_used_with_scripts_off(tmp_path):
    assert pi.command(str(tmp_path), ["a@1.0.0"], False) == ["npm", "install", "--save-exact", "--ignore-scripts", "a@1.0.0"]
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert pi.command(str(tmp_path), ["a@1.0.0"], True)[:5] == ["pnpm", "add", "--save-exact", "--ignore-scripts", "--save-dev"]
