from __future__ import annotations

from pathlib import Path

from analyzepdf.routing import route_with_fallback


def _quality(decisions: dict[str, str]):
    def assess(contract: str | Path, source_pdf: str | Path) -> dict[str, object]:
        del source_pdf
        decision = decisions[Path(contract).stem]
        return {
            "decision": decision,
            "reason_codes": [] if decision == "accept" else ["synthetic_anomaly"],
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
        assessor=_quality({"primary": "accept"}),
    )

    assert decision.status == "selected_primary"
    assert decision.selected_route == "docling"
    assert fallback_calls == 0


def test_primary_review_automatically_calls_and_selects_fallback(tmp_path: Path) -> None:
    fallback_calls = 0

    def fallback() -> Path:
        nonlocal fallback_calls
        fallback_calls += 1
        return tmp_path / "fallback"

    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=lambda: tmp_path / "primary",
        fallback_runner=fallback,
        assessor=_quality({"primary": "review", "fallback": "accept"}),
    )

    assert decision.status == "selected_fallback"
    assert decision.selected_route == "mineru"
    assert fallback_calls == 1


def test_primary_failure_still_calls_fallback(tmp_path: Path) -> None:
    def primary() -> Path:
        raise RuntimeError("synthetic parser failure")

    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=primary,
        fallback_runner=lambda: tmp_path / "fallback",
        assessor=_quality({"fallback": "accept"}),
    )

    assert decision.status == "selected_fallback"
    assert decision.attempts[0].failure_code == "PARSER_RUN_FAILED"


def test_both_routes_unreliable_degrades_without_selecting_text(tmp_path: Path) -> None:
    decision = route_with_fallback(
        tmp_path / "source.pdf",
        primary_runner=lambda: tmp_path / "primary",
        fallback_runner=lambda: tmp_path / "fallback",
        assessor=_quality({"primary": "reject", "fallback": "review"}),
    )

    assert decision.status == "degraded"
    assert decision.selected_route is None
    assert decision.selected_contract is None
    assert [attempt.route for attempt in decision.attempts] == ["docling", "mineru"]
