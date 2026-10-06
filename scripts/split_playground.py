"""Playground for a markdown-aware, lossless Discord message splitter.

Run the built-in adversarial suite:
    python scripts/split_playground.py --selftest

Split your own text (file or stdin) and inspect every chunk:
    python scripts/split_playground.py my_message.txt
    python scripts/split_playground.py my_message.txt --limit 500
    type my_message.txt | python scripts/split_playground.py -

Design rules being tested:
  1. Never split a word. Break preference: paragraph > line > space > hard cut.
  2. If a break lands inside a ``` code fence, close it and reopen (with the
     language tag) on the next chunk.
  3. Unclosed inline markers (**, *, `, ~~, ||) are closed at the end of a
     chunk and reopened at the start of the next one.
  4. LOSSLESS: removing the synthetic closers/reopeners and concatenating the
     chunks must reproduce the input exactly.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

DEFAULT_LIMIT = 1_900

_FENCE = "```"


@dataclass
class _MdState:
    """Markdown state at a given position in the ORIGINAL text."""

    in_fence: bool = False
    fence_lang: str = ""
    inline_stack: list[str] = field(default_factory=list)  # open inline markers


def _scan_state(text: str) -> _MdState:
    """Walk text and return the markdown state at the end of it.

    Handles backslash escapes. Fences are line-oriented (``` toggles),
    everything else is a simple toggle stack (** before *, so bold wins).
    """
    state = _MdState()
    i, n = 0, len(text)
    at_line_start = True
    while i < n:
        ch = text[i]
        if ch == "\\":  # escaped char: skip both
            i += 2
            at_line_start = False
            continue
        if at_line_start and text.startswith(_FENCE, i):
            if not state.in_fence:
                # opening fence: capture language tag up to end of line
                eol = text.find("\n", i)
                eol = n if eol == -1 else eol
                state.in_fence = True
                state.fence_lang = text[i + 3 : eol].strip()
                state.inline_stack.clear()  # nothing inline inside a fence
                i = eol
                continue
            else:
                state.in_fence = False
                state.fence_lang = ""
                i += 3
                at_line_start = False
                continue
        if ch == "\n":
            at_line_start = True
            i += 1
            continue
        at_line_start = False
        if state.in_fence:
            i += 1
            continue
        # inline markers (check longest first)
        for marker in ("**", "~~", "||", "*", "`", "_"):
            if text.startswith(marker, i):
                if state.inline_stack and state.inline_stack[-1] == marker:
                    state.inline_stack.pop()
                else:
                    state.inline_stack.append(marker)
                i += len(marker)
                break
        else:
            i += 1
    return state


def _wrappers(state: _MdState) -> tuple[str, str]:
    """(suffix to close chunk, prefix to reopen next chunk) for a state."""
    suffix = "".join(reversed(state.inline_stack))
    prefix = "".join(state.inline_stack)
    if state.in_fence:
        suffix += "\n" + _FENCE
        prefix = _FENCE + state.fence_lang + "\n" + prefix
    return suffix, prefix


@dataclass
class Chunk:
    text: str           # what Discord would receive
    added_prefix: str   # synthetic reopeners (not part of the original)
    added_suffix: str   # synthetic closers


def _find_break(window: str, min_len: int = 1) -> int:
    """Best split index within window. 0 means 'no good break found'.

    Breaks yielding a piece shorter than min_len are rejected, so we don't
    emit tiny chunks (e.g. a lone ``` fence opener) when a later separator
    or a hard cut would fill the message better.
    """
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep, 1)  # don't break at 0
        keep = len(sep) if sep != " " else 1  # separator stays with prev chunk
        if idx > 0 and idx + keep >= min_len:
            return idx + keep
    return 0


