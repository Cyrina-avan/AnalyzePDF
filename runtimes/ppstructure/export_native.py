"""Run PP-StructureV3 and publish a narrow, sanitized native result package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import shutil
import time
from typing import Any

from runtime_bootstrap import configure_cuda_dll_directories


OUTPUT_STATE_VERSION = 1
_IMAGE_LABELS = {"image", "chart"}


class NativeExportError(RuntimeError):
    """Raised when a safe native package cannot be published."""


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
        raise NativeExportError("Unsafe generated artifact path")
    return relative


def _inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "run.json":
            continue
        rows.append(
            {
                "path": _relative_path(path, root),
                "byte_size": path.stat().st_size,
                "content_sha256": _file_sha256(path),
            }
        )
    return rows


def _bbox(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise NativeExportError("Parser returned an invalid bounding box")
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        result = [float(item) for item in value]
    elif len(value) >= 2 and all(
        isinstance(item, (list, tuple)) and len(item) == 2 for item in value
    ):
        result = [
            min(float(item[0]) for item in value),
            min(float(item[1]) for item in value),
            max(float(item[0]) for item in value),
            max(float(item[1]) for item in value),
        ]
    else:
        raise NativeExportError("Parser returned an invalid bounding box")
    if not (result[0] < result[2] and result[1] < result[3]):
        raise NativeExportError("Parser returned a degenerate bounding box")
    return result


def _safe_model_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (bool, int, float, str)) or item is None:
            safe[str(key)] = item
        elif isinstance(item, list) and all(isinstance(row, str) for row in item):
            safe[str(key)] = list(item)
    return safe


def _blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise NativeExportError("Parser result is missing parsing_res_list")
    rows = []
    for index, block in enumerate(value):
        if not isinstance(block, dict):
            raise NativeExportError("Parser returned a non-object layout block")
        block_id = block.get("block_id")
        if not isinstance(block_id, int):
            block_id = index
        parser_order = block.get("block_order")
        rows.append(
            {
                "block_label": str(block.get("block_label") or "other"),
                "block_content": str(block.get("block_content") or ""),
                "block_bbox": _bbox(block.get("block_bbox")),
                "block_id": block_id,
                # PaddleX already returns parsing_res_list in its reconstructed
                # document sequence, but leaves tables, captions, headers, and
                # page numbers without block_order.  Normalize the complete
                # sequence here so downstream code never pushes those blocks to
                # the end of the page.
                "block_order": index,
                "parser_block_order": (
                    parser_order if isinstance(parser_order, int) else None
                ),
            }
        )
    return rows


def _layout_boxes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("boxes"), list):
        return []
    rows = []
    for box in value["boxes"]:
        if not isinstance(box, dict):
            continue
        try:
            bbox = _bbox(box.get("coordinate"))
            score = float(box.get("score"))
        except (NativeExportError, TypeError, ValueError):
            continue
        rows.append(
            {"label": str(box.get("label") or "other"), "score": score, "bbox": bbox}
        )
    return rows


def _ocr_lines(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    texts = value.get("rec_texts")
    scores = value.get("rec_scores")
    boxes = value.get("rec_boxes")
    if not isinstance(texts, list) or not isinstance(scores, list) or not isinstance(boxes, list):
        return []
    rows = []
    for text, score, box in zip(texts, scores, boxes):
        try:
            rows.append(
                {"text": str(text), "score": float(score), "bbox": _bbox(box)}
            )
        except (NativeExportError, TypeError, ValueError):
            continue
    return rows


def _intersection_over_union(left: list[float], right: list[float]) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if intersection == 0:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / (left_area + right_area - intersection)


def _matching_block_id(
    blocks: list[dict[str, Any]], bbox: list[float], *, labels: set[str]
) -> int | None:
    candidates = sorted(
        (
            (_intersection_over_union(block["block_bbox"], bbox), block["block_id"])
            for block in blocks
            if block["block_label"] in labels
        ),
        reverse=True,
    )
    if not candidates or candidates[0][0] < 0.75:
        return None
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _cell_union_bbox(value: Any) -> list[float]:
    if not isinstance(value, list) or not value:
        raise NativeExportError("Table result has no cell boxes")
    boxes = [_bbox(item) for item in value]
    return [
        min(item[0] for item in boxes),
        min(item[1] for item in boxes),
        max(item[2] for item in boxes),
        max(item[3] for item in boxes),
    ]


def _export_images(
    result: Any,
    *,
    page_number: int,
    blocks: list[dict[str, Any]],
    root: Path,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    records: list[dict[str, Any]] = []
    markdown_paths: dict[str, str] = {}
    images = result.get("imgs_in_doc", [])
    if not isinstance(images, list):
        return records, markdown_paths
    for index, item in enumerate(images):
        if not isinstance(item, dict) or item.get("img") is None:
            continue
        try:
            bbox = _bbox(item.get("coordinate"))
            block_id = _matching_block_id(blocks, bbox, labels=_IMAGE_LABELS)
            if block_id is None:
                raise NativeExportError("Image could not be bound to exactly one block")
            relative = f"images/page-{page_number:04d}-image-{index:03d}.png"
            path = root.joinpath(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            item["img"].save(path, format="PNG")
            records.append(
                {
                    "artifact_path": relative,
                    "block_id": block_id,
                    "bbox": bbox,
                    "label": str(item.get("label") or "image"),
                }
            )
            native_path = item.get("path")
            if isinstance(native_path, str) and native_path:
                markdown_paths[native_path.replace("\\", "/")] = relative
        except Exception:
            errors.append(
                {
                    "code": "IMAGE_EXPORT_INCOMPLETE",
                    "stage": "export",
                    "sanitized_message": "A detected image could not be safely exported",
                    "retryable": False,
                    "page_number": page_number,
                }
            )
    return records, markdown_paths


def _export_tables(
    result: Any,
    *,
    page_number: int,
    blocks: list[dict[str, Any]],
    root: Path,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    import pandas as pd

    records: list[dict[str, Any]] = []
    try:
        html_rows = result.html
    except Exception:
        html_rows = {}
    table_results = result.get("table_res_list", [])
    if not isinstance(html_rows, dict) or not isinstance(table_results, list):
        return records
    for index, table_result in enumerate(table_results):
        try:
            region_id = int(table_result.get("table_region_id"))
            html = html_rows[f"table_{region_id}"]
            bbox = _cell_union_bbox(table_result.get("cell_box_list"))
            block_id = _matching_block_id(blocks, bbox, labels={"table"})
            if block_id is None:
                raise NativeExportError("Table could not be bound to exactly one block")
            frames = pd.read_html(StringIO(str(html)), header=0)
            if len(frames) != 1:
                raise NativeExportError("Table export did not contain exactly one table")
            relative = f"tables/page-{page_number:04d}-table-{index:03d}.csv"
            path = root.joinpath(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            frames[0].to_csv(path, index=False, encoding="utf-8")
            records.append(
                {
                    "artifact_path": relative,
                    "block_id": block_id,
                    "bbox": bbox,
                }
            )
        except Exception:
            errors.append(
                {
                    "code": "TABLE_EXPORT_INCOMPLETE",
                    "stage": "export",
                    "sanitized_message": "A detected table could not be safely exported",
                    "retryable": False,
                    "page_number": page_number,
                }
            )
    return records


def _replace_markdown_paths(text: str, mapping: dict[str, str], *, page_view: bool) -> str:
    for original, relative in mapping.items():
        replacement = f"../{relative}" if page_view else relative
        text = text.replace(original, replacement)
    return text


def export_ppstructure_native(
    input_pdf: str | Path,
    output_dir: str | Path,
    *,
    device: str = "gpu:0",
    use_table_recognition: bool = True,
) -> Path:
    """Publish a sanitized parser-native package without Word or source paths."""

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

    pipeline_request = {
        "device": device,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "use_seal_recognition": False,
        "use_table_recognition": use_table_recognition,
        "use_formula_recognition": False,
        "use_chart_recognition": False,
        "use_region_detection": False,
        "format_block_content": False,
    }
    # PP-StructureV3 rasterizes every page and runs PP-OCR even when the PDF has
    # an embedded text layer.  Record that fact explicitly; these metadata-only
    # fields must not be forwarded as PaddleX constructor arguments.
    request = {
        **pipeline_request,
        "use_ocr": True,
        "ocr_engine_name": "PP-OCR",
        "ocr_engine_version": "v5",
        "ocr_low_confidence_threshold": 0.8,
    }
    started_at = _utc_now()
    started = time.perf_counter()
    errors: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    page_markdowns: list[dict[str, Any]] = []
    markdown_mappings: list[dict[str, str]] = []
    parser = None
    status = "failed"

    configure_cuda_dll_directories()
    try:
        from paddleocr import PPStructureV3
        from paddlex.inference import load_pipeline_config

        paddlex_config = load_pipeline_config("PP-StructureV3")
        paddlex_config["use_doc_preprocessor"] = False
        paddlex_config["use_doc_orientation_classify"] = False
        paddlex_config["use_doc_unwarping"] = False
        paddlex_config["SubPipelines"]["DocPreprocessor"][
            "use_doc_orientation_classify"
        ] = False
        paddlex_config["SubPipelines"]["DocPreprocessor"]["use_doc_unwarping"] = False
        paddlex_config["SubPipelines"]["GeneralOCR"]["use_textline_orientation"] = False
        parser = PPStructureV3(paddlex_config=paddlex_config, **pipeline_request)
        predict_settings = {
            key: value
            for key, value in pipeline_request.items()
            if key != "device"
        }
        for result in parser.predict_iter(str(source_path), **predict_settings):
            data = result.json["res"]
            page_index = int(data["page_index"])
            page_number = page_index + 1
            blocks = _blocks(data.get("parsing_res_list"))
            page_json = {
                "page_index": page_index,
                "page_count": int(data["page_count"]),
                "width": int(data["width"]),
                "height": int(data["height"]),
                "model_settings": _safe_model_settings(data.get("model_settings")),
                "parsing_res_list": blocks,
                "layout_boxes": _layout_boxes(data.get("layout_det_res")),
                "ocr_lines": _ocr_lines(data.get("overall_ocr_res")),
            }
            json_relative = f"pages/page-{page_number:04d}.json"
            markdown_relative = f"pages/page-{page_number:04d}.md"
            _write_json(staging.joinpath(*PurePosixPath(json_relative).parts), page_json)
            images, path_mapping = _export_images(
                result,
                page_number=page_number,
                blocks=blocks,
                root=staging,
                errors=errors,
            )
            tables = _export_tables(
                result,
                page_number=page_number,
                blocks=blocks,
                root=staging,
                errors=errors,
            )
            markdown = result.markdown
            markdown_text = str(markdown.get("markdown_texts") or "")
            page_path = staging.joinpath(*PurePosixPath(markdown_relative).parts)
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(
                _replace_markdown_paths(markdown_text, path_mapping, page_view=True),
                encoding="utf-8",
            )
            page_markdowns.append(markdown)
            markdown_mappings.append(path_mapping)
            page_records.append(
                {
                    "page_number": page_number,
                    "width": page_json["width"],
                    "height": page_json["height"],
                    "json_path": json_relative,
                    "markdown_path": markdown_relative,
                    "images": images,
                    "tables": tables,
                }
            )

        if not page_records:
            raise NativeExportError("Parser returned no pages")
        combined = parser.concatenate_markdown_pages(page_markdowns)
        content = str(combined.get("markdown_texts") or "")
        for mapping in markdown_mappings:
            content = _replace_markdown_paths(content, mapping, page_view=False)
        if not content.strip():
            raise NativeExportError("Parser returned empty document text")
        (staging / "content.md").write_text(content, encoding="utf-8")
        status = "partial" if errors else "succeeded"
    except Exception:
        if not page_records:
            errors = [
                {
                    "code": "PPSTRUCTURE_RUN_FAILED",
                    "stage": "parse",
                    "sanitized_message": "PP-StructureV3 did not produce a usable document",
                    "retryable": False,
                }
            ]
            status = "failed"
        else:
            # A later page may fail after earlier pages were fully exported.
            # Preserve those usable pages and construct the document view from
            # their already-sanitized Markdown files.
            fallback_parts = []
            for page_record in page_records:
                markdown_path = staging.joinpath(
                    *PurePosixPath(page_record["markdown_path"]).parts
                )
                if markdown_path.is_file():
                    fallback_parts.append(markdown_path.read_text(encoding="utf-8"))
            fallback_content = "\n\n".join(fallback_parts)
            if fallback_content.strip():
                (staging / "content.md").write_text(fallback_content, encoding="utf-8")
                errors.append(
                    {
                        "code": "PPSTRUCTURE_RUN_INCOMPLETE",
                        "stage": "parse",
                        "sanitized_message": "PP-StructureV3 stopped after producing usable pages",
                        "retryable": True,
                    }
                )
                status = "partial"
            else:
                for generated in list(staging.iterdir()):
                    if generated.is_dir():
                        shutil.rmtree(generated)
                    else:
                        generated.unlink()
                page_records.clear()
                errors = [
                    {
                        "code": "PPSTRUCTURE_RUN_FAILED",
                        "stage": "parse",
                        "sanitized_message": "PP-StructureV3 did not produce usable document text",
                        "retryable": False,
                    }
                ]
                status = "failed"
    finally:
        if parser is not None:
            try:
                parser.close()
            except Exception:
                # Releasing an already-finished pipeline must not destroy an
                # otherwise valid, sanitized result package.
                pass

    completed_at = _utc_now()
    run = {
        "output_state_version": OUTPUT_STATE_VERSION,
        "source": {
            "content_sha256": _file_sha256(source_path),
            "byte_size": source_path.stat().st_size,
        },
        "parser": {
            "name": "PP-StructureV3",
            "version": importlib.metadata.version("paddleocr"),
            "engine_name": "PaddleX",
            "engine_version": importlib.metadata.version("paddlex"),
            "backend": "paddle-gpu" if device.startswith("gpu") else "paddle-cpu",
        },
        "request": request,
        "result": {
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
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
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--without-tables", action="store_true")
    args = parser.parse_args()
    try:
        run_path = export_ppstructure_native(
            args.input_pdf,
            args.output_dir,
            device=args.device,
            use_table_recognition=not args.without_tables,
        )
    except (NativeExportError, OSError) as exc:
        print(f"PP-StructureV3 原生包生成失败：{exc}")
        return 1
    run = json.loads(run_path.read_text(encoding="utf-8"))
    print(
        "PP-StructureV3 原生包完成："
        f"status={run['result']['status']} pages={len(run['pages'])}"
    )
    return 0 if run["result"]["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
