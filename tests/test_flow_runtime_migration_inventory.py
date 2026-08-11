from __future__ import annotations

from pathlib import Path
import unittest

from ai_dev_flow import cli


class FlowRuntimeMigrationInventoryTests(unittest.TestCase):
    def test_inventory_mentions_every_current_public_command(self) -> None:
        inventory_path = Path(__file__).resolve().parents[1] / "docs" / "flow-runtime-migration-inventory.md"
        text = inventory_path.read_text(encoding="utf-8")

        expected_names = {
            "ai-dev",
            *cli.FIXED_FLOW_EXECUTABLE_COMMANDS,
        }

        missing = sorted(name for name in expected_names if f"| {name} |" not in text)
        self.assertEqual(
            missing,
            [],
            msg=(
                "Inventory is missing current public command rows: "
                + ", ".join(missing)
            ),
        )


if __name__ == "__main__":
    unittest.main()