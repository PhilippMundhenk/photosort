"""The documentation is checked like code (issue #76): the development guide's test table lists
every test file and only existing ones; every configuration field has a form control and a line
in the configuration guide; every local link and image in the user documentation exists; the
README stays short; the design document names every module."""
from __future__ import annotations

import re
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parents[1]
USER_DOCS = ["README.md", "docs/USAGE.md", "docs/CONFIGURATION.md", "docs/DEPLOYMENT.md", "docs/DEVELOPMENT.md"]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_development_guide_lists_every_test_file_and_only_existing_ones():
    guide = _read("docs/DEVELOPMENT.md")
    listed = set()
    for m in re.finditer(r"`tests/((?:e2e/)?test_\{[^}]+\}|(?:e2e/)?test_\w+)\.py`", guide):
        name = m.group(1)
        if "{" in name:
            prefix, rest = name.split("{")
            listed.update(f"tests/{prefix}{x}.py" for x in rest.rstrip("}").split(","))
        else:
            listed.add(f"tests/{name}.py")
    actual = {str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "tests").rglob("test_*.py")}
    missing, stale = sorted(actual - listed), sorted(listed - actual)
    assert listed == actual, f"docs/DEVELOPMENT.md table vs files: missing {missing}, stale {stale}"


def test_settings_form_offers_every_configuration_field():
    form = _read("app/templates/settings.html")
    fields = set(config.Config().as_dict())
    in_form = set(re.findall(r'name="([a-z_]+)"', form))
    assert fields <= in_form, f"fields without a form control: {sorted(fields - in_form)}"


def test_configuration_guide_documents_every_field():
    guide = _read("docs/CONFIGURATION.md")
    missing = sorted(k for k in config.Config().as_dict() if f"`{k}`" not in guide)
    assert not missing, f"docs/CONFIGURATION.md does not document: {missing}"


def test_links_and_images_in_the_user_documentation_exist():
    for rel in USER_DOCS:
        text = _read(rel)
        base = (ROOT / rel).parent
        targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text) + re.findall(r'<img src="([^"]+)"', text)
        for target in targets:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (base / target).exists(), f"{rel} links to {target}, which does not exist"


def test_readme_stays_short_and_points_at_the_issues():
    readme = _read("README.md")
    assert "issues" in readme.lower() and len(readme.splitlines()) <= 120
    for ref in re.findall(r"#(\d+)", readme):
        assert 1 <= int(ref) <= 999, ref                      # issue numbers, not stray hashes


def test_design_document_covers_every_module_and_decision_section():
    design = _read("docs/DESIGN.md")
    for module in (p.stem for p in (ROOT / "app").glob("*.py") if p.stem != "__init__"):
        assert module in design, f"docs/DESIGN.md does not mention app/{module}.py"
    for heading in ("Purpose", "rules", "State", "Triggers", "Review UI", "Hardware", "Testing policy", "Open points",
                    "decision model"):
        assert heading in design, heading
