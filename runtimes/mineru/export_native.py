"""Run MinerU and publish a narrow, sanitized parser-native package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import importlib.metadata
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import time
from typing import Any, Iterator

from runtime_bootstrap import (
    configure_cuda_dll_directories,
    configure_fasttext_model_path,
)


OUTPUT_STATE_VERSION = 1
_IMAGE_TYPES = {"image", "chart"}
_SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class NativeExportError(RuntimeError):
    """Raised when a safe MinerU result package cannot be published."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _relative_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise NativeExportError("Generated artifact path is unsafe")
    return relative


def _inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": _relative_path(path, root),
            "byte_size": path.stat().st_size,
            "content_sha256": _file_sha256(path),
        }
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if path.name != "run.json"
    ]


def _safe_relative(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise NativeExportError(f"{context} is not a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise NativeExportError(f"{context} is not a safe relative path")
    return value


def _bbox(value: Any, *, context: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise NativeExportError(f"{context} must contain four coordinates")
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise NativeExportError(f"{context} contains a non-numeric coordinate") from exc
    if not all(math.isfinite(item) for item in result):
        raise NativeExportError(f"{context} contains a non-finite coordinate")
    if not (result[0] < result[2] and result[1] < result[3]):
        raise NativeExportError(f"{context} is degenerate or inverted")
    return result


def _content_bbox_to_pixels(
    value: Any,
    *,
    width: float,
    height: float,
    context: str,
) -> list[float]:
    x0, y0, x1, y1 = _bbox(value, context=context)
    if x0 < 0 or y0 < 0 or x1 > 1000 or y1 > 1000:
        raise NativeExportError(f"{context} lies outside normalized page bounds")
    return [
        x0 * width / 1000,
        y0 * height / 1000,
        x1 * width / 1000,
        y1 * height / 1000,
    ]


def _label(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "other")
    if item_type == "text":
        level = item.get("text_level")
        if isinstance(level, int) and level >= 1:
            return "doc_title" if level == 1 else "paragraph_title"
        return "text"
    return {
        "title": "paragraph_title",
        "paragraph": "text",
        "list": "list_item",
        "list_item": "list_item",
        "table": "table",
        "image": "image",
        "figure": "image",
        "chart": "chart",
        "equation": "formula",
        "formula": "formula",
        "header": "header",
        "page_header": "header",
        "footer": "footer",
        "page_footer": "footer",
        "number": "number",
        "page_number": "number",
    }.get(item_type, "other")


def _item_text(item: dict[str, Any]) -> str:
    for key in ("text", "content", "code_body"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    values = item.get("list_items")
    if isinstance(values, list):
        return "\n".join(str(value) for value in values if isinstance(value, str))
    if str(item.get("type") or "") == "table":
        value = item.get("table_body")
        if isinstance(value, str):
            return value
    return ""


def _caption_values(item: dict[str, Any]) -> Iterator[tuple[str, str]]:
    item_type = str(item.get("type") or "")
    fields = {
        "image": (("image_caption", "figure_title"), ("image_footnote", "caption")),
        "chart": (("image_caption", "figure_title"), ("image_footnote", "caption")),
        "table": (("table_caption", "table_title"), ("table_footnote", "caption")),
    }.get(item_type, ())
    for field, label in fields:
        values = item.get(field, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                yield label, value


def _iter_spans(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        spans = value.get("spans")
        if isinstance(spans, list):
            for span in spans:
                if isinstance(span, dict):
                    yield span
        for key, child in value.items():
            if key != "spans":
                yield from _iter_spans(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_spans(child)


def _ocr_lines(page_info: dict[str, Any]) -> list[dict[str, Any]]:
    source_blocks = page_info.get("para_blocks")
    if not isinstance(source_blocks, list):
        source_blocks = page_info.get("preproc_blocks", [])
    discarded = page_info.get("discarded_blocks", [])
    values = [source_blocks, discarded if isinstance(discarded, list) else []]
    rows: list[dict[str, Any]] = []
    for span in _iter_spans(values):
        score = span.get("score")
        text = span.get("content")
        bbox = span.get("bbox")
        if not isinstance(score, (int, float)) or not isinstance(text, str):
            continue
        if not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            continue
        try:
            parsed_bbox = _bbox(bbox, context="MinerU OCR span bbox")
        except NativeExportError:
            continue
        rows.append({"text": text, "score": float(score), "bbox": parsed_bbox})
    return rows


def _record_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    stage: str,
    message: str,
    page_number: int | None = None,
    retryable: bool = False,
) -> None:
    record: dict[str, Any] = {
        "code": code,
        "stage": stage,
        "sanitized_message": message,
        "retryable": retryable,
    }
    if page_number is not None:
        record["page_number"] = page_number
    errors.append(record)


def _copy_image(
    raw_root: Path,
    item: dict[str, Any],
    *,
    page_number: int,
    block_id: str,
    bbox: list[float],
    output_root: Path,
    image_index: int,
    errors: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    try:
        relative_source = _safe_relative(item.get("img_path"), "MinerU image path")
        source = raw_root.joinpath(*PurePosixPath(relative_source).parts)
        if not source.is_file() or source.suffix.lower() not in _SUPPORTED_IMAGE_SUFFIXES:
            raise NativeExportError("MinerU image artifact is missing or unsupported")
        relative_output = (
            f"images/page-{page_number:04d}-image-{image_index:03d}"
            f"{source.suffix.lower()}"
        )
        destination = output_root.joinpath(*PurePosixPath(relative_output).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return (
            {"artifact_path": relative_output, "block_id": block_id, "bbox": bbox},
            (relative_source, relative_output),
        )
    except Exception:
        _record_error(
            errors,
            code="IMAGE_EXPORT_INCOMPLETE",
            stage="export",
            message="A detected image could not be safely exported",
            page_number=page_number,
        )
        return None, None


def _export_table(
    item: dict[str, Any],
    *,
    page_number: int,
    block_id: str,
    bbox: list[float],
    output_root: Path,
    table_index: int,
    errors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        import pandas as pd

        html = item.get("table_body")
        if not isinstance(html, str) or not html.strip():
            raise NativeExportError("MinerU table has no HTML body")
        frames = pd.read_html(StringIO(html), header=None)
        if len(frames) != 1:
            raise NativeExportError("MinerU table HTML did not contain exactly one table")
        relative = f"tables/page-{page_number:04d}-table-{table_index:03d}.csv"
        path = output_root.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        frames[0].to_csv(path, index=False, header=False, encoding="utf-8")
        return {"artifact_path": relative, "block_id": block_id, "bbox": bbox}
    except Exception:
        _record_error(
            errors,
            code="TABLE_EXPORT_INCOMPLETE",
            stage="export",
            message="A detected table could not be safely exported",
            page_number=page_number,
        )
        return None


def _find_raw_root(raw_root: Path, *, stem: str) -> Path:
    candidates = list(raw_root.rglob(f"{stem}_middle.json"))
    if len(candidates) != 1:
        raise NativeExportError("MinerU did not produce exactly one middle JSON file")
    return candidates[0].parent


def _publish_safe_files(
    raw_root: Path,
    staging: Path,
    *,
    stem: str,
    expected_page_count: int,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    middle_path = raw_root / f"{stem}_middle.json"
    content_list_path = raw_root / f"{stem}_content_list.json"
    markdown_path = raw_root / f"{stem}.md"
    try:
        middle = json.loads(middle_path.read_text(encoding="utf-8"))
        content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeExportError("MinerU structured output is missing or invalid") from exc
    if not isinstance(middle, dict) or not isinstance(content_list, list):
        raise NativeExportError("MinerU structured output has an invalid top-level shape")
    pdf_info = middle.get("pdf_info")
    if not isinstance(pdf_info, list) or not pdf_info:
        raise NativeExportError("MinerU middle JSON contains no usable pages")

    page_by_index: dict[int, dict[str, Any]] = {}
    page_sizes: dict[int, tuple[float, float]] = {}
    for page_info in pdf_info:
        if not isinstance(page_info, dict):
            raise NativeExportError("MinerU page record is not an object")
        page_index = page_info.get("page_idx")
        size = page_info.get("page_size")
        if (
            not isinstance(page_index, int)
            or page_index < 0
            or page_index in page_by_index
            or not isinstance(size, list)
            or len(size) != 2
            or not all(isinstance(value, (int, float)) for value in size)
            or float(size[0]) <= 0
            or float(size[1]) <= 0
        ):
            raise NativeExportError("MinerU page index or page size is invalid")
        page_by_index[page_index] = page_info
        page_sizes[page_index] = (float(size[0]), float(size[1]))

    actual_indices = sorted(page_by_index)
    expected_indices = list(range(expected_page_count))
    if actual_indices != expected_indices:
        _record_error(
            errors,
            code="PAGE_COVERAGE_INCOMPLETE",
            stage="parse",
            message="MinerU output did not cover every source page",
            retryable=True,
        )

    items_by_page: dict[int, list[dict[str, Any]]] = {
        page_index: [] for page_index in actual_indices
    }
    for item in content_list:
        if not isinstance(item, dict):
            raise NativeExportError("MinerU content list entry is not an object")
        page_index = item.get("page_idx")
        if page_index not in items_by_page:
            raise NativeExportError("MinerU content list references an unknown page")
        items_by_page[page_index].append(item)

    page_records: list[dict[str, Any]] = []
    markdown_mapping: dict[str, str] = {}
    image_index = 0
    table_index = 0
    global_item_index = 0
    for page_index in actual_indices:
        page_number = page_index + 1
        width, height = page_sizes[page_index]
        blocks: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        for item in items_by_page[page_index]:
            try:
                bbox = _content_bbox_to_pixels(
                    item.get("bbox"),
                    width=width,
                    height=height,
                    context=f"MinerU page {page_number} content bbox",
                )
            except NativeExportError:
                _record_error(
                    errors,
                    code="BLOCK_BBOX_UNAVAILABLE",
                    stage="layout",
                    message="A MinerU content block had no safe page coordinates",
                    page_number=page_number,
                )
                global_item_index += 1
                continue

            block_id = f"item-{global_item_index}"
            primary = {
                "block_label": _label(item),
                "block_content": _item_text(item),
                "block_bbox": bbox,
                "block_id": block_id,
                "block_order": len(blocks),
            }
            blocks.append(primary)

            item_type = str(item.get("type") or "")
            if item_type in _IMAGE_TYPES:
                image, mapping = _copy_image(
                    raw_root,
                    item,
                    page_number=page_number,
                    block_id=block_id,
                    bbox=bbox,
                    output_root=staging,
                    image_index=image_index,
                    errors=errors,
                )
                if image is not None:
                    images.append(image)
                    image_index += 1
                if mapping is not None:
                    markdown_mapping[mapping[0]] = mapping[1]
            elif item_type == "table":
                table = _export_table(
                    item,
                    page_number=page_number,
                    block_id=block_id,
                    bbox=bbox,
                    output_root=staging,
                    table_index=table_index,
                    errors=errors,
                )
                if table is not None:
                    tables.append(table)
                    table_index += 1

            for caption_label, caption in _caption_values(item):
                blocks.append(
                    {
                        "block_label": caption_label,
                        "block_content": caption,
                        "block_bbox": bbox,
                        "block_id": f"{block_id}-caption-{len(blocks)}",
                        "block_order": len(blocks),
                    }
                )
            global_item_index += 1

        if not blocks:
            _record_error(
                errors,
                code="PAGE_CONTENT_UNAVAILABLE",
                stage="parse",
                message="A MinerU page had no safely exportable content blocks",
                page_number=page_number,
            )

        page_json = {
            "page_index": page_index,
            "page_count": expected_page_count,
            "width": width,
            "height": height,
            "model_settings": {
                "backend": str(middle.get("_backend") or "unknown"),
                "effort": str(middle.get("_effort") or "unknown"),
                "ocr_enable": bool(middle.get("_ocr_enable")),
            },
            "parsing_res_list": blocks,
            "ocr_lines": _ocr_lines(page_by_index[page_index]),
        }
        json_relative = f"pages/page-{page_number:04d}.json"
        markdown_relative = f"pages/page-{page_number:04d}.md"
        _write_json(staging.joinpath(*PurePosixPath(json_relative).parts), page_json)
        page_text = "\n\n".join(
            block["block_content"] for block in blocks if block["block_content"]
        )
        markdown_output = staging.joinpath(*PurePosixPath(markdown_relative).parts)
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(page_text + ("\n" if page_text else ""), encoding="utf-8")
        page_records.append(
            {
                "page_number": page_number,
                "width": width,
                "height": height,
                "json_path": json_relative,
                "markdown_path": markdown_relative,
                "images": images,
                "tables": tables,
            }
        )

    if markdown_path.is_file():
        content = markdown_path.read_text(encoding="utf-8")
        for original, replacement in markdown_mapping.items():
            content = content.replace(original, replacement)
    else:
        content = "\n\n".join(
            staging.joinpath(*PurePosixPath(page["markdown_path"]).parts).read_text(
                encoding="utf-8"
            )
            for page in page_records
        )
    if not content.strip():
        raise NativeExportError("MinerU returned empty document text")
    (staging / "content.md").write_text(content, encoding="utf-8")
    return page_records, bool(middle.get("_ocr_enable"))


def _source_page_count(source_path: Path) -> int:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(source_path.read_bytes())
    try:
        count = len(document)
    finally:
        document.close()
    if count < 1:
        raise NativeExportError("Input PDF contains no pages")
    return count


def export_mineru_native(
    input_pdf: str | Path,
    output_dir: str | Path,
    *,
    backend: str = "pipeline",
    effort: str = "medium",
    parse_method: str = "auto",
    language: str = "ch",
    formula_enable: bool = True,
    table_enable: bool = True,
    image_analysis: bool = False,
) -> Path:
    """Run one PDF and atomically publish only sanitized, contract-ready evidence."""

    source_path = Path(input_pdf).resolve()
    output_path = Path(output_dir).resolve()
    if not source_path.is_file():
        raise NativeExportError("Input PDF is missing")
    if output_path.exists():
        raise NativeExportError("Output directory already exists")
    staging = output_path.with_name(f".{output_path.name}.staging")
    if staging.exists():
        raise NativeExportError("Staging directory already exists")
    staging.mkdir(parents=True)

    started_at = _utc_now()
    started = time.perf_counter()
    errors: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    actual_ocr = False
    status = "failed"
    raw_root = staging / ".mineru-raw"
    stem = "document"

    configure_cuda_dll_directories()
    configure_fasttext_model_path()
    try:
        from mineru.cli.common import do_parse

        expected_page_count = _source_page_count(source_path)
        raw_root.mkdir()
        do_parse(
            output_dir=str(raw_root),
            pdf_file_names=[stem],
            pdf_bytes_list=[source_path.read_bytes()],
            p_lang_list=[language],
            backend=backend,
            effort=effort,
            parse_method=parse_method,
            formula_enable=formula_enable,
            table_enable=table_enable,
            image_analysis=image_analysis,
            f_draw_layout_bbox=False,
            f_draw_span_bbox=False,
            f_dump_orig_pdf=False,
            f_dump_model_output=False,
            f_dump_md=True,
            f_dump_middle_json=True,
            f_dump_content_list=True,
        )
        parsed_root = _find_raw_root(raw_root, stem=stem)
        page_records, actual_ocr = _publish_safe_files(
            parsed_root,
            staging,
            stem=stem,
            expected_page_count=expected_page_count,
            errors=errors,
        )
        status = "partial" if errors else "succeeded"
    except Exception:
        for generated in list(staging.iterdir()):
            if generated.is_dir():
                shutil.rmtree(generated)
            else:
                generated.unlink()
        page_records = []
        errors = [
            {
                "code": "MINERU_RUN_FAILED",
                "stage": "parse",
                "sanitized_message": "MinerU did not produce a usable document",
                "retryable": False,
            }
        ]
        status = "failed"
    finally:
        if raw_root.exists():
            shutil.rmtree(raw_root)

    mineru_version = importlib.metadata.version("mineru")
    engine_name, engine_version = (
        ("PDF-Extract-Kit", "1.0")
        if backend == "pipeline"
        else ("MinerU2.5-Pro", "2605-1.2B")
    )
    request = {
        "backend": backend,
        "effort": effort,
        "parse_method": parse_method,
        "language": language,
        "formula_enable": formula_enable,
        "table_enable": table_enable,
        "image_analysis": image_analysis,
        "use_ocr": actual_ocr,
        "ocr_engine_name": "PaddleOCR-Torch",
        "ocr_engine_version": "PP-OCRv6",
        "ocr_low_confidence_threshold": 0.8,
    }
    run = {
        "output_state_version": OUTPUT_STATE_VERSION,
        "source": {
            "content_sha256": _file_sha256(source_path),
            "byte_size": source_path.stat().st_size,
        },
        "parser": {
            "name": "MinerU",
            "version": mineru_version,
            "engine_name": engine_name,
            "engine_version": engine_version,
            "backend": backend,
        },
        "request": request,
        "result": {
            "status": status,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "duration_ms": round((time.perf_counter() - started) * 1000),
        },
        "pages": page_records if status != "failed" else [],
        "errors": errors,
        "output_files": _inventory(staging),
    }
    _write_json(staging / "run.json", run)
    staging.replace(output_path)
    return output_path / "run.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--backend", choices=("pipeline", "hybrid-engine"), default="pipeline")
    parser.add_argument("--effort", choices=("medium", "high"), default="medium")
    parser.add_argument("--parse-method", choices=("auto", "txt", "ocr"), default="auto")
    parser.add_argument("--language", default="ch")
    parser.add_argument("--without-formulas", action="store_true")
    parser.add_argument("--without-tables", action="store_true")
    parser.add_argument("--image-analysis", action="store_true")
    args = parser.parse_args()
    try:
        run_path = export_mineru_native(
            args.input_pdf,
            args.output_dir,
            backend=args.backend,
            effort=args.effort,
            parse_method=args.parse_method,
            language=args.language,
            formula_enable=not args.without_formulas,
            table_enable=not args.without_tables,
            image_analysis=args.image_analysis,
        )
    except (NativeExportError, OSError) as exc:
        print(f"MinerU 原生包生成失败：{exc}")
        return 1
    run = json.loads(run_path.read_text(encoding="utf-8"))
    print(f"MinerU 原生包完成：status={run['result']['status']} pages={len(run['pages'])}")
    return 0 if run["result"]["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
