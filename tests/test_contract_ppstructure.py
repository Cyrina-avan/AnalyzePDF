from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from analyzepdf.contracts.validation import ContractValidationError, load_contract
from analyzepdf.contracts.ppstructure import (
    PPStructureAdapterError,
    adapt_ppstructure_output,
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _error(code: str = "PDF_PARSE_FAILED", stage: str = "parse") -> dict:
    return {
        "code": code,
        "stage": stage,
        "sanitized_message": "Parser could not recover all requested content",
        "retryable": False,
    }


def _page_json(
    page_index: int,
    *,
    page_count: int,
    width: float,
    height: float,
    blocks: list[dict],
) -> dict:
    return {
        "page_index": page_index,
        "page_count": page_count,
        "width": width,
        "height": height,
        "model_settings": {"use_doc_orientation_classify": False},
        "parsing_res_list": blocks,
    }


def _block(
    label: str,
    content: str,
    bbox: list[float],
    *,
    block_id: str | int,
    block_order: int | None,
) -> dict:
    block = {
        "block_label": label,
        "block_content": content,
        "block_bbox": bbox,
        "block_id": block_id,
    }
    if block_order is not None:
        block["block_order"] = block_order
    return block


def _create_ppstructure_output(root: Path, *, page_count: int = 3) -> Path:
    output = root / "ppstructure-output"
    (output / "pages").mkdir(parents=True)
    (output / "tables").mkdir()
    (output / "images").mkdir()

    markdown_parts = ["# 三页验收报告\n"]
    pages_meta: list[dict] = []
    all_files: list[Path] = []

    for page_index in range(page_count):
        page_number = page_index + 1
        width = 1200.0
        height = 1600.0
        json_name = f"pages/page_{page_index}.json"
        md_name = f"pages/page_{page_index}.md"
        page_md = f"## 第 {page_number} 页\n\n正文内容 {page_number}\n"
        markdown_parts.append(page_md)
        (output / md_name).write_text(page_md, encoding="utf-8")

        blocks = [
            _block(
                "doc_title" if page_index == 0 else "paragraph_title",
                f"标题 {page_number}",
                [50, 50, 400, 120],
                block_id=f"title-{page_number}",
                block_order=0,
            ),
            _block(
                "text",
                f"正文内容 {page_number}",
                [50, 150, 500, 220],
                block_id=f"text-{page_number}",
                block_order=1,
            ),
        ]
        images: list[dict] = []
        tables: list[dict] = []
        if page_index == 1:
            table_path = "tables/table_page2.csv"
            (output / table_path).write_text("列A,列B\n值1,值2\n", encoding="utf-8")
            blocks.append(
                _block(
                    "table",
                    "",
                    [60, 300, 500, 500],
                    block_id="table-2",
                    block_order=2,
                )
            )
            tables.append(
                {
                    "artifact_path": table_path,
                    "block_id": "table-2",
                    "bbox": [60, 300, 500, 500],
                }
            )
        if page_index == 2:
            image_path = "images/chart_page3.png"
            (output / image_path).write_bytes(b"synthetic-chart-png")
            blocks.append(
                _block(
                    "chart",
                    "",
                    [80, 320, 420, 620],
                    block_id="chart-3",
                    block_order=2,
                )
            )
            blocks.append(
                _block(
                    "figure_title",
                    "图 1 架构示意",
                    [80, 630, 300, 680],
                    block_id="caption-3",
                    block_order=3,
                )
            )
            images.append(
                {
                    "artifact_path": image_path,
                    "block_id": "chart-3",
                    "bbox": [80, 320, 420, 620],
                }
            )

        _write_json(
            output / json_name,
            _page_json(
                page_index,
                page_count=page_count,
                width=width,
                height=height,
                blocks=blocks,
            ),
        )
        pages_meta.append(
            {
                "page_number": page_number,
                "width": width,
                "height": height,
                "json_path": json_name,
                "markdown_path": md_name,
                "images": images,
                "tables": tables,
            }
        )

    (output / "content.md").write_text("".join(markdown_parts), encoding="utf-8")

    output_files = []
    for path in sorted(file for file in output.rglob("*") if file.is_file()):
        output_files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "byte_size": path.stat().st_size,
                "content_sha256": _hash(path),
            }
        )
    run = {
        "output_state_version": 1,
        "source": {"content_sha256": "a" * 64, "byte_size": 4096},
        "parser": {
            "name": "PP-StructureV3",
            "version": "3.7.0",
            "engine_name": "paddleocr",
            "engine_version": "3.7.0",
            "backend": "paddlex",
        },
        "request": {
            "use_ocr": False,
            "layout": True,
            "table": True,
            "chart": True,
        },
        "result": {
            "status": "succeeded",
            "started_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:03Z",
            "duration_ms": 3000,
        },
        "pages": pages_meta,
        "output_files": output_files,
    }
    _write_json(output / "run.json", run)
    return output


