from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from secrets import token_urlsafe

import pytest

from analyzepdf.fixtures.pdf_edges import generate_pdf_edge_fixture_pack
from analyzepdf.quality import (
    assess_ingestion_quality,
    inspect_pdf_source,
)


@pytest.fixture(scope="module")
def edge_pack(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("quality-gate") / "pdf-edge-v1"
    generate_pdf_edge_fixture_pack(
        output,
        encryption_secret=token_urlsafe(24),
    )
    return output


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_contract(
    package: Path,
    source_pdf: Path,
    *,
    status: str = "succeeded",
    use_ocr: bool = False,
    confidence_unavailable: bool = False,
    empty_table: bool = False,
    source_sha256: str | None = None,
) -> Path:
    package.mkdir(parents=True)
    source_hash = source_sha256 or _file_sha256(source_pdf)
    quality_flags: list[str] = []
    ocr: dict[str, object] = {"used": use_ocr, "low_confidence_pages": []}
    if use_ocr:
        ocr.update({"engine_name": "rapidocr", "engine_version": "test"})
    if confidence_unavailable:
        quality_flags.append("ocr_confidence_unavailable")

    contract: dict[str, object] = {
        "contract_version": "1.0",
        "document_id": "quality-gate-fixture",
        "source": {
            "source_ref": "quality-gate-source",
            "content_sha256": source_hash,
            "media_type": "application/pdf",
            "byte_size": source_pdf.stat().st_size,
        },
        "parser_run": {
            "run_id": "run-quality-gate-fixture",
            "parser_name": "AnalyzePDF",
            "parser_version": "0.1.0",
            "engine_name": "docling",
            "engine_version": "test",
            "backend": "pypdfium2",
            "config_sha256": "c" * 64,
            "started_at": "2026-08-11T00:00:00Z",
            "completed_at": "2026-08-11T00:00:01Z",
            "duration_ms": 1000,
        },
        "status": status,
        "pages": [],
        "elements": [],
        "artifacts": [],
        "quality": {"flags": quality_flags, "ocr": ocr},
        "errors": [],
    }

    if status == "failed":
        quality_flags.append("parse_failed")
        contract["errors"] = [
            {
                "code": "PDF_ENCRYPTED",
                "stage": "parse",
                "sanitized_message": "Encrypted fixture requires a password",
                "retryable": False,
            }
        ]
    else:
        content = "\n".join(
            f"第 {page} 页已恢复的非敏感测试正文。" * 8 for page in range(1, 4)
        )
        markdown_path = package / "content.md"
        parser_path = package / "parser.json"
        markdown_path.write_text(content, encoding="utf-8")
        parser_path.write_text('{"schema_name":"quality-gate-test"}\n', encoding="utf-8")
        contract["text"] = {
            "content": content,
            "content_sha256": sha256(content.encode("utf-8")).hexdigest(),
            "language": "zh-CN",
        }
        contract["pages"] = [
            {
                "page_number": page,
                "width": 596,
                "height": 842,
                "unit": "pt",
            }
            for page in range(1, 4)
        ]
        contract["elements"] = [
            {
                "element_id": f"paragraph-{page}",
                "kind": "paragraph",
                "reading_order": page - 1,
                "text": f"第 {page} 页已恢复的非敏感测试正文。" * 8,
                "provenance": [
                    {
                        "page_number": page,
                        "bbox": {
                            "x0": 72,
                            "y0": 650,
                            "x1": 520,
                            "y1": 720,
                            "unit": "pt",
                            "origin": "bottom-left",
                        },
                        "parser_element_ref": f"#/texts/{page - 1}",
                    }
                ],
                "artifact_ids": [],
            }
            for page in range(1, 4)
        ]
        contract["artifacts"] = [
            {
                "artifact_id": "content-markdown",
                "kind": "markdown",
                "path": "content.md",
                "media_type": "text/markdown",
                "content_sha256": _file_sha256(markdown_path),
            },
            {
                "artifact_id": "parser-json",
                "kind": "structured_json",
                "path": "parser.json",
                "media_type": "application/json",
                "content_sha256": _file_sha256(parser_path),
            },
        ]
        if status == "partial":
            quality_flags.append("partial_parse")
            contract["errors"] = [
                {
                    "code": "PARSE_TIMEOUT",
                    "stage": "parse",
                    "sanitized_message": "Processing timeout preserved usable content",
                    "retryable": True,
                }
            ]

    if empty_table:
        table_path = package / "table.csv"
        table_path.write_text("\ufeff", encoding="utf-8")
        contract["artifacts"].append(
            {
                "artifact_id": "table-1",
                "kind": "table_csv",
                "path": "table.csv",
                "media_type": "text/csv",
                "content_sha256": _file_sha256(table_path),
            }
        )
        contract["elements"].append(
            {
                "element_id": "table-1",
                "kind": "table",
                "reading_order": 3,
                "provenance": [
                    {
                        "page_number": 2,
                        "bbox": {
                            "x0": 72,
                            "y0": 400,
                            "x1": 520,
                            "y1": 600,
                            "unit": "pt",
                            "origin": "bottom-left",
                        },
                        "parser_element_ref": "#/tables/0",
                    }
                ],
                "artifact_ids": ["table-1"],
            }
        )

    contract_path = package / "parsed-document.json"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return contract_path


def test_source_evidence_distinguishes_clear_and_low_resolution_scans(
    edge_pack: Path,
) -> None:
    clear = inspect_pdf_source(edge_pack / "scan-clear.pdf")
    low = inspect_pdf_source(edge_pack / "scan-low-resolution.pdf")

    assert clear["requires_ocr_pages"] == [1, 2, 3]
    assert clear["minimum_estimated_scan_dpi"] == 200.0
    assert clear["low_resolution_scan_pages"] == []
    assert low["requires_ocr_pages"] == [1, 2, 3]
    assert low["minimum_estimated_scan_dpi"] == 72.0
    assert low["low_resolution_scan_pages"] == [1, 2, 3]


def test_quality_gate_accepts_complete_digital_text(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "base-text.pdf"
    contract = _write_contract(tmp_path / "digital", source)

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "accept"
    assert report["route_action"] == "publish"
    assert report["quality_gate_version"] == 3
    assert report["reason_codes"] == []
    assert report["warning_codes"] == []
    assert report["blocking_codes"] == []
    assert report["metrics"]["cjk_internal_space_count"] == 0


def test_quality_gate_reviews_spurious_spaces_inside_chinese_text(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "base-text.pdf"
    contract = _write_contract(tmp_path / "cjk-spaces", source)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    for index, element in enumerate(payload["elements"]):
        element["text"] = ("这 是 一 段 被 版 面 换 行 打 断 的 中 文 正 文。" * 8) + str(index)
    content = "\n".join(element["text"] for element in payload["elements"])
    payload["text"]["content"] = content
    payload["text"]["content_sha256"] = sha256(content.encode("utf-8")).hexdigest()
    contract.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "review"
    assert report["route_action"] == "publish"
    assert report["reason_codes"] == ["cjk_internal_space_ratio_above_policy"]
    assert report["warning_codes"] == [
        "cjk_internal_space_ratio_above_policy"
    ]
    assert report["blocking_codes"] == []
    assert report["metrics"]["cjk_internal_space_ratio"] > 0.01


def test_quality_gate_reviews_ocr_without_confidence(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "scan-clear.pdf"
    contract = _write_contract(
        tmp_path / "clear",
        source,
        use_ocr=True,
        confidence_unavailable=True,
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "review"
    assert report["reason_codes"] == ["ocr_confidence_unavailable"]


def test_quality_gate_identifies_low_resolution_scan(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "scan-low-resolution.pdf"
    contract = _write_contract(
        tmp_path / "low",
        source,
        use_ocr=True,
        confidence_unavailable=True,
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "review"
    assert report["source"]["low_resolution_scan_pages"] == [1, 2, 3]
    assert report["source"]["minimum_estimated_scan_dpi"] == 72.0
    assert report["reason_codes"] == [
        "ocr_confidence_unavailable",
        "scan_resolution_below_policy",
    ]


def test_quality_gate_rejects_failed_encrypted_source(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "encrypted.pdf"
    contract = _write_contract(
        tmp_path / "encrypted",
        source,
        status="failed",
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "reject"
    assert report["route_action"] == "fallback"
    assert report["metrics"]["text_characters"] == 0
    assert report["reason_codes"] == ["parse_failed", "source_pdf_encrypted"]
    assert report["warning_codes"] == []
    assert report["blocking_codes"] == [
        "parse_failed",
        "source_pdf_encrypted",
    ]
    assert report["failure_stages"] == ["parse"]


def test_quality_gate_reviews_empty_table_artifact(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "base-text.pdf"
    contract = _write_contract(tmp_path / "table", source, empty_table=True)

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "review"
    assert report["metrics"]["empty_table_count"] == 1
    assert report["reason_codes"] == ["empty_table_artifact"]


def test_quality_gate_rejects_source_hash_mismatch(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "base-text.pdf"
    contract = _write_contract(
        tmp_path / "mismatch",
        source,
        source_sha256="f" * 64,
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "reject"
    assert report["reason_codes"] == ["source_hash_mismatch"]


def test_quality_gate_rejects_source_contract_page_count_mismatch(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "base-text.pdf"
    contract = _write_contract(tmp_path / "page-mismatch", source)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["pages"] = payload["pages"][:2]
    payload["elements"] = payload["elements"][:2]
    contract.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "reject"
    assert report["route_action"] == "fallback"
    assert report["reason_codes"] == ["source_contract_page_count_mismatch"]
    assert report["blocking_codes"] == [
        "source_contract_page_count_mismatch"
    ]


def test_quality_gate_reviews_partial_page_count_mismatch(
    edge_pack: Path, tmp_path: Path
) -> None:
    source = edge_pack / "base-text.pdf"
    contract = _write_contract(
        tmp_path / "partial-page-mismatch",
        source,
        status="partial",
    )
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["pages"] = payload["pages"][:1]
    payload["elements"] = payload["elements"][:1]
    contract.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = assess_ingestion_quality(contract, source)

    assert report["decision"] == "reject"
    assert report["route_action"] == "fallback"
    assert report["reason_codes"] == [
        "partial_parse",
        "partial_source_contract_page_count_mismatch",
    ]
    assert report["blocking_codes"] == [
        "partial_parse",
        "partial_source_contract_page_count_mismatch",
    ]
