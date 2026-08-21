from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyzepdf.contracts.validation import load_contract
from analyzepdf.contracts.mineru import (
    MinerUAdapterError,
    adapt_mineru_output,
)
from test_contract_ppstructure import (
    _create_ppstructure_output,
    _error,
    _refresh_inventory,
    _write_json,
)


def _create_mineru_output(root: Path, *, page_count: int = 3) -> Path:
    output = _create_ppstructure_output(root, page_count=page_count)
    run_path = output / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["parser"] = {
        "name": "MinerU",
        "version": "3.4.4",
        "engine_name": "PDF-Extract-Kit",
        "engine_version": "1.0",
        "backend": "pipeline",
    }
    _write_json(run_path, run)
    return output


def test_adapter_builds_valid_mineru_contract(tmp_path: Path) -> None:
    native = _create_mineru_output(tmp_path)
    contract_path = adapt_mineru_output(
        native,
        tmp_path / "contract",
        document_id="doc-mineru-001",
        source_ref="source-mineru-001",
        language="zh-CN",
    )

    parsed = load_contract(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert parsed.parser_name == "MinerU"
    assert parsed.page_numbers == (1, 2, 3)
    assert contract["parser_run"]["engine_name"] == "PDF-Extract-Kit"
    assert {element["kind"] for element in contract["elements"]} >= {
        "heading",
        "paragraph",
        "table",
        "picture",
    }


def test_adapter_rejects_a_different_parser_package(tmp_path: Path) -> None:
    native = _create_ppstructure_output(tmp_path, page_count=1)
    with pytest.raises(MinerUAdapterError, match="not a MinerU"):
        adapt_mineru_output(
            native,
            tmp_path / "contract",
            document_id="doc-wrong-parser",
            source_ref="source-wrong-parser",
            language="zh-CN",
        )


def test_adapter_rejects_invalid_run_json(tmp_path: Path) -> None:
    native = tmp_path / "native"
    native.mkdir()
    (native / "run.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(MinerUAdapterError, match="not valid JSON"):
        adapt_mineru_output(
            native,
            tmp_path / "contract",
            document_id="doc-invalid-json",
            source_ref="source-invalid-json",
            language="zh-CN",
        )


def test_adapter_reuses_inventory_tamper_protection(tmp_path: Path) -> None:
    native = _create_mineru_output(tmp_path)
    (native / "content.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(MinerUAdapterError, match=r"Inventory (?:size|hash) mismatch"):
        adapt_mineru_output(
            native,
            tmp_path / "contract",
            document_id="doc-tampered",
            source_ref="source-tampered",
            language="zh-CN",
        )


def test_adapter_builds_failed_contract_without_body(tmp_path: Path) -> None:
    native = tmp_path / "native"
    native.mkdir()
    run = {
        "output_state_version": 1,
        "source": {"content_sha256": "a" * 64, "byte_size": 1024},
        "parser": {
            "name": "MinerU",
            "version": "3.4.4",
            "engine_name": "PDF-Extract-Kit",
            "engine_version": "1.0",
            "backend": "pipeline",
        },
        "request": {"backend": "pipeline", "use_ocr": False},
        "result": {
            "status": "failed",
            "started_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:01Z",
            "duration_ms": 1000,
        },
        "errors": [_error("MINERU_RUN_FAILED", "parse")],
        "pages": [],
        "output_files": [],
    }
    _write_json(native / "run.json", run)

    contract_path = adapt_mineru_output(
        native,
        tmp_path / "contract",
        document_id="doc-mineru-failed",
        source_ref="source-mineru-failed",
        language="zh-CN",
    )
    parsed = load_contract(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert parsed.status == "failed"
    assert "text" not in contract
    assert contract["pages"] == []
    assert contract["elements"] == []
    assert contract["artifacts"] == []


def test_adapter_preserves_normalized_list_and_caption_kinds(tmp_path: Path) -> None:
    native = _create_mineru_output(tmp_path, page_count=1)
    page_path = native / "pages/page_0.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["parsing_res_list"].extend(
        [
            {
                "block_label": "list_item",
                "block_content": "第一项",
                "block_bbox": [50, 240, 500, 290],
                "block_id": "list-1",
                "block_order": 2,
            },
            {
                "block_label": "caption",
                "block_content": "补充说明",
                "block_bbox": [50, 300, 500, 350],
                "block_id": "caption-1",
                "block_order": 3,
            },
        ]
    )
    _write_json(page_path, page)
    run = json.loads((native / "run.json").read_text(encoding="utf-8"))
    _refresh_inventory(native, run)

    contract_path = adapt_mineru_output(
        native,
        tmp_path / "contract",
        document_id="doc-mineru-kinds",
        source_ref="source-mineru-kinds",
        language="zh-CN",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert [element["kind"] for element in contract["elements"]][-2:] == [
        "list_item",
        "caption",
    ]


def test_adapter_keeps_available_noncontiguous_pages_for_partial_run(
    tmp_path: Path,
) -> None:
    native = _create_mineru_output(tmp_path, page_count=3)
    run_path = native / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    missing_page = run["pages"].pop(1)
    for key in ("json_path", "markdown_path"):
        (native / missing_page[key]).unlink()
    for table in missing_page["tables"]:
        (native / table["artifact_path"]).unlink()
    run["result"]["status"] = "partial"
    run["errors"] = [_error("PAGE_COVERAGE_INCOMPLETE", "parse")]
    _refresh_inventory(native, run)

    contract_path = adapt_mineru_output(
        native,
        tmp_path / "contract",
        document_id="doc-mineru-partial-pages",
        source_ref="source-mineru-partial-pages",
        language="zh-CN",
    )
    parsed = load_contract(contract_path)
    assert parsed.status == "partial"
    assert parsed.page_numbers == (1, 3)
