from __future__ import annotations

import json
from pathlib import Path

import analyzepdf.pipeline as pipeline


def test_pipeline_publishes_primary_without_calling_mineru(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic")
    mineru_calls = 0

    def docling(source_pdf: Path, route_root: Path, **kwargs) -> Path:
        del source_pdf, kwargs
        contract = route_root / "contract" / "parsed-document.json"
        contract.parent.mkdir(parents=True)
        contract.write_text("{}", encoding="utf-8")
        return contract

    def mineru(source_pdf: Path, route_root: Path, **kwargs) -> Path:
        del source_pdf, route_root, kwargs
        nonlocal mineru_calls
        mineru_calls += 1
        raise AssertionError("MinerU should not run")

    monkeypatch.setattr(pipeline, "_run_docling_route", docling)
    monkeypatch.setattr(pipeline, "_run_mineru_route", mineru)
    report_path = pipeline.run_pipeline(
        source,
        tmp_path / "result",
        document_id="doc-1",
        source_ref="source-1",
        language="zh-CN",
        assessor=lambda contract, pdf: {"decision": "accept", "reason_codes": []},
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "selected_primary"
    assert report["selected_route"] == "docling"
    assert report["selected_contract"] == "routes/docling/contract/parsed-document.json"
    assert report["native_text_evidence"] == "native-text-evidence.json"
    assert (report_path.parent / "native-text-evidence.json").is_file()
    assert mineru_calls == 0


def test_pipeline_calls_mineru_after_docling_anomaly(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic")
    mineru_calls = 0

    def make_contract(route_root: Path) -> Path:
        contract = route_root / "contract" / "parsed-document.json"
        contract.parent.mkdir(parents=True)
        contract.write_text("{}", encoding="utf-8")
        return contract

    def docling(source_pdf: Path, route_root: Path, **kwargs) -> Path:
        del source_pdf, kwargs
        return make_contract(route_root)

    def mineru(source_pdf: Path, route_root: Path, **kwargs) -> Path:
        del source_pdf, kwargs
        nonlocal mineru_calls
        mineru_calls += 1
        return make_contract(route_root)

    def assess(contract: str | Path, source_pdf: str | Path) -> dict[str, object]:
        del source_pdf
        route = Path(contract).parents[1].name
        return {
            "decision": "review" if route == "docling" else "accept",
            "reason_codes": ["synthetic_anomaly"] if route == "docling" else [],
        }

    monkeypatch.setattr(pipeline, "_run_docling_route", docling)
    monkeypatch.setattr(pipeline, "_run_mineru_route", mineru)
    report_path = pipeline.run_pipeline(
        source,
        tmp_path / "result",
        document_id="doc-1",
        source_ref="source-1",
        language="zh-CN",
        assessor=assess,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "selected_fallback"
    assert report["selected_route"] == "mineru"
    assert mineru_calls == 1


def test_pipeline_degrades_when_both_routes_are_unreliable(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic")

    def route(source_pdf: Path, route_root: Path, **kwargs) -> Path:
        del source_pdf, kwargs
        contract = route_root / "contract" / "parsed-document.json"
        contract.parent.mkdir(parents=True)
        contract.write_text("{}", encoding="utf-8")
        return contract

    monkeypatch.setattr(pipeline, "_run_docling_route", route)
    monkeypatch.setattr(pipeline, "_run_mineru_route", route)
    report_path = pipeline.run_pipeline(
        source,
        tmp_path / "result",
        document_id="doc-1",
        source_ref="source-1",
        language="zh-CN",
        assessor=lambda contract, pdf: {
            "decision": "reject",
            "reason_codes": ["synthetic_failure"],
        },
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "degraded"
    assert report["selected_route"] is None
    assert report["selected_contract"] is None
    assert [item["route"] for item in report["attempts"]] == ["docling", "mineru"]
