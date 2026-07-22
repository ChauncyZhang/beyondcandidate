import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Literal


Quality = Literal["good", "poor", "empty"]

_MAX_INSPECTED_CHARS = 500_000
_MAX_INSPECTED_LINES = 2_000
_DATE = r"(?:19|20)\d{2}(?:[./-](?:0?[1-9]|1[0-2])|年(?:0?[1-9]|1[0-2])月?)"
_CONCATENATED_DATES = re.compile(rf"(?<!\d){_DATE}{_DATE}{_DATE}")


@dataclass(frozen=True)
class TextQualityAssessment:
    quality: Quality
    reasons: tuple[str, ...]
    metrics: dict[str, int | float]


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def assess_text_quality(text: str) -> TextQualityAssessment:
    """Classify extracted resume text without retaining or returning its content."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    sample = text[:_MAX_INSPECTED_CHARS]
    visible = [character for character in sample if not character.isspace()]
    visible_count = len(visible)
    metrics: dict[str, int | float] = {
        "char_count": min(len(text), _MAX_INSPECTED_CHARS),
        "visible_char_count": visible_count,
        "line_count": 0,
        "replacement_ratio": 0.0,
        "control_ratio": 0.0,
        "symbol_ratio": 0.0,
        "duplicate_line_ratio": 0.0,
        "concatenated_date_runs": 0,
    }
    if not visible:
        return TextQualityAssessment("empty", ("empty_text",), metrics)

    lines = [line.strip()[:500] for line in sample.splitlines()[:_MAX_INSPECTED_LINES] if line.strip()]
    metrics["line_count"] = len(lines)
    replacement_count = sample.count("\ufffd")
    control_count = sum(
        unicodedata.category(character) == "Cc" and character not in "\n\r\t"
        for character in sample
    )
    symbol_count = sum(
        not character.isalnum()
        and not character.isspace()
        and not unicodedata.category(character).startswith(("L", "N"))
        for character in sample
    )
    metrics["replacement_ratio"] = _ratio(replacement_count, visible_count)
    metrics["control_ratio"] = _ratio(control_count, visible_count)
    metrics["symbol_ratio"] = _ratio(symbol_count, visible_count)

    duplicate_ratio = 0.0
    if len(lines) >= 4:
        normalized_lines = [re.sub(r"\s+", " ", line).casefold() for line in lines]
        counts = Counter(normalized_lines)
        duplicate_occurrences = sum(count - 1 for count in counts.values() if count >= 3)
        duplicate_ratio = _ratio(duplicate_occurrences, len(normalized_lines))
    metrics["duplicate_line_ratio"] = duplicate_ratio

    date_runs = min(len(_CONCATENATED_DATES.findall(sample)), 100)
    metrics["concatenated_date_runs"] = date_runs

    reasons: list[str] = []
    # Keep this deliberately low: concise resumes such as "Python\n5年经验" are valid.
    if visible_count < 6:
        reasons.append("too_short")
    if replacement_count >= 3 and metrics["replacement_ratio"] >= 0.02:
        reasons.append("replacement_characters")
    if control_count >= 2 and metrics["control_ratio"] >= 0.01:
        reasons.append("control_characters")
    if visible_count >= 20 and metrics["symbol_ratio"] >= 0.45:
        reasons.append("gibberish_symbols")
    if duplicate_ratio >= 0.5:
        reasons.append("duplicate_line_domination")
    if date_runs:
        reasons.append("concatenated_date_runs")

    return TextQualityAssessment("poor" if reasons else "good", tuple(reasons[:6]), metrics)
