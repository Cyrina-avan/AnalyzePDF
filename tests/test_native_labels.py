from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from analyzepdf.native_labels import (
    NativeLabelEvidenceError,
    extract_native_label_evidence,
    load_native_label_evidence,
)


def _write_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=(600, 800))
    document.drawString(50, 700, "Table 4. Results")
    document.showPage()
    document.drawString(50, 700, "Table 4 continued")
    document.drawString(50, 300, "Fig. 7: Forest plot")
    document.save()


def test_extracts_exact_table_continuation_and_figure_labels(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)
    path = extract_native_label_evidence(
        source,
        tmp_path / "labels.json",
        document_id="doc-1",
        source_ref="source-1",
    )
    value = load_native_label_evidence(path)
    assert [(item["object_type"], item["label_number"]) for item in value["labels"]] == [
        ("table", 4),
        ("table", 4),
        ("figure", 7),
    ]
    assert [
        item.get("label_form")
        for item in value["labels"]
        if item["object_type"] == "table"
    ] == ["punctuated", "continued"]
    assert all(0 <= item["x_normalized"] <= 1 for item in value["labels"])
    assert all(0 <= item["y_normalized"] <= 1 for item in value["labels"])
    assert all(item["bbox_normalized"]["x0"] < item["bbox_normalized"]["x1"] for item in value["labels"])


def test_rejects_tampered_source_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)
    path = extract_native_label_evidence(
        source,
        tmp_path / "labels.json",
        document_id="doc-1",
        source_ref="source-1",
    )
    with pytest.raises(NativeLabelEvidenceError, match="哈希不一致"):
        load_native_label_evidence(path, expected_source_sha256="0" * 64)
