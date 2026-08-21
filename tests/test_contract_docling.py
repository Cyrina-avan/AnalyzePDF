from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from analyzepdf.contracts.docling import (
    AnalyzePDFAdapterError,
    adapt_analyzepdf_output,
)
from analyzepdf.contracts.validation import ContractValidationError, load_contract


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _create_analyzepdf_output(root: Path) -> Path:
    output = root / "analyzepdf-output"
    (output / "tables").mkdir(parents=True)
    (output / "artifacts").mkdir()
    markdown = "# 验收报告\n\n- 功能通过\n\n![架构图](artifacts/image_000000_demo.png)\n"
    (output / "content.md").write_text(markdown, encoding="utf-8")
    (output / "source.txt").write_text(
        "sample.pdf\nsha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    (output / "tables/table_001.csv").write_text("项目,状态\n功能,通过\n", encoding="utf-8")
    (output / "artifacts/image_000000_demo.png").write_bytes(b"synthetic-png")

    bbox = {"l": 10, "t": 90, "r": 100, "b": 70, "coord_origin": "BOTTOMLEFT"}
    native = {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "name": "sample",
        "origin": {
            "mimetype": "application/pdf",
            "filename": "private-customer-name.pdf",
            "binary_hash": 123,
        },
        "pages": {"1": {"page_no": 1, "size": {"width": 595, "height": 842}}},
        "body": {
            "self_ref": "#/body",
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/groups/0"},
                {"$ref": "#/tables/0"},
                {"$ref": "#/pictures/0"},
            ],
        },
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "section_header",
                "text": "验收报告",
                "prov": [{"page_no": 1, "bbox": bbox}],
            },
            {
                "self_ref": "#/texts/1",
                "label": "list_item",
                "text": "功能通过",
                "prov": [{"page_no": 1, "bbox": bbox}],
            },
            {
                "self_ref": "#/texts/2",
                "label": "caption",
                "text": "架构图",
                "prov": [{"page_no": 1, "bbox": bbox}],
            },
        ],
        "groups": [
            {
                "self_ref": "#/groups/0",
                "label": "list",
                "children": [{"$ref": "#/texts/1"}],
            }
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "label": "table",
                "prov": [{"page_no": 1, "bbox": bbox}],
                "data": {"num_rows": 2, "num_cols": 2},
            }
        ],
        "pictures": [
            {
                "self_ref": "#/pictures/0",
                "label": "picture",
                "prov": [{"page_no": 1, "bbox": bbox}],
                "captions": [{"$ref": "#/texts/2"}],
                "image": {
                    "mimetype": "image/png",
                    "uri": "data:image/png;base64,c2VjcmV0",
                },
            }
        ],
    }
    _write_json(output / "content.json", native)

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
        "source": {"content_sha256": "a" * 64, "byte_size": 2048},
        "parser": {
            "name": "AnalyzePDF",
            "version": "0.1.0",
            "engine_name": "docling",
            "engine_version": "2.112.0",
            "backend": "docling-parse",
            "source_sha256": "b" * 64,
        },
        "request": {
            "use_ocr": False,
            "output_options": {
                "write_json": True,
                "write_tables": True,
                "export_images": True,
                "images_scale": 3.0,
            },
        },
        "result": {
            "status": "succeeded",
            "started_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:01Z",
            "duration_ms": 1000,
        },
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


def _error(code: str = "PDF_PARSE_FAILED", stage: str = "parse") -> dict:
    return {
        "code": code,
        "stage": stage,
        "sanitized_message": "Parser could not recover all requested content",
        "retryable": False,
    }


