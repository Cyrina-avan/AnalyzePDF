from __future__ import annotations

import json
from secrets import token_urlsafe

from pypdf import PdfReader

from analyzepdf.fixtures.pdf_edges import (
    MANAGED_FILENAMES,
    generate_pdf_edge_fixture_pack,
)


def test_generated_pdf_edge_pack_has_known_content_and_safe_metadata(tmp_path) -> None:
    output = tmp_path / "edge-pack"
    non_secret_test_key = token_urlsafe(24)

    manifest_path = generate_pdf_edge_fixture_pack(
        output,
        encryption_secret=non_secret_test_key,
    )

    assert {path.name for path in output.iterdir()} == set(MANAGED_FILENAMES)
    base = PdfReader(output / "base-text.pdf")
    assert len(base.pages) == 3
    extracted = "\n".join(page.extract_text() or "" for page in base.pages)
    assert "EDGE-PDF-BASE-001" in extracted
    assert "ORDER-ALPHA-031" in extracted
    assert "ORDER-BETA-032" in extracted
    assert "ORDER-OMEGA-033" in extracted

    for name in (
        "scan-clear.pdf",
        "scan-tilted.pdf",
        "scan-blurred.pdf",
        "scan-low-resolution.pdf",
    ):
        scanned = PdfReader(output / name)
        assert len(scanned.pages) == 3
        assert not "".join(page.extract_text() or "" for page in scanned.pages).strip()

    encrypted = PdfReader(output / "encrypted.pdf")
    assert encrypted.is_encrypted
    assert encrypted.decrypt("incorrect-test-key") == 0
    assert encrypted.decrypt(non_secret_test_key) != 0
    assert len(encrypted.pages) == 3

    truncated = (output / "corrupted-truncated.pdf").read_bytes()
    assert truncated.startswith(b"%PDF-")
    assert b"%%EOF" not in truncated[-1024:]

    timeout_reader = PdfReader(output / "timeout-long.pdf")
    assert len(timeout_reader.pages) == 120
    assert "TIMEOUT-PAGE-001" in (timeout_reader.pages[0].extract_text() or "")
    assert "TIMEOUT-PAGE-120" in (timeout_reader.pages[-1].extract_text() or "")

    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["fixture_pack_version"] == 1
    assert len(manifest["fixtures"]) == 8
    assert manifest["timeout_experiment"]["fixed_timeout_seconds"] is False
    assert manifest["encryption"]["secret_value_recorded"] is False
    assert non_secret_test_key not in manifest_text
    assert str(tmp_path) not in manifest_text
    assert all("/" not in item["path"] for item in manifest["fixtures"])
