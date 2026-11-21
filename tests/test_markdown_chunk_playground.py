"""Offline tests for the chunking playground rename, underscore-flanking
scanner behavior, flare-up fixtures, and consumer docs.

The playground (scripts/discord_message_limit_chunk_playground.py) is a
standalone executable model of the splitter; production coverage lives in
tests/test_message_splitting.py against discord_hub.bot.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from discord_hub.bot import _MESSAGE_LIMIT, _chunks_with_meta, _scan_state

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYGROUND = REPO_ROOT / "scripts" / "discord_message_limit_chunk_playground.py"
OLD_PLAYGROUND = REPO_ROOT / "scripts" / "split_playground.py"
FLAREUPS_DIR = Path(__file__).parent / "fixtures" / "flareups"
INTEGRATING = REPO_ROOT / "docs" / "integrating.md"


def _load_playground():
    spec = importlib.util.spec_from_file_location(
        "discord_message_limit_chunk_playground", PLAYGROUND
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_clean_split(case: unittest.TestCase, scan_state, chunks_meta, text: str) -> None:
    """Shared flare-up assertion: lossless, in-limit, no phantom wrappers."""
    chunks = [c for c, _, _ in chunks_meta]
    case.assertTrue(chunks and all(chunks), "no empty chunks allowed")
    rejoined = "".join(
        c[len(pre) : len(c) - len(suf) if suf else None] for c, pre, suf in chunks_meta
    )
    case.assertEqual(rejoined, text.strip() or "(No text response.)")
    for _, prefix, suffix in chunks_meta:
        case.assertNotIn("_", prefix, f"phantom underscore reopened: {chunks_meta!r}")
        case.assertNotIn("_", suffix, f"phantom underscore closed: {chunks_meta!r}")
        # A split in plain prose must not invent emphasis wrappers at all.
        case.assertNotIn("*", prefix)
        case.assertNotIn("*", suffix)
    # The full text contains no real emphasis spans, so the scanner must
    # end with an empty inline stack.
    case.assertEqual(scan_state(text).inline_stack, [])


class PlaygroundRenameTests(unittest.TestCase):
    def test_playground_renamed_and_old_name_gone(self):
        self.assertTrue(PLAYGROUND.exists(), f"missing {PLAYGROUND}")
        self.assertFalse(OLD_PLAYGROUND.exists(), f"stale {OLD_PLAYGROUND}")

    def test_playground_selftest_discovers_flareups(self):
        result = subprocess.run(
            [sys.executable, str(PLAYGROUND), "--selftest"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (result.stdout + result.stderr).lower()
        self.assertIn("flareup", output, "--selftest must run fixtures/flareups cases")

    def test_playground_docstring_states_why_and_boundary(self):
        docstring = _load_playground().__doc__ or ""
        text = docstring.lower()
        # The why: Discord's message-length limit motivates the splitter.
        self.assertIn("message limit", text.replace("length limit", "message limit"))
        self.assertTrue("2000" in text or "2,000" in text or "1_900" in text)
        # The boundary: standalone model, not a test of production code.
        self.assertIn("standalone", text)
        self.assertIn("discord_hub.bot", docstring)
        self.assertIn("tests/test_message_splitting.py", docstring)


class UnderscoreFlankingTests(unittest.TestCase):
    """Discord does not treat intra-word underscores as emphasis."""

    def _check_scanner(self, scan_state):
        for text in (
            "snake_case_name",
            "see foo_bar.py for details",
            "use discord_message_limit_chunk_playground.py here",
            "a_b and c_d_e",
        ):
            self.assertEqual(scan_state(text).inline_stack, [], text)

    def test_intraword_underscore_is_literal_production(self):
        self._check_scanner(_scan_state)

    def test_intraword_underscore_is_literal_playground(self):
        self._check_scanner(_load_playground()._scan_state)

    def test_real_underscore_italic_still_toggles(self):
        for scan_state in (_scan_state, _load_playground()._scan_state):
            self.assertEqual(scan_state("_italic_").inline_stack, [])
            self.assertEqual(scan_state("_open italic").inline_stack, ["_"])

    def test_snake_case_split_has_no_phantom_underscore_wrappers(self):
        text = (
            "Discussion of discord_message_limit_chunk_playground.py and other "
            "snake_case identifiers like foo_bar and _MESSAGE_LIMIT. "
            + "filler text to push the break. " * 60
        )
        _assert_clean_split(self, _scan_state, _chunks_with_meta(text, 500), text)


class FlareupFixtureTests(unittest.TestCase):
    """Every saved flare-up message must split cleanly in production."""

    def test_flareups_exist(self):
        self.assertTrue(any(FLAREUPS_DIR.glob("*.txt")), f"no fixtures in {FLAREUPS_DIR}")

    def test_flareup_fixtures_split_without_phantom_wrappers(self):
        for fixture in sorted(FLAREUPS_DIR.glob("*.txt")):
            text = fixture.read_text(encoding="utf-8")
            with self.subTest(fixture=fixture.name):
                meta = _chunks_with_meta(text, _MESSAGE_LIMIT)
                self.assertGreater(len(meta), 1, "fixture must actually split")
                _assert_clean_split(self, _scan_state, meta, text)


class IntegratingDocTests(unittest.TestCase):
    def test_integrating_md_documents_markdown_aware_chunking(self):
        text = INTEGRATING.read_text(encoding="utf-8").lower()
        self.assertIn("markdown-aware", text)
        self.assertIn("inline code", text.replace("inline-code", "inline code"))
        self.assertIn("reopen", text)
        self.assertIn("tests/test_message_splitting.py", text)
        self.assertIn("snake_case", text)


if __name__ == "__main__":
    unittest.main()
