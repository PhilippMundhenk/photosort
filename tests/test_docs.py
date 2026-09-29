"""The documentation is checked like code: every test file is listed in the README's table and
every listed file exists; every configuration field appears in the settings form; every issue
the README's roadmap points at is a real issue number; the design document names every module."""
from __future__ import annotations

import re
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parents[1]


def test_readme_lists_every_test_file_and_only_existing_ones():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    listed = set()
    for m in re.finditer(r"`tests/(test_\{[^}]+\}|[\w/]+)\.py`", readme):
        name = m.group(1)
        if "{" in name:
            prefix, rest = name.split("{")
            listed.update(f"tests/{prefix}{x}.py" for x in rest.rstrip("}").split(","))
        else:
            listed.add(f"tests/{name}.py")
    actual = {str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "tests").rglob("test_*.py")}
    missing, stale = sorted(actual - listed), sorted(listed - actual)
    assert listed == actual, f"README table vs files: missing {missing}, stale {stale}"


def test_settings_form_offers_every_configuration_field():
    form = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
    fields = set(config.Config().as_dict())
    in_form = set(re.findall(r'name="([a-z_]+)"', form))
    assert fields <= in_form, f"fields without a form control: {sorted(fields - in_form)}"


def test_roadmap_points_at_real_issues():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "issues" in readme.lower()
    for ref in re.findall(r"#(\d+)", readme):
        assert 1 <= int(ref) <= 50, ref                       # issue numbers, not stray hashes


def test_design_document_covers_every_module_and_decision_section():
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    for module in (p.stem for p in (ROOT / "app").glob("*.py") if p.stem != "__init__"):
        assert module in design, f"docs/DESIGN.md does not mention app/{module}.py"
    for heading in ("Purpose", "rules", "State", "Triggers", "Review UI", "Hardware", "Testing policy", "Open points",
                    "decision model"):
        assert heading in design, heading