def chunks_with_meta(text: str, limit: int = DEFAULT_LIMIT) -> list[Chunk]:
    content = text.strip() or "(No text response.)"
    out: list[Chunk] = []
    pos = 0
    pending_prefix = ""
    while pos < len(content):
        remaining = content[pos:]
        take = min(max(limit - len(pending_prefix), 1), len(remaining))

        while True:
            window = remaining[:take]
            min_len = max(len(window) // 4, 1)
            piece = window
            if len(remaining) > take:
                brk = _find_break(window, min_len)
                if brk > 0:
                    piece = remaining[:brk]

            state_start = _scan_state(content[:pos])
            state_end = _scan_state(content[: pos + len(piece)])

            # Prefer not to open a fence at the very end of a chunk — but only
            # when breaking before the fence still gives a decent-sized piece.
            # Otherwise the close/reopen wrappers handle it fine.
            if state_end.in_fence and not state_start.in_fence:
                cut = piece.rfind("\n```")
                if cut >= min_len:
                    piece = piece[:cut]
                    state_end = _scan_state(content[: pos + len(piece)])

            if not piece:  # safety net: never emit an empty message
                piece = remaining[:1]
                state_end = _scan_state(content[: pos + len(piece)])

            suffix, next_prefix = _wrappers(state_end)

            if len(pending_prefix) + len(piece) + len(suffix) <= limit:
                break  # fits — done
            if len(piece) <= 1:
                # pathological limit (wrappers alone exceed it): emit raw
                # text with no wrappers at all, but always PROGRESS
                piece = remaining[:1]
                suffix = next_prefix = ""
                pending_prefix = ""
                break
            # shrink and re-evaluate (break choice + state may both change);
            # clamp to >= 1: a negative slice would wrap and never converge
            take = max(len(piece) - (len(pending_prefix) + len(piece) + len(suffix) - limit), 1)

        out.append(Chunk(pending_prefix + piece + suffix, pending_prefix, suffix))
        pending_prefix = next_prefix
        pos += len(piece)  # piece is always non-empty — no infinite loop
    return out


def smart_chunks(text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    return [c.text for c in chunks_with_meta(text, limit)]


def verify_lossless(original: str, limit: int = DEFAULT_LIMIT) -> bool:
    content = original.strip() or "(No text response.)"
    rebuilt = "".join(
        c.text[len(c.added_prefix) : len(c.text) - len(c.added_suffix) if c.added_suffix else None]
        for c in chunks_with_meta(original, limit)
    )
    ok = rebuilt == content
    fits = all(len(c.text) <= limit for c in chunks_with_meta(original, limit))
    return ok and fits


# ---------------------------------------------------------------- selftest --

def _selftest() -> int:
    long_word = "supercalifragilistic" * 200          # ~4000 chars, no spaces
    cases: dict[str, str] = {
        "the 'mean' bug": (
            "7. **Media multitasking / screen distraction**\n"
            "   - Work by **Sana, Weston, & Cepeda (2013)** and related studies.\n"
            + "   - " + "filler text to push the break. " * 55 + "\n"
            "   - This is often what people loosely mean when they say screen time is bad."
        ),
        "bold spanning a break": "**" + "bold words " * 300 + "**",
        "italic + bold nested": "start **bold and *italic both* still bold** " * 150,
        "code fence spanning break": (
            "Intro:\n```python\n" + "x = compute_something()\n" * 200 + "```\nAfter."
        ),
        "inline code spanning break": "Use `" + "some_func_name, " * 200 + "` here.",
        "spoiler spanning break": "||" + "secret " * 400 + "||",
        "one giant word": long_word,
        "empty string": "",
        "whitespace only": "   \n\n   ",
        "exactly at limit": "a" * DEFAULT_LIMIT,
        "limit plus one": "a" * (DEFAULT_LIMIT + 1),
        "escaped markers": r"\*\*not bold\*\* " * 300,
        "paragraph heavy": ("Paragraph one.\n\nParagraph two.\n\n" * 120),
    }
    failures = 0
    for name, text in cases.items():
        chunks = chunks_with_meta(text)
        ok = verify_lossless(text)
        status = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"[{status}] {name:<32} -> {len(chunks)} chunk(s), "
              f"lens {[len(c.text) for c in chunks]}")
        if not ok:
            for i, c in enumerate(chunks):
                print(f"  chunk {i}: {c.text[:80]!r} ... {c.text[-80:]!r}")
    print(f"\n{len(cases) - failures}/{len(cases)} round-trip passed")

    # -- behavioral assertions: WHERE the break lands, not just losslessness --
    behavioral_failures = 0

    # 1) a paragraph gap near the limit is taken, leaving the list intact
    para = " ".join(["wrapping"] * 12)
    text = "Key points:\n- alpha\n- beta\n- gamma\n\n" + para + "\n\n- delta\n- epsilon"
    chunks = smart_chunks(text, 80)
    ok = len(chunks) > 1 and "- alpha" in chunks[0] and "- gamma" in chunks[0]
    print(f"[{'PASS' if ok else 'FAIL'}] list avoided via paragraph break")
    behavioral_failures += 0 if ok else 1

    # 2) with no paragraph gap, every split lands BETWEEN list items
    text = "Intro line here.\n" + "\n".join(
        f"- item number {i} with some text" for i in range(1, 9)
    )
    chunks = smart_chunks(text, 80)
    # every continuation chunk must open with a complete list item, never mid-item
    ok = len(chunks) > 1 and all(c.startswith("- ") for c in chunks[1:])
    print(f"[{'PASS' if ok else 'FAIL'}] list split between items")
    behavioral_failures += 0 if ok else 1

    total = failures + behavioral_failures
    print(f"{len(cases) + 2 - total}/{len(cases) + 2} total passed")
    return 1 if total else 0


# -------------------------------------------------------------------- main --

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="text file to split ('-' for stdin)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--selftest", action="store_true", help="run the built-in suite")
    args = ap.parse_args()

    if args.selftest or not args.file:
        return _selftest()

    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    chunks = chunks_with_meta(text, args.limit)
    print(f"input: {len(text.strip())} chars -> {len(chunks)} chunk(s), limit {args.limit}")
    print(f"lossless round-trip: {'YES' if verify_lossless(text, args.limit) else 'NO'}")
    for i, c in enumerate(chunks):
        bar = "=" * 20
        print(f"\n{bar} chunk {i + 1}/{len(chunks)} ({len(c.text)} chars) {bar}")
        print(c.text)
        if c.added_prefix or c.added_suffix:
            print(f"  [synthetic] prefix={c.added_prefix!r} suffix={c.added_suffix!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
