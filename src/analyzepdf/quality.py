"""根据源 PDF 和统一结果判断解析质量。"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal

from pypdf import PdfReader

from analyzepdf.contracts.validation import load_contract


QUALITY_GATE_VERSION = 3
Decision = Literal["accept", "review", "reject"]
RouteAction = Literal["publish", "fallback"]
_CJK_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CJK_INTERNAL_HORIZONTAL_SPACE_RE = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])[ \t]+"
    r"(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
)


class QualityGateError(ValueError):
    """Raised when quality evidence cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class QualityGatePolicy:
    minimum_scan_dpi: float = 150.0
    minimum_text_characters_per_page: float = 20.0
    minimum_ocr_mean_confidence: float = 0.85
    maximum_replacement_character_ratio: float = 0.005
    maximum_cjk_internal_space_ratio: float = 0.01
    scan_aspect_ratio_tolerance: float = 0.05


def inspect_pdf_source(
    source_pdf: str | Path,
    *,
    policy: QualityGatePolicy | None = None,
) -> dict[str, Any]:
    """Collect non-sensitive source evidence without inventing OCR confidence."""

    active_policy = policy or QualityGatePolicy()
    path = Path(source_pdf)
    payload = path.read_bytes()
    report: dict[str, Any] = {
        "content_sha256": sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "readable": False,
        "encrypted": False,
        "page_count": 0,
        "requires_ocr_pages": [],
        "scan_like_pages": [],
        "low_resolution_scan_pages": [],
        "unclassified_no_text_pages": [],
        "minimum_estimated_scan_dpi": None,
        "pages": [],
    }
    try:
        reader = PdfReader(path)
        report["encrypted"] = bool(reader.is_encrypted)
        if reader.is_encrypted:
            return report

        pages = list(reader.pages)
        report["readable"] = True
        report["page_count"] = len(pages)
        estimated_dpis: list[float] = []
        for index, page in enumerate(pages, start=1):
            width_points = float(page.mediabox.width)
            height_points = float(page.mediabox.height)
            try:
                direct_text_characters = len((page.extract_text() or "").strip())
            except Exception:
                direct_text_characters = 0

            image_count = 0
            scan_like = False
            estimated_dpi: float | None = None
            try:
                images = list(page.images)
                image_count = len(images)
                if direct_text_characters == 0 and len(images) == 1:
                    pixel_width, pixel_height = images[0].image.size
                    page_ratio = width_points / height_points
                    image_ratio = pixel_width / pixel_height
                    ratio_error = abs(image_ratio - page_ratio) / page_ratio
                    if ratio_error <= active_policy.scan_aspect_ratio_tolerance:
                        scan_like = True
                        estimated_dpi = min(
                            pixel_width / (width_points / 72.0),
                            pixel_height / (height_points / 72.0),
                        )
            except Exception:
                image_count = 0

            if scan_like:
                report["requires_ocr_pages"].append(index)
                report["scan_like_pages"].append(index)
                if estimated_dpi is not None:
                    estimated_dpis.append(estimated_dpi)
                    if estimated_dpi < active_policy.minimum_scan_dpi:
                        report["low_resolution_scan_pages"].append(index)
            elif direct_text_characters == 0:
                report["unclassified_no_text_pages"].append(index)

            report["pages"].append(
                {
                    "page_number": index,
                    "direct_text_characters": direct_text_characters,
                    "image_count": image_count,
                    "scan_like": scan_like,
                    "estimated_scan_dpi": (
                        round(estimated_dpi, 2) if estimated_dpi is not None else None
                    ),
                }
            )
        if estimated_dpis:
            report["minimum_estimated_scan_dpi"] = round(min(estimated_dpis), 2)
    except Exception:
        return report
    return report


