"""根据解析质量自动选择 Docling、MinerU 或安全降级。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from analyzepdf.quality import assess_ingestion_quality


ParserRunner = Callable[[], Path]
QualityAssessor = Callable[[str | Path, str | Path], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    """一条解析路线的运行和质量判断摘要。"""

    route: str
    completed: bool
    contract_path: Path | None
    quality_decision: str | None
    route_action: str | None
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    blocking_codes: tuple[str, ...]
    failure_stages: tuple[str, ...]
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """路线选择结果；selected_route 为空表示正文不可安全使用。"""

    status: str
    selected_route: str | None
    selected_contract: Path | None
    attempts: tuple[RouteAttempt, ...]


def route_with_fallback(
    source_pdf: str | Path,
    *,
    primary_runner: ParserRunner,
    fallback_runner: ParserRunner,
    assessor: QualityAssessor = assess_ingestion_quality,
) -> RoutingDecision:
    """先运行 Docling；质量结果要求 fallback 时才运行 MinerU。

    解析器自身的异常不会直接泄漏到上层。详细诊断留在各路线自己的
    ``run.json`` 中；本层只记录稳定错误码，避免路线选择和底层实现耦合。
    """

    attempts: list[RouteAttempt] = []
    primary = _run_and_assess(
        "docling",
        source_pdf=source_pdf,
        runner=primary_runner,
        assessor=assessor,
    )
    attempts.append(primary)
    if primary.route_action == "publish":
        return RoutingDecision(
            status="selected_primary",
            selected_route="docling",
            selected_contract=primary.contract_path,
            attempts=tuple(attempts),
        )

    fallback = _run_and_assess(
        "mineru",
        source_pdf=source_pdf,
        runner=fallback_runner,
        assessor=assessor,
    )
    attempts.append(fallback)
    if fallback.route_action == "publish":
        return RoutingDecision(
            status="selected_fallback",
            selected_route="mineru",
            selected_contract=fallback.contract_path,
            attempts=tuple(attempts),
        )

    return RoutingDecision(
        status="degraded",
        selected_route=None,
        selected_contract=None,
        attempts=tuple(attempts),
    )


def _run_and_assess(
    route: str,
    *,
    source_pdf: str | Path,
    runner: ParserRunner,
    assessor: QualityAssessor,
) -> RouteAttempt:
    try:
        contract_path = Path(runner())
    except Exception:
        return RouteAttempt(
            route=route,
            completed=False,
            contract_path=None,
            quality_decision=None,
            route_action=None,
            reason_codes=(),
            warning_codes=(),
            blocking_codes=(),
            failure_stages=(),
            failure_code="PARSER_RUN_FAILED",
        )

    try:
        quality = assessor(contract_path, source_pdf)
    except Exception:
        return RouteAttempt(
            route=route,
            completed=True,
            contract_path=contract_path,
            quality_decision=None,
            route_action=None,
            reason_codes=(),
            warning_codes=(),
            blocking_codes=(),
            failure_stages=(),
            failure_code="QUALITY_ASSESSMENT_FAILED",
        )

    decision = quality.get("decision")
    if decision not in {"accept", "review", "reject"}:
        return RouteAttempt(
            route=route,
            completed=True,
            contract_path=contract_path,
            quality_decision=None,
            route_action=None,
            reason_codes=(),
            warning_codes=(),
            blocking_codes=(),
            failure_stages=(),
            failure_code="INVALID_QUALITY_DECISION",
        )
    route_action = quality.get("route_action")
    if route_action not in {"publish", "fallback"}:
        return RouteAttempt(
            route=route,
            completed=True,
            contract_path=contract_path,
            quality_decision=None,
            route_action=None,
            reason_codes=(),
            warning_codes=(),
            blocking_codes=(),
            failure_stages=(),
            failure_code="INVALID_QUALITY_REPORT",
        )
    list_fields = {
        name: quality.get(name)
        for name in (
            "reason_codes",
            "warning_codes",
            "blocking_codes",
            "failure_stages",
        )
    }
    if any(
        not isinstance(values, list)
        or not all(isinstance(item, str) and item for item in values)
        for values in list_fields.values()
    ):
        return RouteAttempt(
            route=route,
            completed=True,
            contract_path=contract_path,
            quality_decision=None,
            route_action=None,
            reason_codes=(),
            warning_codes=(),
            blocking_codes=(),
            failure_stages=(),
            failure_code="INVALID_QUALITY_REPORT",
        )
    reason_codes = set(list_fields["reason_codes"])
    warning_codes = set(list_fields["warning_codes"])
    blocking_codes = set(list_fields["blocking_codes"])
    if (
        reason_codes != warning_codes | blocking_codes
        or warning_codes & blocking_codes
        or (route_action == "publish" and blocking_codes)
        or (route_action == "fallback" and not blocking_codes)
    ):
        return RouteAttempt(
            route=route,
            completed=True,
            contract_path=contract_path,
            quality_decision=None,
            route_action=None,
            reason_codes=(),
            warning_codes=(),
            blocking_codes=(),
            failure_stages=(),
            failure_code="INVALID_QUALITY_REPORT",
        )
    return RouteAttempt(
        route=route,
        completed=True,
        contract_path=contract_path,
        quality_decision=decision,
        route_action=route_action,
        reason_codes=tuple(sorted(reason_codes)),
        warning_codes=tuple(sorted(warning_codes)),
        blocking_codes=tuple(sorted(blocking_codes)),
        failure_stages=tuple(sorted(set(list_fields["failure_stages"]))),
    )
