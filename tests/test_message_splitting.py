"""Offline tests for markdown-aware, lossless message splitting (_chunks)."""

from __future__ import annotations

import random
import string
import unittest
from pathlib import Path

from discord_hub.bot import _MESSAGE_LIMIT, _chunks, _chunks_with_meta, _scan_state

FIXTURE = Path(__file__).parent / "fixtures" / "my_message.txt"


def assert_lossless(case: unittest.TestCase, text: str, limit: int) -> list[str]:
    meta = _chunks_with_meta(text, limit)
    chunks = [c for c, _, _ in meta]
    case.assertTrue(chunks and all(chunks), "no empty chunks allowed")
    case.assertTrue(
        all(len(c) <= limit for c in chunks),
        f"oversize: {[len(c) for c in chunks]} (limit {limit})",
    )
    rejoined = "".join(c[len(pre) : len(c) - len(suf) if suf else None] for c, pre, suf in meta)
    case.assertEqual(rejoined, text.strip() or "(No text response.)")
    case.assertEqual(chunks, _chunks(text, limit), "_chunks must wrap the meta variant")
    return chunks


class MessageSplittingTests(unittest.TestCase):
    def test_mean_bug_and_fixture(self):
        """The word-split that started this, plus the real-world fixture file."""
        text = (
            "7. **Media multitasking / screen distraction**\n"
            "   - Work by **Sana, Weston, & Cepeda (2013)** and related studies.\n"
            + "   - "
            + "filler text to push the break. " * 55
            + "\n"
            "   - This is often what people loosely mean when they say screen time is bad."
        )
        for c in assert_lossless(self, text, _MESSAGE_LIMIT):
            self.assertNotRegex(c, r"\bme$")  # never 'me' / 'an' again
        for limit in (50, _MESSAGE_LIMIT):
            assert_lossless(self, FIXTURE.read_text(encoding="utf-8"), limit)

    def test_markdown_closed_and_reopened_across_breaks(self):
        bold = assert_lossless(self, "**" + "bold words " * 300 + "**", _MESSAGE_LIMIT)
        self.assertTrue(bold[0].endswith("**") and bold[1].startswith("**"))
        fence = assert_lossless(
            self, "Intro:\n```python\n" + "x = f()\n" * 300 + "```\nAfter.", _MESSAGE_LIMIT
        )
        self.assertTrue(fence[0].endswith("\n```") and fence[1].startswith("```python\n"))
        spoiler = assert_lossless(self, "||" + "secret " * 400 + "||", _MESSAGE_LIMIT)
        self.assertTrue(spoiler[0].endswith("||") and spoiler[1].startswith("||"))

    def test_edge_cases(self):
        self.assertEqual(_chunks(""), ["(No text response.)"])
        self.assertEqual(_chunks("   \n\n  "), ["(No text response.)"])
        self.assertEqual(len(_chunks("a" * _MESSAGE_LIMIT)), 1)
        self.assertEqual(len(assert_lossless(self, "a" * (_MESSAGE_LIMIT + 1), _MESSAGE_LIMIT)), 2)
        giant = assert_lossless(self, "supercalifragilistic" * 200, _MESSAGE_LIMIT)
        self.assertEqual(len(giant[0]), _MESSAGE_LIMIT)  # hard-cut fallback fills

    def test_lists_split_sensibly(self):
        # paragraph gap near the limit: list stays intact in chunk 1
        text = (
            "Key points:\n- alpha\n- beta\n- gamma\n\n"
            + " ".join(["wrapping"] * 12)
            + "\n\n- delta\n- epsilon"
        )
        chunks = assert_lossless(self, text, 80)
        self.assertIn("- alpha", chunks[0])
        self.assertIn("- gamma", chunks[0])
        # no gap: every continuation chunk opens with a complete list item
        text = "Intro.\n" + "\n".join(f"- item number {i} with some text" for i in range(1, 9))
        for c in assert_lossless(self, text, 80)[1:]:
            self.assertTrue(c.startswith("- "), f"continuation starts mid-item: {c!r}")

    def test_captured_inline_code_phantom_suffix(self):
        text = (FIXTURE.parent / "dev_agent_phantom_suffix.txt").read_text(encoding="utf-8")
        self.assertEqual(len(text), 894)  # the exact dev-agent source captured on 2026-10-28
        self.assertEqual(_scan_state(text).inline_stack, [])
        self.assertEqual(_chunks_with_meta(text), [(text, "", "")])
        self.assertEqual(_chunks(text), [text])

    def test_inline_code_ignores_markdown_markers(self):
        cases = (
            "`test_excited_uppercases_and_adds_exclamation`",
            "`*_**~~||_snake_case_||~~**_*`",
            "**bold `snake_case_name` bold**",
            "``double_underscores_and_`single_backticks`_ignored``",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(_scan_state(text).inline_stack, [])
                self.assertEqual(_chunks_with_meta(text), [(text, "", "")])

    def test_inline_code_split_has_only_matching_code_wrappers(self):
        # No spaces: force a hard split through snake_case, not a convenient word boundary.
        text = "Before `" + "test_excited_uppercases_and_adds_exclamation" * 100 + "` after."
        meta = _chunks_with_meta(text, _MESSAGE_LIMIT)
        assert_lossless(self, text, _MESSAGE_LIMIT)
        self.assertGreater(len(meta), 1)
        for previous, following in zip(meta, meta[1:], strict=False):
            self.assertEqual(previous[2], "`")
            self.assertEqual(following[1], "`")
        self.assertEqual(meta[0][1], "")
        self.assertEqual(meta[-1][2], "")

    def test_escaped_markers_remain_escaped(self):
        text = r"\*literal\* and \`literal\` and \_literal\_"
        self.assertEqual(_scan_state(text).inline_stack, [])

    def test_randomized_stress(self):
        rng = random.Random(42)
        alphabet = string.ascii_letters + "     \n\n" + "*`_|~#"
        for _ in range(50):
            parts = [rng.choice(alphabet) for _ in range(rng.randint(0, 600))]
            for _ in range(rng.randint(0, 4)):
                parts.insert(
                    rng.randint(0, len(parts)), rng.choice(["```", "```python", "**", "||", "~~"])
                )
            limit = rng.choice([20, 50, 100, 500, 1900])
            with self.subTest(limit=limit):
                assert_lossless(self, "".join(parts), limit)


if __name__ == "__main__":
    unittest.main()
