from __future__ import annotations

from pathlib import Path
import re
import unittest


# The catalog is derivative discovery metadata, so the tree is the source of truth
# and these tests only prove the catalog still describes it. Audience directories
# are the ones that hold packages; every other direct child of skills/ is shared.
_AUDIENCE_SECTIONS = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "copilot": "Copilot",
}
_SHARED_SECTION = "Shared skills"

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|.*\|\s*`([^`]+)`\s*\|\s*$")


def _catalog_sections(catalog_path: Path) -> dict[str, set[tuple[str, str]]]:
    """Map each `##` heading to the (skill, canonical path) rows beneath it."""
    sections: dict[str, set[tuple[str, str]]] = {}
    current: str | None = None
    for line in catalog_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, set())
            continue
        if current is None:
            continue
        match = _ROW.match(line)
        if match:
            sections[current].add((match.group(1), match.group(2)))
    return sections


def _packages_on_disk(skills_root: Path) -> dict[str, set[tuple[str, str]]]:
    """Map each expected catalog section to the packages that actually exist."""
    expected: dict[str, set[tuple[str, str]]] = {
        _SHARED_SECTION: set(),
        **{section: set() for section in _AUDIENCE_SECTIONS.values()},
    }
    for skill_file in skills_root.rglob("SKILL.md"):
        relative = skill_file.relative_to(skills_root).parent.parts
        if len(relative) == 1:
            expected[_SHARED_SECTION].add(
                (relative[0], f"skills/{relative[0]}/SKILL.md")
            )
        elif len(relative) == 2 and relative[0] in _AUDIENCE_SECTIONS:
            audience, name = relative
            expected[_AUDIENCE_SECTIONS[audience]].add(
                (name, f"skills/{audience}/{name}/SKILL.md")
            )
    return expected


class SkillCatalogAudienceCoverageTests(unittest.TestCase):
    """`skills/index.md` must keep describing the audience tree it catalogs.

    Issue #56 added a Claude audience and moved `executor` to the shared root
    without updating the catalog, so it advertised neither Claude package and
    listed a shared skill under Copilot. Nothing caught it, because no test tied
    the catalog to the tree.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.skills_root = self.repo_root / "skills"
        self.catalog_path = self.skills_root / "index.md"
        self.sections = _catalog_sections(self.catalog_path)
        self.expected = _packages_on_disk(self.skills_root)

    def test_every_audience_has_its_catalog_section(self) -> None:
        for section, packages in self.expected.items():
            if not packages:
                continue
            with self.subTest(section=section):
                self.assertIn(
                    section,
                    self.sections,
                    f"skills/index.md has no '## {section}' section, so its packages are undiscoverable.",
                )

    def test_every_package_on_disk_is_catalogued_in_its_own_section(self) -> None:
        for section, packages in self.expected.items():
            listed = self.sections.get(section, set())
            for package in sorted(packages):
                with self.subTest(section=section, skill=package[0]):
                    self.assertIn(
                        package,
                        listed,
                        f"{package[1]} exists but is not listed under '## {section}'.",
                    )

    def test_no_package_is_catalogued_outside_its_own_section(self) -> None:
        for section in self.expected:
            owned = self.expected[section]
            for skill, path in sorted(self.sections.get(section, set())):
                with self.subTest(section=section, skill=skill):
                    self.assertIn(
                        (skill, path),
                        owned,
                        f"'## {section}' lists {path}, which does not belong to that audience.",
                    )

    def test_every_catalogued_path_exists(self) -> None:
        # Project-local rows point at other repositories, so only the audience and
        # shared sections are checked against this tree.
        for section in list(self.expected):
            for skill, path in sorted(self.sections.get(section, set())):
                with self.subTest(section=section, skill=skill):
                    self.assertTrue(
                        (self.repo_root / path).is_file(),
                        f"'## {section}' lists {path}, which does not exist.",
                    )


if __name__ == "__main__":
    unittest.main()
