"""把 Docling 解析结果转换为统一文档结果。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import Any, Iterator

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
_PROVENANCE_PAGE_TOLERANCE_PT = 0.5
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|\\\\[^\\\s]+[\\/]"
    r"|(?<![A-Za-z0-9:/])/(?:Users|Volumes|home|private|tmp|var|mnt)/)"
)
_LABEL_TO_KIND = {
    "caption": "caption",
    "code": "other",
    "footnote": "paragraph",
    "formula": "formula",
    "list_item": "list_item",
    "page_footer": "footer",
    "page_header": "header",
    "page_number": "page_number",
    "section_header": "heading",
    "text": "paragraph",
}


class AnalyzePDFAdapterError(ValueError):
    """Raised when AnalyzePDF output cannot safely become Contract v1."""


def adapt_analyzepdf_output(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    document_id: str,
    source_ref: str,
    language: str,
) -> Path:
    """Create a self-contained, sanitized Contract Package.

    ``output_dir`` must not already exist. This prevents an Adapter retry from
    silently mixing old and new Artifacts; the caller chooses any replacement
    policy explicitly.
    """

    input_path = Path(input_dir).resolve()
    output_path = Path(output_dir).resolve()
    _identifier(document_id, "document_id")
    _identifier(source_ref, "source_ref")
    if not isinstance(language, str) or len(language.strip()) < 2:
        raise AnalyzePDFAdapterError("language must contain at least two characters")
    if output_path.exists():
        raise AnalyzePDFAdapterError(f"Output directory already exists: {output_path}")

    run = _read_json(input_path / "run.json", "AnalyzePDF run.json")
    status = _validate_run(input_path, run)
    errors = _validated_run_errors(run, status)
    if status != "failed":
        _require_complete_request(run)

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
        f"{source['content_sha256']}:{parser['source_sha256']}:"
        f"{result['started_at']}:{request_hash}"
    )
    run_id = f"run-{sha256(run_id_material.encode('utf-8')).hexdigest()[:24]}"
    use_ocr = bool(request.get("use_ocr"))
    quality_flags = ["ocr_confidence_unavailable"] if use_ocr else []
    if status == "partial":
        quality_flags.append("partial_parse")
    elif status == "failed":
        quality_flags.append("parse_failed")
    ocr: dict[str, Any] = {"used": use_ocr, "low_confidence_pages": []}
    if use_ocr:
        ocr.update({"engine_name": "rapidocr", "engine_version": "unknown"})

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
        raise AnalyzePDFAdapterError(f"Staging directory already exists: {staging}")
    staging.mkdir(parents=True)

    try:
        if status != "failed":
            native = _read_json(input_path / "content.json", "Docling content.json")
            markdown_path = input_path / "content.md"
            if not markdown_path.is_file() or markdown_path.stat().st_size == 0:
                raise AnalyzePDFAdapterError("AnalyzePDF content.md is missing or empty")
            shutil.copy2(markdown_path, staging / "content.md")
            sanitized_native = _sanitize_parser_payload(native)
            sanitized_native.pop("name", None)
            _write_json(staging / SANITIZED_PARSER_FILENAME, sanitized_native)

            pages = _adapt_pages(
                native.get("pages"), allow_invalid_placeholders=status == "partial"
            )
            table_artifacts = _copy_indexed_artifacts(
                input_path / "tables",
                staging / "tables",
                pattern="table_*.csv",
                kind="table_csv",
                id_prefix="table",
            )
            image_artifacts = _copy_indexed_artifacts(
                input_path / "artifacts",
                staging / "artifacts",
                pattern="image_*",
                kind="image",
                id_prefix="image",
            )
            artifacts = [
                _artifact_record(
                    "content-markdown", "markdown", staging / "content.md", staging
                ),
                _artifact_record(
                    "parser-json",
                    "structured_json",
                    staging / SANITIZED_PARSER_FILENAME,
                    staging,
                ),
                *table_artifacts,
                *image_artifacts,
            ]
            table_complete, image_complete = _artifact_cardinality(
                native, table_artifacts, image_artifacts
            )
            if status == "succeeded" and not (table_complete and image_complete):
                _require_artifact_cardinality(native, table_artifacts, image_artifacts)

            (
                elements,
                degenerate_bbox_expansion_count,
                formula_orig_fallback_count,
                page_bound_clamp_count,
            ) = _adapt_elements(
                native,
                pages=pages,
                table_artifact_ids=[artifact["artifact_id"] for artifact in table_artifacts],
                image_artifact_ids=[artifact["artifact_id"] for artifact in image_artifacts],
                include_tables=table_complete,
                include_pictures=image_complete,
            )
            if degenerate_bbox_expansion_count:
                quality_flags.append("degenerate_bbox_expanded")
            if formula_orig_fallback_count:
                quality_flags.append("formula_text_from_orig")
            if page_bound_clamp_count:
                quality_flags.append("bbox_clamped_to_page")
            markdown = (staging / "content.md").read_text(encoding="utf-8")
            contract["source"]["media_type"] = _source_media_type(native)
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
        raise AnalyzePDFAdapterError("Unsupported or missing AnalyzePDF output_state_version")
    result = _mapping(run.get("result"), "run.result")
    status = result.get("status")
    if status not in _RUN_STATUSES:
        raise AnalyzePDFAdapterError(f"Unsupported AnalyzePDF run status: {status!r}")
    inventory = run.get("output_files")
    if not isinstance(inventory, list) or (status != "failed" and not inventory):
        raise AnalyzePDFAdapterError("AnalyzePDF output inventory is missing")
    inventory_paths: set[str] = set()
    for index, item_value in enumerate(inventory):
        item = _mapping(item_value, f"run.output_files[{index}]")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise AnalyzePDFAdapterError(f"Unsafe inventory path at index {index}")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise AnalyzePDFAdapterError(f"Unsafe inventory path at index {index}")
        if relative in inventory_paths:
            raise AnalyzePDFAdapterError(f"Duplicate inventory path: {relative}")
        inventory_paths.add(relative)
        artifact = input_dir.joinpath(*path.parts)
        if not artifact.is_file():
            raise AnalyzePDFAdapterError(f"Inventory file is missing: {relative}")
        if artifact.stat().st_size != item.get("byte_size"):
            raise AnalyzePDFAdapterError(f"Inventory size mismatch: {relative}")
        if _file_sha256(artifact) != item.get("content_sha256"):
            raise AnalyzePDFAdapterError(f"Inventory hash mismatch: {relative}")

    actual_managed_paths: set[str] = set()
    for filename in ("content.md", "content.json", "source.txt"):
        path = input_dir / filename
        if path.is_file():
            actual_managed_paths.add(filename)
    for directory_name in ("artifacts", "tables"):
        directory = input_dir / directory_name
        if directory.is_dir():
            actual_managed_paths.update(
                path.relative_to(input_dir).as_posix()
                for path in directory.rglob("*")
                if path.is_file()
            )
    if inventory_paths != actual_managed_paths:
        raise AnalyzePDFAdapterError("AnalyzePDF inventory does not match managed output files")
    return status


def _validated_run_errors(run: dict[str, Any], status: str) -> list[dict[str, Any]]:
    values = run.get("errors", [])
    if not isinstance(values, list):
        raise AnalyzePDFAdapterError("AnalyzePDF errors must be an array")
    if status == "succeeded" and values:
        raise AnalyzePDFAdapterError("Succeeded AnalyzePDF run must not contain errors")
    if status in {"partial", "failed"} and not values:
        raise AnalyzePDFAdapterError(f"{status.capitalize()} AnalyzePDF run has no errors")

    records: list[dict[str, Any]] = []
    required = {"code", "stage", "sanitized_message", "retryable"}
    allowed = required | {"page_number"}
    for index, value in enumerate(values):
        record = _mapping(value, f"run.errors[{index}]")
        if set(record) - allowed or required - set(record):
            raise AnalyzePDFAdapterError(f"Invalid keys in run.errors[{index}]")
        code = record.get("code")
        stage = record.get("stage")
        message = record.get("sanitized_message")
        retryable = record.get("retryable")
        if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
            raise AnalyzePDFAdapterError(f"Invalid error code in run.errors[{index}]")
        if stage not in _ERROR_STAGES:
            raise AnalyzePDFAdapterError(f"Invalid error stage in run.errors[{index}]")
        if (
            not isinstance(message, str)
            or not message.strip()
            or _ABSOLUTE_PATH_RE.search(message)
            or "Traceback (most recent call last)" in message
        ):
            raise AnalyzePDFAdapterError(f"Unsafe error message in run.errors[{index}]")
        if not isinstance(retryable, bool):
            raise AnalyzePDFAdapterError(f"Invalid retryable flag in run.errors[{index}]")
        if "page_number" in record and (
            not isinstance(record["page_number"], int) or record["page_number"] < 1
        ):
            raise AnalyzePDFAdapterError(f"Invalid page number in run.errors[{index}]")
        records.append(dict(record))
    return records


def _require_complete_request(run: dict[str, Any]) -> None:
    request = _mapping(run.get("request"), "run.request")
    options = _mapping(request.get("output_options"), "run.request.output_options")
    required = {"write_json": True, "write_tables": True, "export_images": True}
    mismatched = [key for key, expected in required.items() if options.get(key) is not expected]
    if mismatched:
        raise AnalyzePDFAdapterError(
            "Contract v1 Adapter requires AnalyzePDF full output; mismatched options: "
            f"{mismatched}"
        )


def _sanitize_parser_payload(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key in {"filename", "file_name", "source_path", "absolute_path"}:
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
            raise AnalyzePDFAdapterError(f"Parser payload contains an absolute path at {key}")
    return value


def _adapt_pages(
    value: Any, *, allow_invalid_placeholders: bool = False
) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise AnalyzePDFAdapterError("Docling pages must be a non-empty object")
    pages: list[dict[str, Any]] = []
    for key, page_value in value.items():
        page = _mapping(page_value, f"pages.{key}")
        size = _mapping(page.get("size"), f"pages.{key}.size")
        page_number = page.get("page_no", key)
        try:
            page_number = int(page_number)
            width = float(size["width"])
            height = float(size["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalyzePDFAdapterError(f"Invalid Docling page metadata at page {key}") from exc
        if page_number < 1 or width <= 0 or height <= 0:
            if allow_invalid_placeholders:
                continue
            raise AnalyzePDFAdapterError(f"Invalid Docling page metadata at page {key}")
        pages.append(
            {"page_number": page_number, "width": width, "height": height, "unit": "pt"}
        )
    if not pages:
        raise AnalyzePDFAdapterError("Docling pages contain no usable page metadata")
    return sorted(pages, key=lambda page: page["page_number"])


def _adapt_elements(
    native: dict[str, Any],
    *,
    pages: list[dict[str, Any]],
    table_artifact_ids: list[str],
    image_artifact_ids: list[str],
    include_tables: bool = True,
    include_pictures: bool = True,
) -> tuple[list[dict[str, Any]], int, int, int]:
    registry = _element_registry(native)
    page_sizes = {
        int(page["page_number"]): (float(page["width"]), float(page["height"]))
        for page in pages
    }
    body = _mapping(native.get("body"), "Docling body")
    children = body.get("children")
    if not isinstance(children, list):
        raise AnalyzePDFAdapterError("Docling body.children must be an array")

    elements: list[dict[str, Any]] = []
    degenerate_bbox_expansion_count = 0
    formula_orig_fallback_count = 0
    page_bound_clamp_count = 0
    seen_refs: set[str] = set()
    for reference in _walk_references(children, registry):
        if reference in seen_refs:
            continue
        seen_refs.add(reference)
        collection, index = _split_reference(reference)
        item = registry[reference]
        provenance, expansion_count, clamp_count = _adapt_provenance(
            item.get("prov"), reference, page_sizes=page_sizes
        )
        degenerate_bbox_expansion_count += expansion_count
        page_bound_clamp_count += clamp_count
        if collection == "texts":
            kind = _LABEL_TO_KIND.get(str(item.get("label")), "other")
            artifact_ids: list[str] = []
            text = item.get("text")
            if (
                kind == "formula"
                and (not isinstance(text, str) or not text.strip())
                and isinstance(item.get("orig"), str)
                and item["orig"].strip()
            ):
                text = item["orig"]
                formula_orig_fallback_count += 1
        elif collection == "tables":
            if not include_tables:
                continue
            kind = "table"
            try:
                artifact_ids = [table_artifact_ids[index]]
            except IndexError as exc:
                raise AnalyzePDFAdapterError(f"Missing table Artifact for {reference}") from exc
            text = None
        elif collection == "pictures":
            if not include_pictures:
                continue
            kind = "picture"
            try:
                artifact_ids = [image_artifact_ids[index]]
            except IndexError as exc:
                raise AnalyzePDFAdapterError(f"Missing image Artifact for {reference}") from exc
            text = _picture_caption(item, registry)
        else:
            continue

        element: dict[str, Any] = {
            "element_id": f"{collection[:-1]}-{index}",
            "kind": kind,
            "reading_order": len(elements),
            "provenance": provenance,
            "artifact_ids": artifact_ids,
        }
        if isinstance(text, str):
            element["text"] = text
        elements.append(element)

    if not elements:
        raise AnalyzePDFAdapterError("Docling body produced no Contract elements")
    return (
        elements,
        degenerate_bbox_expansion_count,
        formula_orig_fallback_count,
        page_bound_clamp_count,
    )


def _element_registry(native: dict[str, Any]) -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    for collection in ("texts", "tables", "pictures", "groups"):
        values = native.get(collection, [])
        if not isinstance(values, list):
            raise AnalyzePDFAdapterError(f"Docling {collection} must be an array")
        for index, value in enumerate(values):
            item = _mapping(value, f"{collection}[{index}]")
            reference = item.get("self_ref", f"#/{collection}/{index}")
            if not isinstance(reference, str) or reference in registry:
                raise AnalyzePDFAdapterError(f"Invalid or duplicate Docling ref {reference!r}")
            registry[reference] = item
    return registry


def _walk_references(
    references: list[Any], registry: dict[str, dict[str, Any]]
) -> Iterator[str]:
    for reference_value in references:
        reference_object = _mapping(reference_value, "Docling reference")
        reference = reference_object.get("$ref")
        if not isinstance(reference, str) or reference not in registry:
            raise AnalyzePDFAdapterError(f"Unknown Docling reference {reference!r}")
        collection, _ = _split_reference(reference)
        if collection == "groups":
            children = registry[reference].get("children")
            if not isinstance(children, list):
                raise AnalyzePDFAdapterError(f"Docling group {reference} has no children")
            yield from _walk_references(children, registry)
        else:
            yield reference


def _split_reference(reference: str) -> tuple[str, int]:
    parts = reference.split("/")
    if len(parts) != 3 or parts[0] != "#":
        raise AnalyzePDFAdapterError(f"Unsupported Docling reference {reference!r}")
    try:
        return parts[1], int(parts[2])
    except ValueError as exc:
        raise AnalyzePDFAdapterError(f"Unsupported Docling reference {reference!r}") from exc


def _adapt_provenance(
    value: Any,
    reference: str,
    *,
    page_sizes: dict[int, tuple[float, float]],
) -> tuple[list[dict[str, Any]], int, int]:
    if not isinstance(value, list) or not value:
        raise AnalyzePDFAdapterError(f"Docling element {reference} has no Provenance")
    records: list[dict[str, Any]] = []
    expansion_count = 0
    clamp_count = 0
    for index, value in enumerate(value):
        provenance = _mapping(value, f"{reference}.prov[{index}]")
        bbox = _mapping(provenance.get("bbox"), f"{reference}.prov[{index}].bbox")
        try:
            left = float(bbox["l"])
            top = float(bbox["t"])
            right = float(bbox["r"])
            bottom = float(bbox["b"])
            page_number = int(provenance["page_no"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalyzePDFAdapterError(f"Invalid Provenance for {reference}") from exc
        if not all(math.isfinite(number) for number in (left, top, right, bottom)):
            raise AnalyzePDFAdapterError(f"Non-finite Provenance for {reference}")
        page_size = page_sizes.get(page_number)
        if page_size is None:
            raise AnalyzePDFAdapterError(
                f"Provenance references an unknown page for {reference}"
            )
        origin = str(bbox.get("coord_origin", "BOTTOMLEFT")).upper()
        if origin not in {"BOTTOMLEFT", "TOPLEFT"}:
            raise AnalyzePDFAdapterError(f"Unsupported coordinate origin {origin!r}")
        x0, x1 = sorted((left, right))
        y0, y1 = sorted((top, bottom))
        page_width, page_height = page_size
        if (
            x0 < -_PROVENANCE_PAGE_TOLERANCE_PT
            or y0 < -_PROVENANCE_PAGE_TOLERANCE_PT
            or x1 > page_width + _PROVENANCE_PAGE_TOLERANCE_PT
            or y1 > page_height + _PROVENANCE_PAGE_TOLERANCE_PT
        ):
            raise AnalyzePDFAdapterError(f"Provenance lies outside the page for {reference}")
        clamped_x0 = min(max(x0, 0.0), page_width)
        clamped_x1 = min(max(x1, 0.0), page_width)
        clamped_y0 = min(max(y0, 0.0), page_height)
        clamped_y1 = min(max(y1, 0.0), page_height)
        if (clamped_x0, clamped_x1, clamped_y0, clamped_y1) != (x0, x1, y0, y1):
            clamp_count += 1
        x0, x1 = clamped_x0, clamped_x1
        y0, y1 = clamped_y0, clamped_y1
        x0, x1, x_expanded = _expand_degenerate_interval(
            x0, x1, limit=page_width
        )
        y0, y1, y_expanded = _expand_degenerate_interval(
            y0, y1, limit=page_height
        )
        if x_expanded or y_expanded:
            expansion_count += 1
        records.append(
            {
                "page_number": page_number,
                "bbox": {
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "unit": "pt",
                    "origin": "bottom-left" if origin == "BOTTOMLEFT" else "top-left",
                },
                "parser_element_ref": reference,
            }
        )
    return records, expansion_count, clamp_count


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
        raise AnalyzePDFAdapterError("Degenerate Provenance cannot be expanded safely")
    expanded_lower = max(0.0, lower - minimum_size / 2)
    expanded_upper = min(limit, expanded_lower + minimum_size)
    if expanded_upper - expanded_lower < minimum_size:
        expanded_lower = max(0.0, expanded_upper - minimum_size)
    if expanded_upper <= expanded_lower:
        raise AnalyzePDFAdapterError("Degenerate Provenance cannot be expanded safely")
    return expanded_lower, expanded_upper, True


def _picture_caption(
    picture: dict[str, Any], registry: dict[str, dict[str, Any]]
) -> str | None:
    captions = picture.get("captions", [])
    if not isinstance(captions, list):
        return None
    parts: list[str] = []
    for value in captions:
        reference = _mapping(value, "picture caption").get("$ref")
        item = registry.get(reference)
        if item is not None and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts) or None


def _copy_indexed_artifacts(
    source_dir: Path,
    destination_dir: Path,
    *,
    pattern: str,
    kind: str,
    id_prefix: str,
) -> list[dict[str, Any]]:
    if not source_dir.is_dir():
        return []
    paths = sorted(path for path in source_dir.glob(pattern) if path.is_file())
    if paths:
        destination_dir.mkdir(parents=True)
    artifacts: list[dict[str, Any]] = []
    for index, source in enumerate(paths):
        destination = destination_dir / source.name
        shutil.copy2(source, destination)
        artifacts.append(
            _artifact_record(f"{id_prefix}-{index}", kind, destination, destination_dir.parent)
        )
    return artifacts


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


def _require_artifact_cardinality(
    native: dict[str, Any],
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> None:
    native_tables = native.get("tables", [])
    native_pictures = native.get("pictures", [])
    if not isinstance(native_tables, list) or len(native_tables) != len(tables):
        raise AnalyzePDFAdapterError("Docling table count does not match exported CSV count")
    if not isinstance(native_pictures, list) or len(native_pictures) != len(images):
        raise AnalyzePDFAdapterError("Docling picture count does not match exported image count")


def _artifact_cardinality(
    native: dict[str, Any],
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> tuple[bool, bool]:
    native_tables = native.get("tables", [])
    native_pictures = native.get("pictures", [])
    table_complete = isinstance(native_tables, list) and len(native_tables) == len(tables)
    image_complete = isinstance(native_pictures, list) and len(native_pictures) == len(images)
    return table_complete, image_complete


def _source_media_type(native: dict[str, Any]) -> str:
    origin = _mapping(native.get("origin"), "Docling origin")
    media_type = origin.get("mimetype")
    if not isinstance(media_type, str) or not media_type:
        raise AnalyzePDFAdapterError("Docling origin.mimetype is missing")
    return media_type


def _read_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnalyzePDFAdapterError(f"Cannot read {context}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AnalyzePDFAdapterError(f"Invalid JSON in {context}: {exc.msg}") from exc
    return _mapping(value, context)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalyzePDFAdapterError(f"{context} must be an object")
    return value


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise AnalyzePDFAdapterError(f"{context} is not a valid identifier")
    return value


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, help="AnalyzePDF per-document output")
    parser.add_argument("--output-dir", required=True, help="New Contract Package directory")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--language", required=True)
    args = parser.parse_args(argv)
    try:
        contract_path = adapt_analyzepdf_output(
            args.input_dir,
            args.output_dir,
            document_id=args.document_id,
            source_ref=args.source_ref,
            language=args.language,
        )
    except (AnalyzePDFAdapterError, ContractValidationError, OSError) as exc:
        print(f"AnalyzePDF Adapter failed: {exc}", file=sys.stderr)
        return 1
    print(f"Contract Package written to {contract_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
