"""AnalyzePDF 的完整解析入口：Docling、质量检查、MinerU 备用和降级。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable

from analyzepdf.contracts.docling import adapt_analyzepdf_output
from analyzepdf.contracts.mineru import adapt_mineru_output
from analyzepdf.native_labels import extract_native_label_evidence
from analyzepdf.native_text import extract_native_text_evidence
from analyzepdf.page_visual import extract_page_visual_evidence
from analyzepdf.quality import assess_ingestion_quality
from analyzepdf.routing import QualityAssessor, RoutingDecision, route_with_fallback


ROUTING_VERSION = 2


class PipelineError(RuntimeError):
    """完整解析流水线无法安全开始或发布。"""


def run_pipeline(
    source_pdf: str | Path,
    output_dir: str | Path,
    *,
    document_id: str,
    source_ref: str,
    language: str,
    use_ocr: bool = False,
    device: str | None = None,
    mineru_backend: str = "hybrid-engine",
    assessor: QualityAssessor = assess_ingestion_quality,
) -> Path:
    """运行完整解析流水线并返回 ``routing.json``。

    输出目录必须尚不存在。每条路线保留自己的原始结果和统一结果；
    ``routing.json`` 只通过相对路径指出最终被选择的结果。
    """

    source_path = Path(source_pdf).resolve()
    output_path = Path(output_dir).resolve()
    if not source_path.is_file():
        raise PipelineError("源 PDF 不存在")
    if output_path.exists():
        raise PipelineError("输出目录已经存在")
    staging = output_path.with_name(f".{output_path.name}.staging")
    if staging.exists():
        raise PipelineError("暂存目录已经存在，请先核对上次运行状态")
    staging.mkdir(parents=True)

    try:
        extract_native_text_evidence(
            source_path,
            staging / "native-text-evidence.json",
            document_id=document_id,
            source_ref=source_ref,
        )
        extract_native_label_evidence(
            source_path,
            staging / "native-label-evidence.json",
            document_id=document_id,
            source_ref=source_ref,
        )
        extract_page_visual_evidence(
            source_path,
            staging / "page-visual-evidence",
            document_id=document_id,
            source_ref=source_ref,
        )
        decision = route_with_fallback(
            source_path,
            primary_runner=lambda: _run_docling_route(
                source_path,
                staging / "routes" / "docling",
                document_id=document_id,
                source_ref=source_ref,
                language=language,
                use_ocr=use_ocr,
                device=device,
            ),
            fallback_runner=lambda: _run_mineru_route(
                source_path,
                staging / "routes" / "mineru",
                document_id=document_id,
                source_ref=source_ref,
                language=language,
                backend=mineru_backend,
            ),
            assessor=assessor,
        )
        report_path = staging / "routing.json"
        report_path.write_text(
            json.dumps(
                _routing_payload(decision, staging),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return output_path / "routing.json"


def _run_docling_route(
    source_pdf: Path,
    route_root: Path,
    *,
    document_id: str,
    source_ref: str,
    language: str,
    use_ocr: bool,
    device: str | None,
) -> Path:
    """运行 Docling 并生成统一结果。"""

    from analyzepdf.parsers import docling as docling_parser

    route_root.mkdir(parents=True, exist_ok=False)
    native_root = route_root / "native"
    native_dir = docling_parser.parse_pdf(
        source_pdf,
        output_root=native_root,
        use_ocr=use_ocr,
        device=docling_parser.resolve_accelerator_device(device),
        output_options=docling_parser.OutputOptions.full(),
    )
    return adapt_analyzepdf_output(
        native_dir,
        route_root / "contract",
        document_id=document_id,
        source_ref=source_ref,
        language=language,
    )


def _run_mineru_route(
    source_pdf: Path,
    route_root: Path,
    *,
    document_id: str,
    source_ref: str,
    language: str,
    backend: str,
) -> Path:
    """在 MinerU 的独立 uv 环境中运行备用解析。"""

    repository_root = Path(__file__).resolve().parents[2]
    runtime_root = repository_root / "runtimes" / "mineru"
    exporter = runtime_root / "export_native.py"
    if not exporter.is_file() or not (runtime_root / "pyproject.toml").is_file():
        raise PipelineError("MinerU 运行环境不完整")
    uv = shutil.which("uv")
    if uv is None:
        raise PipelineError("未找到 uv，无法启动 MinerU 独立环境")

    route_root.mkdir(parents=True, exist_ok=False)
    native_dir = route_root / "native"
    mineru_language = "ch" if language.lower().startswith("zh") else "en"
    completed = subprocess.run(
        [
            uv,
            "run",
            "--project",
            str(runtime_root),
            "python",
            str(exporter),
            "--input-pdf",
            str(source_pdf),
            "--output-dir",
            str(native_dir),
            "--backend",
            backend,
            "--language",
            mineru_language,
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not (native_dir / "run.json").is_file():
        raise PipelineError(
            "MinerU 未生成可检查的运行记录"
            if completed.returncode
            else "MinerU 输出不完整"
        )
    return adapt_mineru_output(
        native_dir,
        route_root / "contract",
        document_id=document_id,
        source_ref=source_ref,
        language=language,
    )


def _routing_payload(decision: RoutingDecision, staging: Path) -> dict[str, Any]:
    selected_contract = _relative_contract(decision.selected_contract, staging)
    return {
        "routing_version": ROUTING_VERSION,
        "status": decision.status,
        "selected_route": decision.selected_route,
        "selected_contract": selected_contract,
        "native_text_evidence": "native-text-evidence.json",
        "native_label_evidence": "native-label-evidence.json",
        "page_visual_evidence": "page-visual-evidence/manifest.json",
        "attempts": [
            {
                "route": attempt.route,
                "completed": attempt.completed,
                "contract": _relative_contract(attempt.contract_path, staging),
                "quality_decision": attempt.quality_decision,
                "route_action": attempt.route_action,
                "reason_codes": list(attempt.reason_codes),
                "warning_codes": list(attempt.warning_codes),
                "blocking_codes": list(attempt.blocking_codes),
                "failure_stages": list(attempt.failure_stages),
                "failure_code": attempt.failure_code,
            }
            for attempt in decision.attempts
        ],
    }


def _relative_contract(path: Path | None, staging: Path) -> str | None:
    if path is None:
        return None
    resolved = path.resolve()
    try:
        return resolved.relative_to(staging.resolve()).as_posix()
    except ValueError as exc:
        raise PipelineError("解析结果位于输出目录之外") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--use-ocr", action="store_true")
    parser.add_argument(
        "--device", choices=("auto", "cpu", "cuda", "gpu", "mps", "xpu")
    )
    parser.add_argument(
        "--mineru-backend",
        choices=("pipeline", "hybrid-engine"),
        default="hybrid-engine",
    )
    args = parser.parse_args(argv)
    try:
        report_path = run_pipeline(
            args.source_pdf,
            args.output_dir,
            document_id=args.document_id,
            source_ref=args.source_ref,
            language=args.language,
            use_ocr=args.use_ocr,
            device=args.device,
            mineru_backend=args.mineru_backend,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        print(
            f"解析完成：status={report['status']} "
            f"selected={report['selected_route'] or 'none'}"
        )
        return 0 if report["selected_route"] else 2
    except (PipelineError, OSError, ValueError) as exc:
        print(f"解析流水线失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
