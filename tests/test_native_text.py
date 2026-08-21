from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from analyzepdf.native_text import (
    NativeTextEvidenceError,
    extract_native_text_evidence,
    load_native_text_evidence,
)


def _write_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path))
    document.drawString(72, 760, "PAGE-ONE-TEXT")
    document.showPage()
    document.drawString(72, 760, "PAGE-TWO-TEXT")
    document.save()


def test_extracts_page_text_without_paths_or_file_name(tmp_path: Path) -> None:
    source = tmp_path / "private-customer-name.pdf"
    _write_pdf(source)
    output = tmp_path / "native-text-evidence.json"

    extract_native_text_evidence(
        source,
        output,
        document_id="doc-001",
        source_ref="source-001",
    )
    payload = load_native_text_evidence(
        output,
        expected_source_sha256=sha256(source.read_bytes()).hexdigest(),
    )
    serialized = output.read_text(encoding="utf-8")

    assert payload["status"] == "succeeded"
    assert [page["page_number"] for page in payload["pages"]] == [1, 2]
    assert "PAGE-ONE-TEXT" in payload["pages"][0]["text"]
    assert "private-customer-name.pdf" not in serialized
    assert str(tmp_path) not in serialized


def test_invalid_pdf_publishes_failed_evidence(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not-a-pdf")
    output = tmp_path / "native-text-evidence.json"

    extract_native_text_evidence(
        source,
        output,
        document_id="doc-broken",
        source_ref="source-broken",
    )
    payload = load_native_text_evidence(output)

    assert payload["status"] == "failed"
    assert payload["pages"] == []
    assert payload["errors"][0]["code"] == "PDF_TEXT_LAYER_UNREADABLE"


def test_rejects_source_hash_mismatch_and_page_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)
    output = tmp_path / "native-text-evidence.json"
    extract_native_text_evidence(
        source,
        output,
        document_id="doc-001",
        source_ref="source-001",
    )

    with pytest.raises(NativeTextEvidenceError, match="源哈希"):
        load_native_text_evidence(output, expected_source_sha256="0" * 64)

    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["pages"][0]["text"] += "tampered"
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NativeTextEvidenceError, match="页面哈希"):
        load_native_text_evidence(output)
