from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.alias_config import AliasConfigError, load_desired_alias_state


class AliasConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.config_path = self.tmp_path / "config.yaml"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_loads_sorted_aliases(self) -> None:
        self.config_path.write_text(
            "aliases:\n  zed: status\n  alpha: start\n",
            encoding="utf-8",
        )

        state = load_desired_alias_state(self.config_path, case_insensitive_names=False)

        self.assertEqual(state.aliases, {"alpha": "start", "zed": "status"})

    def test_invalid_alias_name_rejected(self) -> None:
        self.config_path.write_text(
            "aliases:\n  bad/name: start\n",
            encoding="utf-8",
        )

        with self.assertRaises(AliasConfigError):
            load_desired_alias_state(self.config_path, case_insensitive_names=False)

    def test_invalid_alias_target_rejected(self) -> None:
        self.config_path.write_text(
            "aliases:\n  gs: status now\n",
            encoding="utf-8",
        )

        with self.assertRaises(AliasConfigError):
            load_desired_alias_state(self.config_path, case_insensitive_names=False)

    def test_portable_alias_name_with_underscore_is_valid(self) -> None:
        self.config_path.write_text(
            "aliases:\n  review_short: review\n",
            encoding="utf-8",
        )

        state = load_desired_alias_state(self.config_path, case_insensitive_names=False)
        self.assertEqual(state.aliases, {"review_short": "review"})

    def test_hyphenated_alias_name_is_rejected(self) -> None:
        self.config_path.write_text(
            "aliases:\n  review-short: review\n",
            encoding="utf-8",
        )

        with self.assertRaises(AliasConfigError):
            load_desired_alias_state(self.config_path, case_insensitive_names=False)

    def test_case_insensitive_duplicate_rejected(self) -> None:
        self.config_path.write_text(
            "aliases:\n  GS: status\n  gs: status\n",
            encoding="utf-8",
        )

        with self.assertRaises(AliasConfigError):
            load_desired_alias_state(self.config_path, case_insensitive_names=True)


if __name__ == "__main__":
    unittest.main()