def test_adapter_builds_valid_sanitized_contract_package(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    package_dir = tmp_path / "contract-package"

    contract_path = adapt_analyzepdf_output(
        input_dir,
        package_dir,
        document_id="doc-001",
        source_ref="source-001",
        language="zh-CN",
    )

    parsed = load_contract(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    parser_json = (package_dir / "parser.json").read_text(encoding="utf-8")
    assert parsed.document_id == "doc-001"
    assert parsed.page_numbers == (1,)
    assert [element["kind"] for element in contract["elements"]] == [
        "heading",
        "list_item",
        "table",
        "picture",
    ]
    assert contract["elements"][-1]["text"] == "架构图"
    assert "private-customer-name.pdf" not in parser_json
    assert '"name": "sample"' not in parser_json
    assert "data:image" not in parser_json
    assert (package_dir / "tables/table_001.csv").is_file()
    assert (package_dir / "artifacts/image_000000_demo.png").is_file()

    (package_dir / "content.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="content_sha256 mismatch"):
        load_contract(contract_path)


def test_adapter_expands_in_page_zero_width_bbox_and_keeps_raw_evidence(
    tmp_path: Path,
) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    native_path = input_dir / "content.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    native["texts"][0]["prov"][0]["bbox"].update({"l": 10, "r": 10})
    _write_json(native_path, native)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    package_dir = tmp_path / "contract-package"
    contract_path = adapt_analyzepdf_output(
        input_dir,
        package_dir,
        document_id="doc-zero-width",
        source_ref="source-zero-width",
        language="zh-CN",
    )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    bbox = contract["elements"][0]["provenance"][0]["bbox"]
    parser_json = json.loads((package_dir / "parser.json").read_text(encoding="utf-8"))
    assert bbox["x1"] - bbox["x0"] == pytest.approx(0.5)
    assert "degenerate_bbox_expanded" in contract["quality"]["flags"]
    assert parser_json["texts"][0]["prov"][0]["bbox"]["l"] == 10
    assert parser_json["texts"][0]["prov"][0]["bbox"]["r"] == 10


def test_adapter_rejects_out_of_page_degenerate_bbox(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    native_path = input_dir / "content.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    native["texts"][0]["prov"][0]["bbox"].update({"l": -1, "r": -1})
    _write_json(native_path, native)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    with pytest.raises(AnalyzePDFAdapterError, match="outside the page"):
        adapt_analyzepdf_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-outside",
            source_ref="source-outside",
            language="zh-CN",
        )


def test_adapter_clamps_subpoint_page_rounding_and_keeps_raw_evidence(
    tmp_path: Path,
) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    native_path = input_dir / "content.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    page_width = native["pages"]["1"]["size"]["width"]
    native["texts"][0]["prov"][0]["bbox"].update(
        {"l": 10, "r": page_width + 0.11}
    )
    _write_json(native_path, native)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    package_dir = tmp_path / "contract-package"
    contract_path = adapt_analyzepdf_output(
        input_dir,
        package_dir,
        document_id="doc-page-rounding",
        source_ref="source-page-rounding",
        language="zh-CN",
    )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    bbox = contract["elements"][0]["provenance"][0]["bbox"]
    parser_json = json.loads((package_dir / "parser.json").read_text(encoding="utf-8"))
    assert bbox["x1"] == pytest.approx(page_width)
    assert "bbox_clamped_to_page" in contract["quality"]["flags"]
    assert parser_json["texts"][0]["prov"][0]["bbox"]["r"] == pytest.approx(
        page_width + 0.11
    )


def test_adapter_uses_formula_orig_when_formula_text_is_empty(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    native_path = input_dir / "content.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    native["texts"][0].update(
        {
            "label": "formula",
            "text": "",
            "orig": "E = mc² (1)",
        }
    )
    _write_json(native_path, native)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(input_dir, run)

    contract_path = adapt_analyzepdf_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-formula",
        source_ref="source-formula",
        language="en",
    )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["elements"][0]["kind"] == "formula"
    assert contract["elements"][0]["text"] == "E = mc² (1)"
    assert "formula_text_from_orig" in contract["quality"]["flags"]


def test_adapter_rejects_slim_output(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    run_path = input_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["request"]["output_options"]["write_json"] = False
    _write_json(run_path, run)

    with pytest.raises(AnalyzePDFAdapterError, match="requires AnalyzePDF full output"):
        adapt_analyzepdf_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-001",
            source_ref="source-001",
            language="zh-CN",
        )


def test_adapter_rejects_tampered_analyzepdf_artifact(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    (input_dir / "content.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(AnalyzePDFAdapterError, match="Inventory .* mismatch"):
        adapt_analyzepdf_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-001",
            source_ref="source-001",
            language="zh-CN",
        )


def test_adapter_rejects_managed_file_missing_from_inventory(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    (input_dir / "artifacts/image_999999_untracked.png").write_bytes(b"untracked")

    with pytest.raises(AnalyzePDFAdapterError, match="inventory does not match"):
        adapt_analyzepdf_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-001",
            source_ref="source-001",
            language="zh-CN",
        )


def test_adapter_builds_partial_contract(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    run_path = input_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["result"]["status"] = "partial"
    run["errors"] = [_error()]
    _write_json(run_path, run)

    contract_path = adapt_analyzepdf_output(
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


def test_partial_adapter_omits_zero_sized_page_placeholders(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    native_path = input_dir / "content.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    native["pages"]["2"] = {
        "page_no": 2,
        "size": {"width": 0, "height": 0},
    }
    _write_json(native_path, native)
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["result"]["status"] = "partial"
    run["errors"] = [_error("PARSE_TIMEOUT")]
    _refresh_inventory(input_dir, run)

    contract_path = adapt_analyzepdf_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-partial-pages",
        source_ref="source-partial-pages",
        language="zh-CN",
    )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["status"] == "partial"
    assert [page["page_number"] for page in contract["pages"]] == [1]


def test_partial_adapter_omits_tables_when_csv_export_is_incomplete(tmp_path: Path) -> None:
    input_dir = _create_analyzepdf_output(tmp_path)
    (input_dir / "tables/table_001.csv").unlink()
    run = json.loads((input_dir / "run.json").read_text(encoding="utf-8"))
    run["result"]["status"] = "partial"
    run["errors"] = [_error("TABLE_EXPORT_FAILED", "export")]
    _refresh_inventory(input_dir, run)

    contract_path = adapt_analyzepdf_output(
        input_dir,
        tmp_path / "contract-package",
        document_id="doc-partial-table",
        source_ref="source-partial-table",
        language="zh-CN",
    )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["status"] == "partial"
    assert "table" not in {element["kind"] for element in contract["elements"]}
    assert "picture" in {element["kind"] for element in contract["elements"]}


def test_adapter_builds_failed_contract_without_source_artifacts(tmp_path: Path) -> None:
    input_dir = tmp_path / "failed-output"
    input_dir.mkdir()
    run = {
        "output_state_version": 1,
        "source": {"content_sha256": "a" * 64, "byte_size": 2048},
        "parser": {
            "name": "AnalyzePDF",
            "version": "0.1.0",
            "engine_name": "docling",
            "engine_version": "2.112.0",
            "backend": "docling-parse+pypdfium2",
            "source_sha256": "b" * 64,
        },
        "request": {
            "use_ocr": False,
            "output_options": {
                "write_json": False,
                "write_tables": False,
                "export_images": False,
                "images_scale": 1.0,
            },
        },
        "result": {
            "status": "failed",
            "started_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:01Z",
            "duration_ms": 1000,
        },
        "errors": [_error()],
        "output_files": [],
    }
    _write_json(input_dir / "run.json", run)

    contract_path = adapt_analyzepdf_output(
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


def test_adapter_rejects_path_in_failed_run_error(tmp_path: Path) -> None:
    input_dir = tmp_path / "failed-output"
    input_dir.mkdir()
    run = {
        "output_state_version": 1,
        "source": {"content_sha256": "a" * 64, "byte_size": 2048},
        "parser": {
            "name": "AnalyzePDF",
            "version": "0.1.0",
            "engine_name": "docling",
            "engine_version": "2.112.0",
            "backend": "docling-parse+pypdfium2",
            "source_sha256": "b" * 64,
        },
        "request": {"use_ocr": False, "output_options": {}},
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
        "output_files": [],
    }
    _write_json(input_dir / "run.json", run)

    with pytest.raises(AnalyzePDFAdapterError, match="Unsafe error message"):
        adapt_analyzepdf_output(
            input_dir,
            tmp_path / "contract-package",
            document_id="doc-failed",
            source_ref="source-failed",
            language="zh-CN",
        )
