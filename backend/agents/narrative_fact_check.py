"""Deterministic fact check for LLM-written match narratives.

The LLM is told to use only the stats in its prompt. This checks that it
did: every number in the narrative (digits, or a number word such as
"three") must match a number the prompt contained, either exactly or as
that number rounded. A narrative that cites a number the data does not
support is rejected, and the caller falls back to the template narrative,
which is built from the computed stats directly.

It checks numbers only. A sentence with correct numbers but a wrong claim
(the wrong team winning, say) still passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")

# "one" is left out on purpose: it is used too often in ordinary prose
# ("one of the", "one-sided") to be read as a stat.
_NUMBER_WORDS = {
    "zero": 0, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)


@dataclass
class FactCheckResult:
    passed: bool
    checked: int
    unsupported: list[str] = field(default_factory=list)


def numbers_in(text: str) -> list[tuple[str, float]]:
    """Every number in the text, as (how it was written, its value)."""
    found = [(m.group(0), float(m.group(0))) for m in _NUMBER.finditer(text)]
    found += [(m.group(0), float(_NUMBER_WORDS[m.group(0).lower()])) for m in _WORD.finditer(text)]
    return found


def _supported(value: float, allowed: set[float]) -> bool:
    for a in allowed:
        if abs(value - a) < 1e-9 or value == round(a) or value == round(a, 1):
            return True
    return False


def check_narrative(narrative: str, source_text: str) -> FactCheckResult:
    """Check every number in the narrative against the numbers in the text
    the model was given."""
    allowed = {value for _, value in numbers_in(source_text)}
    cited = numbers_in(narrative)
    unsupported = [written for written, value in cited if not _supported(value, allowed)]
    return FactCheckResult(passed=not unsupported, checked=len(cited), unsupported=unsupported)
