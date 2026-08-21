"""把 PP-StructureV3 解析结果转换为统一文档结果。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import Any

from analyzepdf.contracts.validation import (
    CONTRACT_VERSION,
    ContractValidationError,
    load_contract,
)


CONTRACT_FILENAME = "parsed-document.json"
SANITIZED_PARSER_FILENAME = "parser.json"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ERROR_STAGES = {"input", "download", "parse", "ocr", "layout", "export", "unknown"}
_RUN_STATUSES = {"succeeded", "partial", "failed"}
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|\\\\[^\\\s]+[\\/]"
    r"|(?<![A-Za-z0-9:/])/(?:Users|Volumes|home|private|tmp|var|mnt)/)"
)
_SENSITIVE_REJECT_KEYS = {"input_path", "traceback"}
_SENSITIVE_STRIP_KEYS = {
    "absolute_path",
    "filename",
    "file_name",
    "input_path",
    "source_path",
    "traceback",
}
_SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_LABEL_TO_KIND = {
    "caption": "caption",
    "chart": "picture",
    "doc_title": "heading",
    "figure_title": "caption",
    "footer": "footer",
    "formula": "formula",
    "header": "header",
    "image": "picture",
    "list_item": "list_item",
    "number": "page_number",
    "paragraph_title": "heading",
    "table": "table",
    "table_title": "caption",
    "text": "paragraph",
}
_ARTIFACT_KINDS = {"table", "picture"}


class PPStructureAdapterError(ValueError):
    """Raised when PP-StructureV3 output cannot safely become Contract v1."""


def adapt_ppstructure_output(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    document_id: str,
    source_ref: str,
    language: str,
) -> Path:
    """Create a self-contained, sanitized Contract Package from PP-StructureV3 output."""

    input_path = Path(input_dir).resolve()
    output_path = Path(output_dir).resolve()
    _identifier(document_id, "document_id")
    _identifier(source_ref, "source_ref")
    if not isinstance(language, str) or len(language.strip()) < 2:
        raise PPStructureAdapterError("language must contain at least two characters")
    if output_path.exists():
        raise PPStructureAdapterError(f"Output directory already exists: {output_path}")

    run = _read_json(input_path / "run.json", "PP-Structure run.json")
    _reject_sensitive_keys(run, "run.json")
    status = _validate_run(input_path, run)
    errors = _validated_run_errors(run, status)

    source = _mapping(run.get("source"), "run.source")
    parser = _mapping(run.get("parser"), "run.parser")
    result = _mapping(run.get("result"), "run.result")
    request = _mapping(run.get("request"), "run.request")
    request_hash = sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    run_id_material = (
        f"{source['content_sha256']}:{parser['name']}:{parser['version']}:"
        f"{result['started_at']}:{request_hash}"
    )
    run_id = f"run-{sha256(run_id_material.encode('utf-8')).hexdigest()[:24]}"
    use_ocr = _request_uses_ocr(request)
    quality_flags: list[str] = []
    if status == "partial":
        quality_flags.append("partial_parse")
    elif status == "failed":
        quality_flags.append("parse_failed")
    ocr_threshold = _ocr_confidence_threshold(request) if use_ocr else None
    ocr: dict[str, Any] = {"used": use_ocr, "low_confidence_pages": []}
    if use_ocr:
        ocr.update(_ocr_engine_metadata(request, parser))
        quality_flags.append("ocr_confidence_unavailable")

    contract: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "document_id": document_id,
        "source": {
            "source_ref": source_ref,
            "content_sha256": source["content_sha256"],
            "media_type": "application/pdf",
            "byte_size": source["byte_size"],
        },
        "parser_run": {
            "run_id": run_id,
            "parser_name": parser["name"],
            "parser_version": parser["version"],
            "engine_name": parser["engine_name"],
            "engine_version": parser["engine_version"],
            "backend": parser["backend"],
            "config_sha256": request_hash,
            "started_at": result["started_at"],
            "completed_at": result["completed_at"],
            "duration_ms": result["duration_ms"],
        },
        "status": status,
        "pages": [],
        "elements": [],
        "artifacts": [],
        "quality": {"flags": quality_flags, "ocr": ocr},
        "errors": errors,
    }

    staging = output_path.with_name(f".{output_path.name}.staging")
    if staging.exists():
        raise PPStructureAdapterError(f"Staging directory already exists: {staging}")
    staging.mkdir(parents=True)

    try:
        if status != "failed":
            markdown_path = input_path / "content.md"
            if not markdown_path.is_file() or markdown_path.stat().st_size == 0:
                raise PPStructureAdapterError("PP-Structure content.md is missing or empty")
            shutil.copy2(markdown_path, staging / "content.md")

            page_runs = _page_run_records(run)
            page_payloads = _load_page_payloads(input_path, page_runs)
            sanitized_pages = [_sanitize_parser_payload(page) for page in page_payloads]
            parser_payload = {"pages": sanitized_pages}
            _write_json(staging / SANITIZED_PARSER_FILENAME, parser_payload)

            pages = _adapt_pages(page_runs, page_payloads)
            artifact_map, artifacts = _copy_page_artifacts(
                input_path, page_runs, staging
            )
            artifacts.extend(
                [
                    _artifact_record(
                        "content-markdown", "markdown", staging / "content.md", staging
                    ),
                    _artifact_record(
                        "parser-json",
                        "structured_json",
                        staging / SANITIZED_PARSER_FILENAME,
                        staging,
                    ),
                ]
            )

            ocr_scores_available = _apply_ocr_confidence(
                ocr,
                page_runs,
                page_payloads,
                threshold=ocr_threshold,
            )
            if use_ocr and ocr_scores_available:
                quality_flags.remove("ocr_confidence_unavailable")

            elements, missing_artifact_count, bbox_degenerate_expanded = _adapt_elements(
                page_runs,
                page_payloads,
                artifact_map=artifact_map,
                status=status,
            )
            if bbox_degenerate_expanded:
                quality_flags.append("bbox_degenerate_expanded")
            if missing_artifact_count:
                if status == "succeeded":
                    raise PPStructureAdapterError(
                        "Succeeded PP-Structure run has table or picture blocks without artifacts"
                    )
                quality_flags.append("artifact_mapping_degraded")

            markdown = (staging / "content.md").read_text(encoding="utf-8")
            contract["text"] = {
                "content": markdown,
                "content_sha256": sha256(markdown.encode("utf-8")).hexdigest(),
                "language": language.strip(),
            }
            contract["pages"] = pages
            contract["elements"] = elements
            contract["artifacts"] = artifacts

        contract_path = staging / CONTRACT_FILENAME
        _write_json(contract_path, contract)
        load_contract(contract_path)
        staging.replace(output_path)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return output_path / CONTRACT_FILENAME


def _validate_run(input_dir: Path, run: dict[str, Any]) -> str:
    if run.get("output_state_version") != 1:
        raise PPStructureAdapterError("Unsupported or missing PP-Structure output_state_version")
    result = _mapping(run.get("result"), "run.result")
    status = result.get("status")
    if status not in _RUN_STATUSES:
        raise PPStructureAdapterError(f"Unsupported PP-Structure run status: {status!r}")

    inventory = run.get("output_files")
    if not isinstance(inventory, list) or (status != "failed" and not inventory):
        raise PPStructureAdapterError("PP-Structure output inventory is missing")

    inventory_paths: set[str] = set()
    for index, item_value in enumerate(inventory):
        item = _mapping(item_value, f"run.output_files[{index}]")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise PPStructureAdapterError(f"Unsafe inventory path at index {index}")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise PPStructureAdapterError(f"Unsafe inventory path at index {index}")
        if relative in inventory_paths:
            raise PPStructureAdapterError(f"Duplicate inventory path: {relative}")
        inventory_paths.add(relative)
        artifact = input_dir.joinpath(*path.parts)
        if not artifact.is_file():
            raise PPStructureAdapterError(f"Inventory file is missing: {relative}")
        if artifact.stat().st_size != item.get("byte_size"):
            raise PPStructureAdapterError(f"Inventory size mismatch: {relative}")
        if _file_sha256(artifact) != item.get("content_sha256"):
            raise PPStructureAdapterError(f"Inventory hash mismatch: {relative}")

    managed_paths = _managed_output_paths(input_dir, run, status)
    if inventory_paths != managed_paths:
        raise PPStructureAdapterError("PP-Structure inventory does not match managed output files")
    return status


def _managed_output_paths(input_dir: Path, run: dict[str, Any], status: str) -> set[str]:
    if status == "failed":
        return set()
    managed: set[str] = set()
    content_md = input_dir / "content.md"
    if content_md.is_file():
        managed.add("content.md")
    for page_run in _page_run_records(run):
        for key in ("json_path", "markdown_path"):
            relative = page_run.get(key)
            if isinstance(relative, str) and relative:
                managed.add(relative)
        for collection in ("images", "tables"):
            for index, item_value in enumerate(page_run.get(collection, [])):
                item = _mapping(item_value, f"pages[].{collection}[{index}]")
                artifact_path = item.get("artifact_path")
                if isinstance(artifact_path, str) and artifact_path:
                    managed.add(artifact_path)
    return managed


def _page_run_records(run: dict[str, Any]) -> list[dict[str, Any]]:
    pages = run.get("pages")
    if not isinstance(pages, list) or not pages:
        raise PPStructureAdapterError("PP-Structure pages must be a non-empty array")
    records: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for index, value in enumerate(pages):
        page = _mapping(value, f"run.pages[{index}]")
        page_number = page.get("page_number")
        if not isinstance(page_number, int) or page_number < 1:
            raise PPStructureAdapterError(f"Invalid page_number in run.pages[{index}]")
        if page_number in seen_numbers:
            raise PPStructureAdapterError(f"Duplicate page_number {page_number}")
        seen_numbers.add(page_number)
        width = page.get("width")
        height = page.get("height")
        if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
            raise PPStructureAdapterError(f"Invalid page size in run.pages[{index}]")
        if width <= 0 or height <= 0:
            raise PPStructureAdapterError(f"Invalid page size in run.pages[{index}]")
        json_path = page.get("json_path")
        if not isinstance(json_path, str) or not json_path or "\\" in json_path:
            raise PPStructureAdapterError(f"Unsafe json_path in run.pages[{index}]")
        _safe_relative_path(json_path, f"run.pages[{index}].json_path")
        for key in ("markdown_path",):
            relative = page.get(key)
            if relative is not None:
                if not isinstance(relative, str) or "\\" in relative:
                    raise PPStructureAdapterError(f"Unsafe {key} in run.pages[{index}]")
                _safe_relative_path(relative, f"run.pages[{index}].{key}")
        for collection in ("images", "tables"):
            values = page.get(collection, [])
            if not isinstance(values, list):
                raise PPStructureAdapterError(f"run.pages[{index}].{collection} must be an array")
            for item_index, item_value in enumerate(values):
                item = _mapping(item_value, f"run.pages[{index}].{collection}[{item_index}]")
                artifact_path = item.get("artifact_path")
                if not isinstance(artifact_path, str) or not artifact_path:
                    raise PPStructureAdapterError(
                        f"Missing artifact_path in run.pages[{index}].{collection}[{item_index}]"
                    )
                _safe_relative_path(
                    artifact_path,
                    f"run.pages[{index}].{collection}[{item_index}].artifact_path",
                )
                block_id = item.get("block_id")
                if not isinstance(block_id, (str, int)):
                    raise PPStructureAdapterError(
                        f"Missing block_id in run.pages[{index}].{collection}[{item_index}]"
                    )
                bbox = item.get("bbox")
                if bbox is not None:
                    _parse_bbox(bbox, f"run.pages[{index}].{collection}[{item_index}].bbox")
        records.append(page)
    return sorted(records, key=lambda page: int(page["page_number"]))


def _load_page_payloads(
    input_dir: Path, page_runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    document_page_count: int | None = None
    highest_page_number = max(int(page["page_number"]) for page in page_runs)
    for page_run in page_runs:
        json_path = page_run["json_path"]
        payload = _read_json(input_dir / json_path, f"page JSON {json_path}")
        _reject_sensitive_keys(payload, json_path)
        page_index = payload.get("page_index")
        page_number = int(page_run["page_number"])
        if not isinstance(page_index, int) or page_index < 0:
            raise PPStructureAdapterError(f"Invalid page_index in {json_path}")
        if page_index + 1 != page_number:
            raise PPStructureAdapterError(
                f"page_index {page_index} does not match page_number {page_number} in {json_path}"
            )
        page_count = payload.get("page_count")
        if not isinstance(page_count, int) or page_count < 1:
            raise PPStructureAdapterError(f"Invalid page_count in {json_path}")
        if page_count < len(page_runs) or page_count < highest_page_number:
            raise PPStructureAdapterError(
                f"page_count mismatch in {json_path}: smaller than emitted page coverage"
            )
        if document_page_count is None:
            document_page_count = page_count
        elif page_count != document_page_count:
            raise PPStructureAdapterError(
                f"page_count mismatch in {json_path}: expected {document_page_count}"
            )
        width = payload.get("width")
        height = payload.get("height")
        if float(width) != float(page_run["width"]) or float(height) != float(page_run["height"]):
            raise PPStructureAdapterError(f"Page size mismatch in {json_path}")
        parsing_res_list = payload.get("parsing_res_list")
        if not isinstance(parsing_res_list, list):
            raise PPStructureAdapterError(f"parsing_res_list must be an array in {json_path}")
        payloads.append(payload)
    return payloads


def _adapt_pages(
    page_runs: list[dict[str, Any]], page_payloads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for page_run, payload in zip(page_runs, page_payloads, strict=True):
        pages.append(
            {
                "page_number": int(page_run["page_number"]),
                "width": float(payload["width"]),
                "height": float(payload["height"]),
                "unit": "px",
            }
        )
    return pages


def _copy_page_artifacts(
    input_dir: Path,
    page_runs: list[dict[str, Any]],
    staging: Path,
) -> tuple[dict[tuple[int, str], str], list[dict[str, Any]]]:
    artifact_map: dict[tuple[int, str], str] = {}
    artifacts: list[dict[str, Any]] = []
    destination_paths: set[str] = set()
    table_index = 0
    image_index = 0
    for page_run in page_runs:
        page_number = int(page_run["page_number"])
        for collection, kind, destination_name, id_prefix, counter_name in (
            ("tables", "table_csv", "tables", "table", "table_index"),
            ("images", "image", "images", "image", "image_index"),
        ):
            for item_value in page_run.get(collection, []):
                item = _mapping(item_value, f"pages[{page_number}].{collection}")
                artifact_path = item["artifact_path"]
                block_id = _artifact_block_id_key(item["block_id"])
                artifact_key = (page_number, block_id)
                if artifact_key in artifact_map:
                    raise PPStructureAdapterError(
                        f"Duplicate artifact binding for page {page_number} block {block_id}"
                    )
                suffix = Path(artifact_path).suffix.lower()
                if collection == "tables":
                    if suffix != ".csv":
                        raise PPStructureAdapterError(
                            f"Table artifact must be .csv: {artifact_path}"
                        )
                elif suffix not in _SUPPORTED_IMAGE_SUFFIXES:
                    raise PPStructureAdapterError(
                        f"Unsupported image artifact suffix: {artifact_path}"
                    )
                source = input_dir / artifact_path
                destination_dir = staging / destination_name
                destination_dir.mkdir(parents=True, exist_ok=True)
                destination = destination_dir / Path(artifact_path).name
                relative_destination = destination.relative_to(staging).as_posix()
                if relative_destination in destination_paths:
                    raise PPStructureAdapterError(
                        f"Artifact destination conflict: {relative_destination}"
                    )
                destination_paths.add(relative_destination)
                shutil.copy2(source, destination)
                if counter_name == "table_index":
                    artifact_id = f"{id_prefix}-{table_index}"
                    table_index += 1
                else:
                    artifact_id = f"{id_prefix}-{image_index}"
                    image_index += 1
                artifacts.append(
                    _artifact_record(artifact_id, kind, destination, staging)
                )
                artifact_map[artifact_key] = artifact_id
    return artifact_map, artifacts


def _adapt_elements(
    page_runs: list[dict[str, Any]],
    page_payloads: list[dict[str, Any]],
    *,
    artifact_map: dict[tuple[int, str], str],
    status: str,
) -> tuple[list[dict[str, Any]], int, bool]:
    page_sizes = {
        int(page_run["page_number"]): (
            float(payload["width"]),
            float(payload["height"]),
        )
        for page_run, payload in zip(page_runs, page_payloads, strict=True)
    }
    blocks: list[tuple[int, dict[str, Any]]] = []
    page_block_lists: dict[int, list[dict[str, Any]]] = {}
    for page_run, payload in zip(page_runs, page_payloads, strict=True):
        page_number = int(page_run["page_number"])
        for block_value in payload.get("parsing_res_list", []):
            block = _mapping(block_value, f"page {page_number} block")
            order = block.get("block_order")
            if order is not None and not isinstance(order, int):
                raise PPStructureAdapterError(
                    f"block_order must be an integer or null on page {page_number}"
                )
            page_block_lists.setdefault(page_number, []).append(block)

    for page_number in sorted(page_block_lists):
        page_blocks = page_block_lists[page_number]
        # PaddleX returns parsing_res_list in reconstructed document sequence,
        # but tables, captions, headers, and page numbers may have no numeric
        # block_order.  In that mixed case the list position is the only signal
        # that can keep an unnumbered table between the surrounding paragraphs.
        # Fully numbered pages can still use the explicit parser order.
        if any(block.get("block_order") is None for block in page_blocks):
            ordered_blocks = page_blocks
        else:
            ordered_blocks = sorted(
                page_blocks,
                key=lambda block: _within_page_block_sort_key(page_number, block),
            )
        blocks.extend((page_number, block) for block in ordered_blocks)

    elements: list[dict[str, Any]] = []
    missing_artifact_count = 0
    bbox_degenerate_expanded = False
    for page_number, block in blocks:
        label = str(block.get("block_label", ""))
        mapped_kind = _LABEL_TO_KIND.get(label, "other")
        block_id_key = _artifact_block_id_key(block.get("block_id"))
        artifact_ids: list[str] = []
        kind = mapped_kind
        if mapped_kind in _ARTIFACT_KINDS:
            artifact_id = artifact_map.get((page_number, block_id_key))
            if artifact_id is None:
                missing_artifact_count += 1
                kind = "other"
            else:
                artifact_ids = [artifact_id]

        page_width, page_height = page_sizes[page_number]
        bbox, expanded = _adapt_block_bbox(
            block.get("block_bbox"),
            page_number=page_number,
            page_width=page_width,
            page_height=page_height,
            context=f"page {page_number} block {block_id_key}",
        )
        if expanded:
            bbox_degenerate_expanded = True
        element_id = _element_id(page_number, block)
        element: dict[str, Any] = {
            "element_id": element_id,
            "kind": kind,
            "reading_order": len(elements),
            "provenance": [
                {
                    "page_number": page_number,
                    "bbox": bbox,
                    "parser_element_ref": f"page:{page_number - 1}/block:{block_id_key}",
                }
            ],
            "artifact_ids": artifact_ids,
        }
        content = block.get("block_content")
        if isinstance(content, str) and content:
            element["text"] = content
        elements.append(element)

    if not elements:
        raise PPStructureAdapterError("PP-Structure output produced no Contract elements")
    return elements, missing_artifact_count, bbox_degenerate_expanded


def _adapt_block_bbox(
    value: Any,
    *,
    page_number: int,
    page_width: float,
    page_height: float,
    context: str,
) -> tuple[dict[str, Any], bool]:
    x0, y0, x1, y1 = _parse_bbox(value, context)
    if not all(math.isfinite(number) for number in (x0, y0, x1, y1)):
        raise PPStructureAdapterError(f"Non-finite bbox for {context}")
    if x1 < x0:
        raise PPStructureAdapterError(f"Inverted bbox x coordinates for {context}")
    if y1 < y0:
        raise PPStructureAdapterError(f"Inverted bbox y coordinates for {context}")
    if x0 < 0 or y0 < 0 or x1 > page_width or y1 > page_height:
        raise PPStructureAdapterError(f"Bbox lies outside page bounds for {context}")
    expanded = False
    if x1 == x0:
        x0, x1, x_expanded = _expand_degenerate_interval(x0, x1, limit=page_width)
        expanded = expanded or x_expanded
    if y1 == y0:
        y0, y1, y_expanded = _expand_degenerate_interval(y0, y1, limit=page_height)
        expanded = expanded or y_expanded
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "unit": "px",
        "origin": "top-left",
    }, expanded


def _parse_bbox(value: Any, context: str) -> tuple[float, float, float, float]:
    if isinstance(value, list) and len(value) == 4:
        try:
            return tuple(float(item) for item in value)  # type: ignore[return-value]
        except (TypeError, ValueError) as exc:
            raise PPStructureAdapterError(f"Invalid bbox for {context}") from exc
    if isinstance(value, dict):
        try:
            return (
                float(value["x0"]),
                float(value["y0"]),
                float(value["x1"]),
                float(value["y1"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PPStructureAdapterError(f"Invalid bbox for {context}") from exc
    raise PPStructureAdapterError(f"Invalid bbox for {context}")


def _expand_degenerate_interval(
    lower: float,
    upper: float,
    *,
    limit: float,
    minimum_size: float = 0.5,
) -> tuple[float, float, bool]:
    if upper > lower:
        return lower, upper, False
    if not 0 <= lower <= limit or limit < minimum_size:
        raise PPStructureAdapterError("Degenerate bbox cannot be expanded safely")
    expanded_lower = max(0.0, lower - minimum_size / 2)
    expanded_upper = min(limit, expanded_lower + minimum_size)
    if expanded_upper - expanded_lower < minimum_size:
        expanded_lower = max(0.0, expanded_upper - minimum_size)
    if expanded_upper <= expanded_lower:
        raise PPStructureAdapterError("Degenerate bbox cannot be expanded safely")
    return expanded_lower, expanded_upper, True


def _element_id(page_number: int, block: dict[str, Any]) -> str:
    block_id = block.get("block_id")
    if isinstance(block_id, int):
        suffix = str(block_id)
    elif isinstance(block_id, str):
        suffix = _identifier_suffix(block_id)
    else:
        order = block.get("block_order")
        suffix = str(order) if isinstance(order, int) else "unknown"
    candidate = f"block-{page_number}-{suffix}"
    return _identifier(candidate, "element_id")


def _identifier_suffix(value: str) -> str:
    if _IDENTIFIER_RE.fullmatch(value):
        return value
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    if not sanitized or not re.match(r"[A-Za-z0-9]", sanitized[0]):
        sanitized = f"id-{sha256(value.encode('utf-8')).hexdigest()[:16]}"
    return sanitized[:159]


def _artifact_block_id_key(value: Any) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise PPStructureAdapterError("block_id must be a string or integer")


def _within_page_block_sort_key(page_number: int, block: dict[str, Any]) -> tuple[Any, ...]:
    order = block.get("block_order")
    if isinstance(order, int):
        return (0, order)
    y0, x0, y1, x1 = _sort_bbox_tuple(
        block.get("block_bbox"),
        context=f"page {page_number} block sort",
    )
    label = str(block.get("block_label", ""))
    block_id_key = _artifact_block_id_key(block.get("block_id"))
    return (1, y0, x0, y1, x1, label, block_id_key)


def _sort_bbox_tuple(value: Any, *, context: str) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = _parse_bbox(value, context)
    return (y0, x0, y1, x1)


def _ocr_engine_metadata(request: dict[str, Any], parser: dict[str, Any]) -> dict[str, str]:
    engine_name = request.get("ocr_engine_name")
    if not isinstance(engine_name, str) or not engine_name.strip():
        engine_name = parser.get("engine_name", "unknown")
    engine_version = request.get("ocr_engine_version")
    if not isinstance(engine_version, str) or not engine_version.strip():
        engine_version = parser.get("engine_version", "unknown")
    return {
        "engine_name": str(engine_name),
        "engine_version": str(engine_version),
    }


def _ocr_confidence_threshold(request: dict[str, Any]) -> float:
    threshold = request.get("ocr_low_confidence_threshold", 0.8)
    return _confidence_score(threshold, "request.ocr_low_confidence_threshold")


def _confidence_score(value: Any, context: str) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PPStructureAdapterError(f"Invalid confidence score at {context}")
    score = float(value)
    if not 0 <= score <= 1:
        raise PPStructureAdapterError(f"Confidence score out of range at {context}")
    return score


def _apply_ocr_confidence(
    ocr: dict[str, Any],
    page_runs: list[dict[str, Any]],
    page_payloads: list[dict[str, Any]],
    *,
    threshold: float | None,
) -> bool:
    if threshold is None:
        return False

    all_scores: list[float] = []
    low_confidence_pages: list[int] = []
    for page_run, payload in zip(page_runs, page_payloads, strict=True):
        page_number = int(page_run["page_number"])
        json_path = page_run.get("json_path", f"page {page_number}")
        ocr_lines = payload.get("ocr_lines", [])
        if ocr_lines is None:
            ocr_lines = []
        if not isinstance(ocr_lines, list):
            raise PPStructureAdapterError(f"ocr_lines must be an array in {json_path}")
        page_scores: list[float] = []
        for index, line_value in enumerate(ocr_lines):
            line = _mapping(line_value, f"{json_path}.ocr_lines[{index}]")
            page_scores.append(
                _confidence_score(line.get("score"), f"{json_path}.ocr_lines[{index}].score")
            )
        if page_scores:
            page_mean = sum(page_scores) / len(page_scores)
            all_scores.extend(page_scores)
            if page_mean < threshold:
                low_confidence_pages.append(page_number)

    if not all_scores:
        return False

    ocr["mean_confidence"] = sum(all_scores) / len(all_scores)
    ocr["low_confidence_pages"] = sorted(low_confidence_pages)
    return True


def _validated_run_errors(run: dict[str, Any], status: str) -> list[dict[str, Any]]:
    values = run.get("errors", [])
    if not isinstance(values, list):
        raise PPStructureAdapterError("PP-Structure errors must be an array")
    if status == "succeeded" and values:
        raise PPStructureAdapterError("Succeeded PP-Structure run must not contain errors")
    if status in {"partial", "failed"} and not values:
        raise PPStructureAdapterError(f"{status.capitalize()} PP-Structure run has no errors")

    records: list[dict[str, Any]] = []
    required = {"code", "stage", "sanitized_message", "retryable"}
    allowed = required | {"page_number"}
    for index, value in enumerate(values):
        record = _mapping(value, f"run.errors[{index}]")
        if set(record) - allowed or required - set(record):
            raise PPStructureAdapterError(f"Invalid keys in run.errors[{index}]")
        code = record.get("code")
        stage = record.get("stage")
        message = record.get("sanitized_message")
        retryable = record.get("retryable")
        if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
            raise PPStructureAdapterError(f"Invalid error code in run.errors[{index}]")
        if stage not in _ERROR_STAGES:
            raise PPStructureAdapterError(f"Invalid error stage in run.errors[{index}]")
        if (
            not isinstance(message, str)
            or not message.strip()
            or _ABSOLUTE_PATH_RE.search(message)
            or "Traceback (most recent call last)" in message
        ):
            raise PPStructureAdapterError(f"Unsafe error message in run.errors[{index}]")
        if not isinstance(retryable, bool):
            raise PPStructureAdapterError(f"Invalid retryable flag in run.errors[{index}]")
        if "page_number" in record and (
            not isinstance(record["page_number"], int) or record["page_number"] < 1
        ):
            raise PPStructureAdapterError(f"Invalid page number in run.errors[{index}]")
        records.append(dict(record))
    return records


def _sanitize_parser_payload(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key in _SENSITIVE_STRIP_KEYS:
                continue
            if child_key == "uri" and isinstance(child_value, str) and child_value.startswith("data:"):
                continue
            sanitized[child_key] = _sanitize_parser_payload(child_value, key=child_key)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_parser_payload(child, key=key) for child in value]
    if isinstance(value, str):
        if value.startswith("data:"):
            return "[binary-data-removed]"
        if _ABSOLUTE_PATH_RE.search(value):
            raise PPStructureAdapterError(f"Parser payload contains an absolute path at {key}")
        if "Traceback (most recent call last)" in value:
            raise PPStructureAdapterError(f"Parser payload contains a traceback at {key}")
    return value


def _reject_sensitive_keys(value: Any, context: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _SENSITIVE_REJECT_KEYS:
                raise PPStructureAdapterError(f"Forbidden key {key!r} at {context}")
            _reject_sensitive_keys(child, f"{context}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{context}[{index}]")


def _request_uses_ocr(request: dict[str, Any]) -> bool:
    for key in ("use_ocr", "enable_ocr", "ocr"):
        value = request.get(key)
        if isinstance(value, bool) and value:
            return True
    return False


def _artifact_record(
    artifact_id: str, kind: str, path: Path, package_root: Path
) -> dict[str, Any]:
    media_types = {
        ".csv": "text/csv",
        ".json": "application/json",
        ".md": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "path": path.relative_to(package_root).as_posix(),
        "media_type": media_types.get(path.suffix.lower(), "application/octet-stream"),
        "content_sha256": _file_sha256(path),
    }


def _safe_relative_path(value: str, context: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise PPStructureAdapterError(f"Unsafe path at {context}")
    return value


def _read_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PPStructureAdapterError(f"Cannot read {context}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PPStructureAdapterError(f"Invalid JSON in {context}: {exc.msg}") from exc
    return _mapping(value, context)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PPStructureAdapterError(f"{context} must be an object")
    return value


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise PPStructureAdapterError(f"{context} is not a valid identifier")
    return value


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, help="PP-StructureV3 per-document output")
    parser.add_argument("--output-dir", required=True, help="New Contract Package directory")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--language", required=True)
    args = parser.parse_args(argv)
    try:
        contract_path = adapt_ppstructure_output(
            args.input_dir,
            args.output_dir,
            document_id=args.document_id,
            source_ref=args.source_ref,
            language=args.language,
        )
    except (PPStructureAdapterError, ContractValidationError, OSError) as exc:
        print(f"PP-Structure Adapter failed: {exc}", file=sys.stderr)
        return 1
    print(f"Contract Package written to {contract_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
