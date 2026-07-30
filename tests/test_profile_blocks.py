from __future__ import annotations

import unittest

from ai_dev_flow.profile_blocks import (
    MARKER_BEGIN,
    MARKER_END,
    ProfileBlockError,
    remove_managed_block,
    upsert_managed_block,
)


class ProfileBlocksTests(unittest.TestCase):
    def test_insert_block_into_empty_profile(self) -> None:
        result = upsert_managed_block("", "source line")
        self.assertTrue(result.changed)
        self.assertEqual(
            result.text,
            f"{MARKER_BEGIN}\nsource line\n{MARKER_END}\n",
        )

    def test_replace_existing_block(self) -> None:
        original = f"prefix\n{MARKER_BEGIN}\nold\n{MARKER_END}\nsuffix\n"
        result = upsert_managed_block(original, "new")
        self.assertTrue(result.changed)
        self.assertEqual(
            result.text,
            f"prefix\n{MARKER_BEGIN}\nnew\n{MARKER_END}\nsuffix\n",
        )

    def test_remove_block_beginning_preserves_suffix_bytes(self) -> None:
        original = f"{MARKER_BEGIN}\nline\n{MARKER_END}\nsuffix\n"
        result = remove_managed_block(original)
        self.assertTrue(result.changed)
        self.assertEqual(result.text, "suffix\n")

    def test_remove_block_middle_preserves_prefix_suffix_bytes(self) -> None:
        original = f"abc\n\n{MARKER_BEGIN}\nline\n{MARKER_END}\nxyz\n"
        result = remove_managed_block(original)
        self.assertTrue(result.changed)
        self.assertEqual(result.text, "abc\nxyz\n")

    def test_remove_block_end_restores_no_final_newline(self) -> None:
        before = "prefix-without-newline"
        inserted = upsert_managed_block(before, "line")
        removed = remove_managed_block(inserted.text)
        self.assertEqual(removed.text, before)

    def test_remove_block_preserves_adjacent_blank_lines(self) -> None:
        original = f"a\n\n\n{MARKER_BEGIN}\nline\n{MARKER_END}\n\n\nb\n"
        result = remove_managed_block(original)
        self.assertEqual(result.text, "a\n\n\n\nb\n")

    def test_idempotent_upsert(self) -> None:
        first = upsert_managed_block("abc", "line")
        second = upsert_managed_block(first.text, "line")
        self.assertFalse(second.changed)
        self.assertEqual(second.text, first.text)

    def test_no_block_remove_is_noop(self) -> None:
        profile = "abc\n\n"
        result = remove_managed_block(profile)
        self.assertFalse(result.changed)
        self.assertEqual(result.text, profile)

    def test_duplicate_markers_fail(self) -> None:
        malformed = f"{MARKER_BEGIN}\n1\n{MARKER_END}\n{MARKER_BEGIN}\n2\n{MARKER_END}\n"
        with self.assertRaises(ProfileBlockError):
            upsert_managed_block(malformed, "line")


if __name__ == "__main__":
    unittest.main()
