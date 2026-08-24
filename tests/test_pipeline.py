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
        assessor=lambda contract, pdf: {
            "decision": "review",
            "route_action": "publish",
            "reason_codes": ["cjk_internal_space_ratio_above_policy"],
            "warning_codes": ["cjk_internal_space_ratio_above_policy"],
            "blocking_codes": [],
            "failure_stages": [],
        },
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "selected_primary"
    assert report["selected_route"] == "docling"
    assert report["selected_contract"] == "routes/docling/contract/parsed-document.json"
    assert report["native_text_evidence"] == "native-text-evidence.json"
    assert report["native_label_evidence"] == "native-label-evidence.json"
    assert report["page_visual_evidence"] == "page-visual-evidence/manifest.json"
    assert report["attempts"][0]["quality_decision"] == "review"
    assert report["attempts"][0]["route_action"] == "publish"
    assert report["attempts"][0]["warning_codes"] == [
        "cjk_internal_space_ratio_above_policy"
    ]
    assert (report_path.parent / "native-text-evidence.json").is_file()
    assert (report_path.parent / "page-visual-evidence" / "manifest.json").is_file()
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
        route_action = "fallback" if route == "docling" else "publish"
        blocking_codes = ["synthetic_anomaly"] if route == "docling" else []
        return {
            "decision": "reject" if route == "docling" else "accept",
            "route_action": route_action,
            "reason_codes": blocking_codes,
            "warning_codes": [],
            "blocking_codes": blocking_codes,
            "failure_stages": [],
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
            "route_action": "fallback",
            "reason_codes": ["synthetic_failure"],
            "warning_codes": [],
            "blocking_codes": ["synthetic_failure"],
            "failure_stages": ["parse"],
        },
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "degraded"
    assert report["selected_route"] is None
    assert report["selected_contract"] is None
    assert [item["route"] for item in report["attempts"]] == ["docling", "mineru"]
    assert report["attempts"][1]["failure_stages"] == ["parse"]
    assert "Traceback" not in report_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in report_path.read_text(encoding="utf-8")