def assess_ingestion_quality(
    contract_path: str | Path,
    source_pdf: str | Path,
    *,
    policy: QualityGatePolicy | None = None,
) -> dict[str, Any]:
    """Assess a validated Contract package and its exact source PDF."""

    active_policy = policy or QualityGatePolicy()
    parsed = load_contract(contract_path)
    contract_file = Path(contract_path)
    try:
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityGateError("Cannot read validated Contract payload") from exc

    source = inspect_pdf_source(source_pdf, policy=active_policy)
    blocking_reasons: set[str] = set()
    warning_reasons: set[str] = set()

    if source["content_sha256"] != parsed.source_sha256:
        blocking_reasons.add("source_hash_mismatch")
    if not source["readable"]:
        blocking_reasons.add(
            "source_pdf_encrypted" if source["encrypted"] else "source_pdf_unreadable"
        )
    if parsed.status == "failed":
        blocking_reasons.add("parse_failed")
    elif parsed.status == "partial":
        blocking_reasons.add("partial_parse")

    quality = contract["quality"]
    ocr = quality["ocr"]
    flags = set(quality["flags"])
    if "ocr_confidence_unavailable" in flags:
        warning_reasons.add("ocr_confidence_unavailable")
    if ocr.get("low_confidence_pages"):
        blocking_reasons.add("ocr_low_confidence_pages")
    mean_confidence = ocr.get("mean_confidence")
    if (
        isinstance(mean_confidence, (int, float))
        and mean_confidence < active_policy.minimum_ocr_mean_confidence
    ):
        blocking_reasons.add("ocr_mean_confidence_below_policy")
    if source["requires_ocr_pages"] and not ocr["used"] and parsed.status != "failed":
        blocking_reasons.add("ocr_required_but_disabled")
    if source["low_resolution_scan_pages"]:
        warning_reasons.add("scan_resolution_below_policy")
    if source["unclassified_no_text_pages"]:
        warning_reasons.add("source_pages_without_classifiable_content")

    page_numbers = set(parsed.page_numbers)
    if (
        source["readable"]
        and parsed.status != "failed"
        and source["page_count"] != len(page_numbers)
    ):
        if parsed.status == "partial":
            blocking_reasons.add("partial_source_contract_page_count_mismatch")
        else:
            blocking_reasons.add("source_contract_page_count_mismatch")
    covered_pages: set[int] = set()
    text_covered_pages: set[int] = set()
    for element in contract["elements"]:
        has_text = bool(str(element.get("text", "")).strip())
        has_content = has_text or bool(element.get("artifact_ids"))
        for provenance in element["provenance"]:
            page_number = provenance["page_number"]
            if has_content:
                covered_pages.add(page_number)
            if has_text:
                text_covered_pages.add(page_number)

    uncovered_pages = sorted(page_numbers - covered_pages)
    if page_numbers and len(uncovered_pages) == len(page_numbers):
        blocking_reasons.add("no_pages_with_parsed_content")
    elif uncovered_pages:
        blocking_reasons.add("parsed_page_coverage_incomplete")

    text = parsed.text or ""
    page_count = len(page_numbers)
    text_characters_per_page = len(text) / page_count if page_count else 0.0
    if (
        parsed.status != "failed"
        and text_characters_per_page < active_policy.minimum_text_characters_per_page
    ):
        warning_reasons.add("text_density_below_policy")
    replacement_character_ratio = text.count("\ufffd") / max(len(text), 1)
    if replacement_character_ratio > active_policy.maximum_replacement_character_ratio:
        blocking_reasons.add("replacement_character_ratio_above_policy")
    cjk_character_count = len(_CJK_CHARACTER_RE.findall(text))
    cjk_internal_space_count = len(_CJK_INTERNAL_HORIZONTAL_SPACE_RE.findall(text))
    cjk_internal_space_ratio = cjk_internal_space_count / max(cjk_character_count, 1)
    if cjk_internal_space_ratio > active_policy.maximum_cjk_internal_space_ratio:
        warning_reasons.add("cjk_internal_space_ratio_above_policy")

    table_metrics = _inspect_table_artifacts(contract_file.parent, contract)
    if table_metrics["empty_table_count"]:
        warning_reasons.add("empty_table_artifact")
    if table_metrics["sparse_table_count"]:
        warning_reasons.add("sparse_table_artifact")

    decision: Decision
    route_action: RouteAction
    if blocking_reasons:
        decision = "reject"
        route_action = "fallback"
    elif warning_reasons:
        decision = "review"
        route_action = "publish"
    else:
        decision = "accept"
        route_action = "publish"

    failure_stages = sorted(
        {
            stage
            for error in contract["errors"]
            if isinstance(error, dict)
            and isinstance((stage := error.get("stage")), str)
            and stage
        }
    )

    return {
        "quality_gate_version": QUALITY_GATE_VERSION,
        "decision": decision,
        "route_action": route_action,
        "reason_codes": sorted(blocking_reasons | warning_reasons),
        "warning_codes": sorted(warning_reasons),
        "blocking_codes": sorted(blocking_reasons),
        "failure_stages": failure_stages,
        "source": {key: value for key, value in source.items() if key != "pages"},
        "metrics": {
            "contract_status": parsed.status,
            "contract_page_count": page_count,
            "covered_pages": sorted(covered_pages),
            "text_covered_pages": sorted(text_covered_pages),
            "uncovered_pages": uncovered_pages,
            "text_characters": len(text),
            "text_characters_per_page": round(text_characters_per_page, 2),
            "replacement_character_ratio": round(replacement_character_ratio, 6),
            "cjk_character_count": cjk_character_count,
            "cjk_internal_space_count": cjk_internal_space_count,
            "cjk_internal_space_ratio": round(cjk_internal_space_ratio, 6),
            "ocr_used": bool(ocr["used"]),
            "ocr_mean_confidence": mean_confidence,
            **table_metrics,
        },
        "policy": asdict(active_policy),
    }


def _inspect_table_artifacts(package_dir: Path, contract: dict[str, Any]) -> dict[str, int]:
    artifacts = {item["artifact_id"]: item for item in contract["artifacts"]}
    table_count = 0
    empty_table_count = 0
    sparse_table_count = 0
    for element in contract["elements"]:
        if element["kind"] != "table":
            continue
        table_count += 1
        table_artifacts = [
            artifacts[artifact_id]
            for artifact_id in element["artifact_ids"]
            if artifacts[artifact_id]["kind"] == "table_csv"
        ]
        for artifact in table_artifacts:
            path = package_dir / artifact["path"]
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            non_empty_cells = sum(bool(cell.strip()) for row in rows for cell in row)
            cell_count = sum(len(row) for row in rows)
            if not rows or non_empty_cells == 0:
                empty_table_count += 1
            elif cell_count and non_empty_cells / cell_count < 0.5:
                sparse_table_count += 1
    return {
        "table_count": table_count,
        "empty_table_count": empty_table_count,
        "sparse_table_count": sparse_table_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--source-pdf", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = assess_ingestion_quality(args.contract, args.source_pdf)
        serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(serialized, end="")
        else:
            if args.output.exists() and not args.overwrite:
                raise QualityGateError("Output already exists; use --overwrite explicitly")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(f"质量报告已写入：{args.output.name}")
    except (QualityGateError, OSError, ValueError) as exc:
        print(f"质量评估失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
