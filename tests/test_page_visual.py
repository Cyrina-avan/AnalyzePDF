from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from analyzepdf.page_visual import (
    PageVisualEvidenceError,
    extract_page_visual_evidence,
    load_page_visual_evidence,
)


def _pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=(300, 400))
    document.drawString(40, 350, "Page one")
    document.showPage()
    document.drawString(40, 350, "Page two")
    document.save()


def test_page_visual_evidence_renders_and_verifies_every_page(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source)
    manifest = extract_page_visual_evidence(
        source,
        tmp_path / "evidence",
        document_id="doc-1",
        source_ref="source-1",
    )
    payload = load_page_visual_evidence(
        manifest,
        expected_source_sha256=sha256(source.read_bytes()).hexdigest(),
    )
    assert payload["status"] == "succeeded"
    assert [page["page_number"] for page in payload["pages"]] == [1, 2]
    assert all(page["width_pixels"] == 600 for page in payload["pages"])
    assert all(page["height_pixels"] == 800 for page in payload["pages"])


def test_page_visual_evidence_rejects_tampered_image(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source)
    manifest = extract_page_visual_evidence(
        source,
        tmp_path / "evidence",
        document_id="doc-1",
        source_ref="source-1",
    )
    image = manifest.parent / "pages" / "page-0001.png"
    image.write_bytes(b"tampered")
    with pytest.raises(PageVisualEvidenceError, match="哈希不一致"):
        load_page_visual_evidence(manifest)
