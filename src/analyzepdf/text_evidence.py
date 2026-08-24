"""比较主解析正文与 PDF 原生文字层，给出保守的解析证据结论。"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any


_CJK_INTERNAL_SPACE = re.compile(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])")


def compare_text_views(*, parsed_text: str, native_text: str) -> dict[str, Any]:
    """描述主解析正文与 PDF 原生文字的字符差异，不直接宣布谁正确。"""

    parsed = _compact_text(parsed_text)
    native = _compact_text(native_text)
    parsed_counter = Counter(parsed)
    native_counter = Counter(native)
    shared = sum((parsed_counter & native_counter).values())
    return {
        "exact_match_ignoring_whitespace": parsed == native,
        "native_cjk_internal_space_count": len(_CJK_INTERNAL_SPACE.findall(native_text)),
        "native_compact_character_count": len(native),
        "native_line_count": len(native_text.splitlines()),
        "native_multiset_coverage_by_parsed": _ratio(shared, len(native)),
        "order_similarity": round(SequenceMatcher(None, parsed, native).ratio(), 6),
        "parsed_cjk_internal_space_count": len(_CJK_INTERNAL_SPACE.findall(parsed_text)),
        "parsed_compact_character_count": len(parsed),
        "parsed_line_count": len(parsed_text.splitlines()),
        "parsed_multiset_coverage_by_native": _ratio(shared, len(parsed)),
    }


def choose_text_evidence_route(*, parsed_text: str, native_text: str) -> dict[str, Any]:
    """只根据双路字符证据提出解析视图建议；冲突时保持未决。"""

    metrics = compare_text_views(parsed_text=parsed_text, native_text=native_text)
    parsed_characters = metrics["parsed_compact_character_count"]
    native_characters = metrics["native_compact_character_count"]
    if native_characters == 0:
        return {
            "decision": "visual_primary",
            "reason": "native_text_layer_empty",
            "requires_reconciliation": False,
            "metrics": metrics,
        }
    if parsed_characters == 0:
        return {
            "decision": "native_primary",
            "reason": "native_text_only_nonempty_view",
            "requires_reconciliation": False,
            "metrics": metrics,
        }
    parsed_coverage = metrics["parsed_multiset_coverage_by_native"]
    native_coverage = metrics["native_multiset_coverage_by_parsed"]
    if parsed_coverage >= 0.98 and native_coverage >= 0.98:
        return {
            "decision": "native_primary",
            "reason": "cross_view_character_agreement_at_least_0_98",
            "requires_reconciliation": False,
            "metrics": metrics,
        }
    return {
        "decision": "unresolved",
        "reason": "native_and_parsed_views_materially_disagree",
        "requires_reconciliation": True,
        "metrics": metrics,
    }


def _compact_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not character.isspace()
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None