def _refresh_inventory(output: Path, run: dict) -> None:
    run["output_files"] = []
    for path in sorted(file for file in output.rglob("*") if file.is_file()):
        if path.name == "run.json":
            continue
        run["output_files"].append(
            {
                "path": path.relative_to(output).as_posix(),
                "byte_size": path.stat().st_size,
                "content_sha256": _hash(path),
            }
        )
    _write_json(output / "run.json", run)


def test_adapter_builds_valid_three_page_contract_package(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path)
    package_dir = tmp_path / "contract-package"

    contract_path = adapt_ppstructure_output(
        input_dir,
        package_dir,
        document_id="doc-pp-001",
        source_ref="source-pp-001",
        language="zh-CN",
    )

    parsed = load_contract(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    parser_json = (package_dir / "parser.json").read_text(encoding="utf-8")

    assert parsed.document_id == "doc-pp-001"
    assert parsed.page_numbers == (1, 2, 3)
    assert contract["pages"][0]["unit"] == "px"
    assert contract["pages"][0]["page_number"] == 1
    assert contract["elements"][0]["provenance"][0]["bbox"]["origin"] == "top-left"
    kinds = [element["kind"] for element in contract["elements"]]
    assert "heading" in kinds
    assert "paragraph" in kinds
    assert "table" in kinds
    assert "picture" in kinds
    assert "caption" in kinds
    assert contract["elements"][-1]["text"] == "图 1 架构示意"
    reading_orders = [element["reading_order"] for element in contract["elements"]]
    assert reading_orders == list(range(len(reading_orders)))
    assert (package_dir / "tables/table_page2.csv").is_file()
    assert (package_dir / "images/chart_page3.png").is_file()
    assert "input_path" not in parser_json
    assert "filename" not in parser_json

    table_element = next(element for element in contract["elements"] if element["kind"] == "table")
    picture_element = next(
        element for element in contract["elements"] if element["kind"] == "picture"
    )
    assert table_element["artifact_ids"] == ["table-0"]
    assert picture_element["artifact_ids"] == ["image-0"]

    (package_dir / "content.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="content_sha256 mismatch"):
        load_contract(contract_path)


def test_adapter_maps_page_index_zero_to_page_number_one(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    assert page_json["page_index"] == 0

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-page-index",
        source_ref="source-page-index",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["pages"][0]["page_number"] == 1
    assert contract["elements"][0]["provenance"][0]["page_number"] == 1


def test_adapter_rejects_tampered_inventory_hash(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path)
    (input_dir / "content.md").write_text("tampered markdown", encoding="utf-8")

    with pytest.raises(PPStructureAdapterError, match="Inventory .* mismatch"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-tamper",
            source_ref="source-tamper",
            language="zh-CN",
        )


def test_adapter_rejects_unsafe_inventory_path(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["output_files"][0]["path"] = "../escape/content.md"
    _write_json(input_dir / "run.json", run)

    with pytest.raises(PPStructureAdapterError, match="Unsafe inventory path"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-unsafe",
            source_ref="source-unsafe",
            language="zh-CN",
        )


def test_adapter_rejects_sensitive_keys_in_page_json(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json["input_path"] = "/Users/alice/secret.pdf"
    _write_json(input_dir / "pages/page_0.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Forbidden key 'input_path'"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-sensitive",
            source_ref="source-sensitive",
            language="zh-CN",
        )


def test_adapter_rejects_path_in_failed_run_error(tmp_path: Path) -> None:
    input_dir = tmp_path / "failed-output"
    input_dir.mkdir()
    run = {
        "output_state_version": 1,
        "source": {"content_sha256": "a" * 64, "byte_size": 2048},
        "parser": {
            "name": "PP-StructureV3",
            "version": "3.7.0",
            "engine_name": "paddleocr",
            "engine_version": "3.7.0",
            "backend": "paddlex",
        },
        "request": {"use_ocr": False},
        "result": {
            "status": "failed",
            "started_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:01Z",
            "duration_ms": 1000,
        },
        "errors": [
            {
                **_error(),
                "sanitized_message": "Failed at /Users/alice/private-document.pdf",
            }
        ],
        "pages": [],
        "output_files": [],
    }
    _write_json(input_dir / "run.json", run)

    with pytest.raises(PPStructureAdapterError, match="Unsafe error message"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-failed-unsafe",
            source_ref="source-failed-unsafe",
            language="zh-CN",
        )


def test_adapter_rejects_missing_artifact_on_succeeded_run(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    page_json = json.loads((input_dir / "pages/page_1.json").read_text(encoding="utf-8"))
    page_json["parsing_res_list"].append(
        _block(
            "table",
            "",
            [100, 600, 400, 800],
            block_id="table-missing",
            block_order=9,
        )
    )
    _write_json(input_dir / "pages/page_1.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    with pytest.raises(
        PPStructureAdapterError,
        match="table or picture blocks without artifacts",
    ):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-missing-artifact",
            source_ref="source-missing-artifact",
            language="zh-CN",
        )


def test_adapter_builds_partial_contract_with_degraded_table(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    page_json = json.loads((input_dir / "pages/page_1.json").read_text(encoding="utf-8"))
    page_json["parsing_res_list"].append(
        _block(
            "table",
            "",
            [100, 600, 400, 800],
            block_id="table-missing",
            block_order=9,
        )
    )
    _write_json(input_dir / "pages/page_1.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["result"]["status"] = "partial"
    run["errors"] = [_error("TABLE_EXPORT_FAILED", "export")]
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-partial",
        source_ref="source-partial",
        language="zh-CN",
    )

    parsed = load_contract(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert parsed.status == "partial"
    assert contract["errors"] == run["errors"]
    assert "partial_parse" in contract["quality"]["flags"]
    assert "artifact_mapping_degraded" in contract["quality"]["flags"]
    degraded = [
        element
        for element in contract["elements"]
        if element["kind"] == "other"
        and "table-missing" in element["provenance"][0]["parser_element_ref"]
    ]
    assert len(degraded) == 1


def test_adapter_builds_failed_contract_without_body(tmp_path: Path) -> None:
    input_dir = tmp_path / "failed-output"
    input_dir.mkdir()
    run = {
        "output_state_version": 1,
        "source": {"content_sha256": "a" * 64, "byte_size": 2048},
        "parser": {
            "name": "PP-StructureV3",
            "version": "3.7.0",
            "engine_name": "paddleocr",
            "engine_version": "3.7.0",
            "backend": "paddlex",
        },
        "request": {"use_ocr": False},
        "result": {
            "status": "failed",
            "started_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:01Z",
            "duration_ms": 1000,
        },
        "errors": [_error()],
        "pages": [],
        "output_files": [],
    }
    _write_json(input_dir / "run.json", run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-failed",
        source_ref="source-failed",
        language="zh-CN",
    )

    parsed = load_contract(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert parsed.status == "failed"
    assert "text" not in contract
    assert contract["pages"] == []
    assert contract["elements"] == []
    assert contract["artifacts"] == []
    assert contract["errors"] == run["errors"]


def test_adapter_publishes_output_atomically(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    package_dir = tmp_path / "contract-package"
    staging_dir = tmp_path / ".contract-package.staging"

    contract_path = adapt_ppstructure_output(
        input_dir,
        package_dir,
        document_id="doc-atomic",
        source_ref="source-atomic",
        language="zh-CN",
    )

    assert contract_path.is_file()
    assert package_dir.is_dir()
    assert not staging_dir.exists()


def test_adapter_rejects_existing_output_directory(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    package_dir = tmp_path / "contract-package"
    package_dir.mkdir()

    with pytest.raises(PPStructureAdapterError, match="already exists"):
        adapt_ppstructure_output(
            input_dir,
            package_dir,
            document_id="doc-existing",
            source_ref="source-existing",
            language="zh-CN",
        )


def test_adapter_privacy_scan_removes_sensitive_parser_fields(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json["model_settings"]["filename"] = "customer-secret.pdf"
    page_json["model_settings"]["absolute_path"] = "C:/secrets/customer-secret.pdf"
    _write_json(input_dir / "pages/page_0.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-privacy",
        source_ref="source-privacy",
        language="zh-CN",
    )

    parser_json = (contract_path.parent / "parser.json").read_text(encoding="utf-8")
    contract_text = contract_path.read_text(encoding="utf-8")
    assert "customer-secret.pdf" not in parser_json
    assert "customer-secret.pdf" not in contract_text
    assert "C:/secrets" not in parser_json
    assert "absolute_path" not in parser_json
    assert "filename" not in parser_json


def test_adapter_element_ids_are_page_unique_for_duplicate_block_ids(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    for page_index in range(2):
        page_json = json.loads((input_dir / f"pages/page_{page_index}.json").read_text(encoding="utf-8"))
        page_json["parsing_res_list"] = [
            _block("text", f"正文 {page_index + 1}", [50, 150, 500, 220], block_id=0, block_order=0),
            _block("header", "页眉", [50, 10, 400, 40], block_id="hdr-shared", block_order=None),
        ]
        _write_json(input_dir / f"pages/page_{page_index}.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-element-ids",
        source_ref="source-element-ids",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    element_ids = [element["element_id"] for element in contract["elements"]]
    assert element_ids == [
        "block-1-0",
        "block-1-hdr-shared",
        "block-2-0",
        "block-2-hdr-shared",
    ]
    assert len(set(element_ids)) == len(element_ids)


def test_adapter_preserves_list_position_when_block_order_is_incomplete(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json["parsing_res_list"] = [
        _block("text", "表格前正文", [50, 150, 500, 220], block_id="text-before", block_order=1),
        _block("figure_title", "中间的无编号块", [50, 250, 500, 300], block_id="middle", block_order=None),
        _block("text", "表格后正文", [50, 350, 500, 420], block_id="text-after", block_order=2),
        _block("number", "1", [1100, 1550, 1180, 1580], block_id="num-1", block_order=None),
    ]
    _write_json(input_dir / "pages/page_0.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-null-order",
        source_ref="source-null-order",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    ordered_kinds = [element["kind"] for element in contract["elements"]]
    assert ordered_kinds == ["paragraph", "caption", "paragraph", "page_number"]
    reading_orders = [element["reading_order"] for element in contract["elements"]]
    assert reading_orders == list(range(len(reading_orders)))


def test_adapter_computes_ocr_confidence_from_page_json(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["request"]["use_ocr"] = True
    run["request"]["ocr_engine_name"] = "custom-ocr"
    run["request"]["ocr_engine_version"] = "2.1.0"
    run["request"]["ocr_low_confidence_threshold"] = 0.75
    for page_index in range(2):
        page_json = json.loads((input_dir / f"pages/page_{page_index}.json").read_text(encoding="utf-8"))
        page_json["ocr_lines"] = [
            {"score": 0.9, "text": "高置信"},
            {"score": 0.5, "text": "低置信"},
        ]
        _write_json(input_dir / f"pages/page_{page_index}.json", page_json)
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-ocr",
        source_ref="source-ocr",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    ocr = contract["quality"]["ocr"]
    assert ocr["engine_name"] == "custom-ocr"
    assert ocr["engine_version"] == "2.1.0"
    assert ocr["mean_confidence"] == pytest.approx(0.7)
    assert ocr["low_confidence_pages"] == [1, 2]
    assert "ocr_confidence_unavailable" not in contract["quality"]["flags"]


def test_adapter_keeps_ocr_confidence_unavailable_without_scores(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["request"]["use_ocr"] = True
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-ocr-missing",
        source_ref="source-ocr-missing",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert "ocr_confidence_unavailable" in contract["quality"]["flags"]
    assert "mean_confidence" not in contract["quality"]["ocr"]


def test_adapter_rejects_invalid_ocr_score_and_threshold(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["request"]["use_ocr"] = True
    run["request"]["ocr_low_confidence_threshold"] = 1.2
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Confidence score out of range"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-ocr-threshold",
            source_ref="source-ocr-threshold",
            language="zh-CN",
        )

    run["request"]["ocr_low_confidence_threshold"] = 0.8
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json["ocr_lines"] = [{"score": 1.5, "text": "非法"}]
    _write_json(input_dir / "pages/page_0.json", page_json)
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Confidence score out of range"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package-2",
            document_id="doc-ocr-score",
            source_ref="source-ocr-score",
            language="zh-CN",
        )


def test_adapter_rejects_duplicate_artifact_binding(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    table_path = "tables/dup_table.csv"
    (input_dir / table_path).write_text("A,B\n1,2\n", encoding="utf-8")
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["pages"][1]["tables"].append(
        {
            "artifact_path": table_path,
            "block_id": "table-2",
            "bbox": [60, 300, 500, 500],
        }
    )
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Duplicate artifact binding"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-dup-artifact",
            source_ref="source-dup-artifact",
            language="zh-CN",
        )


def test_adapter_rejects_non_csv_table_and_unsupported_image_suffix(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    bad_table = "tables/bad_table.json"
    (input_dir / bad_table).write_text("{}", encoding="utf-8")
    (input_dir / "tables/table_page2.csv").unlink()
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["pages"][1]["tables"][0]["artifact_path"] = bad_table
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Table artifact must be .csv"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-bad-table",
            source_ref="source-bad-table",
            language="zh-CN",
        )

    input_dir = _create_ppstructure_output(tmp_path / "image-case", page_count=3)
    bad_image = "images/chart_page3.gif"
    (input_dir / bad_image).write_bytes(b"gif")
    (input_dir / "images/chart_page3.png").unlink()
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["pages"][2]["images"][0]["artifact_path"] = bad_image
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Unsupported image artifact suffix"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package-2",
            document_id="doc-bad-image",
            source_ref="source-bad-image",
            language="zh-CN",
        )


def test_adapter_rejects_artifact_destination_conflict(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=2)
    page_one_table = "tables/nested/shared_name.csv"
    page_two_table = "tables/alt/shared_name.csv"
    (input_dir / page_one_table).parent.mkdir(parents=True)
    (input_dir / page_two_table).parent.mkdir(parents=True)
    (input_dir / page_one_table).write_text("A,B\n1,2\n", encoding="utf-8")
    (input_dir / page_two_table).write_text("A,B\n3,4\n", encoding="utf-8")
    if (input_dir / "tables/table_page2.csv").is_file():
        (input_dir / "tables/table_page2.csv").unlink()
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["pages"][0]["tables"] = [
        {
            "artifact_path": page_one_table,
            "block_id": "table-page1",
            "bbox": [60, 300, 500, 500],
        }
    ]
    run["pages"][1]["tables"] = [
        {
            "artifact_path": page_two_table,
            "block_id": "table-page2",
            "bbox": [60, 300, 500, 500],
        }
    ]
    page_json_0 = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json_0["parsing_res_list"].append(
        _block("table", "", [60, 300, 500, 500], block_id="table-page1", block_order=5)
    )
    page_json_1 = json.loads((input_dir / "pages/page_1.json").read_text(encoding="utf-8"))
    page_json_1["parsing_res_list"].append(
        _block("table", "", [60, 300, 500, 500], block_id="table-page2", block_order=5)
    )
    _write_json(input_dir / "pages/page_0.json", page_json_0)
    _write_json(input_dir / "pages/page_1.json", page_json_1)
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Artifact destination conflict"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-dest-conflict",
            source_ref="source-dest-conflict",
            language="zh-CN",
        )


def test_adapter_rejects_inverted_bbox(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json["parsing_res_list"][0]["block_bbox"] = [500, 50, 50, 120]
    _write_json(input_dir / "pages/page_0.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    with pytest.raises(PPStructureAdapterError, match="Inverted bbox"):
        adapt_ppstructure_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-inverted",
            source_ref="source-inverted",
            language="zh-CN",
        )


def test_adapter_expands_zero_width_bbox_and_flags_once(tmp_path: Path) -> None:
    input_dir = _create_ppstructure_output(tmp_path, page_count=1)
    page_json = json.loads((input_dir / "pages/page_0.json").read_text(encoding="utf-8"))
    page_json["parsing_res_list"][0]["block_bbox"] = [100, 100, 100, 200]
    page_json["parsing_res_list"][1]["block_bbox"] = [200, 200, 300, 200]
    _write_json(input_dir / "pages/page_0.json", page_json)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    contract_path = adapt_ppstructure_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-degenerate",
        source_ref="source-degenerate",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    flags = contract["quality"]["flags"]
    assert flags.count("bbox_degenerate_expanded") == 1
    bbox = contract["elements"][0]["provenance"][0]["bbox"]
    assert bbox["x1"] - bbox["x0"] == pytest.approx(0.5)
    bbox2 = contract["elements"][1]["provenance"][0]["bbox"]
    assert bbox2["y1"] - bbox2["y0"] == pytest.approx(0.5)
