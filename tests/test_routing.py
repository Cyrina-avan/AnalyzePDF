from __future__ import annotations

from pathlib import Path

from analyzepdf.routing import route_with_fallback


def _quality(route_actions: dict[str, str]):
    def assess(contract: str | Path, source_pdf: str | Path) -> dict[str, object]:
        del source_pdf
        route_action = route_actions[Path(contract).stem]
        blocking_codes = [] if route_action == "publish" else ["synthetic_blocker"]
        return {
            "decision": "accept" if route_action == "publish" else "reject",
            "route_action": route_action,
            "reason_codes": blocking_codes,
            "warning_codes": [],
            "blocking_codes": blocking_codes,
            "failure_stages": [],
        }

    return assess


def test_primary_accept_does_not_call_fallback(tmp_path: Path) -> None:
    fallback_calls = 0

    def fallback() -> Path:
        nonlocal fallback_calls
        fallback_calls += 1
        return tmp_path / "fallback"

    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=lambda: tmp_path / "primary",
        fallback_runner=fallback,
        assessor=_quality({"primary": "publish"}),
    )

    assert decision.status == "selected_primary"
    assert decision.selected_route == "docling"
    assert fallback_calls == 0


def test_primary_review_warning_does_not_call_fallback(tmp_path: Path) -> None:
    fallback_calls = 0

    def fallback() -> Path:
        nonlocal fallback_calls
        fallback_calls += 1
        return tmp_path / "fallback"

    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=lambda: tmp_path / "primary",
        fallback_runner=fallback,
        assessor=lambda contract, source_pdf: {
            "decision": "review",
            "route_action": "publish",
            "reason_codes": ["cjk_internal_space_ratio_above_policy"],
            "warning_codes": ["cjk_internal_space_ratio_above_policy"],
            "blocking_codes": [],
            "failure_stages": [],
        },
    )

    assert decision.status == "selected_primary"
    assert decision.selected_route == "docling"
    assert decision.attempts[0].quality_decision == "review"
    assert decision.attempts[0].route_action == "publish"
    assert decision.attempts[0].warning_codes == (
        "cjk_internal_space_ratio_above_policy",
    )
    assert fallback_calls == 0


def test_primary_blocker_calls_and_selects_fallback(tmp_path: Path) -> None:
    fallback_calls = 0

    def fallback() -> Path:
        nonlocal fallback_calls
        fallback_calls += 1
        return tmp_path / "fallback"

    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=lambda: tmp_path / "primary",
        fallback_runner=fallback,
        assessor=_quality({"primary": "fallback", "fallback": "publish"}),
    )

    assert decision.status == "selected_fallback"
    assert decision.selected_route == "mineru"
    assert decision.attempts[0].blocking_codes == ("synthetic_blocker",)
    assert fallback_calls == 1


def test_primary_failure_still_calls_fallback(tmp_path: Path) -> None:
    def primary() -> Path:
        raise RuntimeError("synthetic parser failure")

    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=primary,
        fallback_runner=lambda: tmp_path / "fallback",
        assessor=_quality({"fallback": "publish"}),
    )

    assert decision.status == "selected_fallback"
    assert decision.attempts[0].failure_code == "PARSER_RUN_FAILED"


def test_both_routes_unreliable_degrades_without_selecting_text(tmp_path: Path) -> None:
    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=lambda: tmp_path / "primary",
        fallback_runner=lambda: tmp_path / "fallback",
        assessor=_quality({"primary": "fallback", "fallback": "fallback"}),
    )

    assert decision.status == "degraded"
    assert decision.selected_route is None
    assert decision.selected_contract is None
    assert [attempt.route for attempt in decision.attempts] == ["docling", "mineru"]
