from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HF_MIRROR_ENV_VAR = "HF_ENDPOINT"
USE_HF_MIRROR_ENV_VAR = "ANALYZEPDF_USE_HF_MIRROR"


def configure_stdio() -> None:
    """尽量让中文日志在 Windows / Linux 控制台都能打印，避免 GBK 打断整批任务。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_hf_hub() -> None:
    """在导入 Docling/HuggingFace 前决定 Endpoint，避免库内缓存旧值。"""

    if os.environ.get(USE_HF_MIRROR_ENV_VAR):
        return

    mirror = os.environ.pop(HF_MIRROR_ENV_VAR, None)
    if mirror:
        print(
            f"提示：已忽略 {HF_MIRROR_ENV_VAR}={mirror!r}，"
            "Docling 模型将从官方 Hugging Face Hub 下载。"
            f"若你确认镜像可用，可设 {USE_HF_MIRROR_ENV_VAR}=1 保留镜像。",
            file=sys.stderr,
        )


# HuggingFace 会在 import 时缓存 Endpoint，因此必须先配置再导入 Docling。
configure_stdio()
configure_hf_hub()

# Windows 下 HuggingFace 缓存不支持符号链接，这条警告可以忽略
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import RapidOcrOptions, ThreadedPdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode


ARTIFACTS_DIR_NAME = "artifacts"
MARKDOWN_FILENAME = "content.md"
SOURCE_FILENAME = "source.txt"
RUN_METADATA_FILENAME = "run.json"
DEVICE_ENV_VAR = "ANALYZEPDF_DEVICE"
OUTPUT_STATE_VERSION = 1


def package_version(package: str, fallback: str = "unknown") -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return fallback


PARSER_VERSION = package_version("analyzepdf", "0.1.0")
ENGINE_VERSION = package_version("docling")


@dataclass(frozen=True)
class OutputOptions:
    """控制导出产物；瘦模式适合只做正文抽取/离线回传。"""

    write_json: bool = True
    write_tables: bool = True
    export_images: bool = True
    images_scale: float = 3.0

    @classmethod
    def full(cls) -> OutputOptions:
        return cls()

    @classmethod
    def slim(cls) -> OutputOptions:
        return cls(
            write_json=False,
            write_tables=False,
            export_images=False,
            images_scale=1.0,
        )


@dataclass
class BatchResult:
    output_dirs: list[Path] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped: int = 0
    partial: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    @property
    def has_issues(self) -> bool:
        return self.failed or self.partial > 0


@dataclass(frozen=True)
class ConversionOutcome:
    """Docling 结果、实际 backend 与可公开的降级错误。"""

    result: Any
    backend: str
    errors: tuple[dict[str, Any], ...] = ()


class PDFConversionFailure(RuntimeError):
    """All parser backends failed, with sanitized machine-readable errors."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("所有 PDF 解析后端均失败")
        self.errors = tuple(errors)


class EmptyTextOutputError(RuntimeError):
    """Raised when a parser reports success but exports no usable text."""


def resolve_accelerator_device(device: str | None = None) -> AcceleratorDevice:
    """
    解析加速设备：
    1. CLI --device
    2. 环境变量 ANALYZEPDF_DEVICE
    3. 默认 auto
    """

    raw = (device or os.environ.get(DEVICE_ENV_VAR) or "auto").strip().lower()
    mapping = {
        "auto": AcceleratorDevice.AUTO,
        "cpu": AcceleratorDevice.CPU,
        "cuda": AcceleratorDevice.CUDA,
        "gpu": AcceleratorDevice.CUDA,
        "mps": AcceleratorDevice.MPS,
        "xpu": AcceleratorDevice.XPU,
    }
    if raw not in mapping:
        known = ", ".join(sorted(mapping))
        raise ValueError(f"不支持的设备：{raw}（可选：{known}）")
    return mapping[raw]


def normalize_markdown_image_paths(markdown_path: Path) -> None:
    """统一图片引用为相对路径 artifacts/image_xxx.png。"""

    markdown = markdown_path.read_text(encoding="utf-8")

    def fix_image_ref(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        filename = Path(match.group(2).replace("\\", "/")).name
        return f"![{alt_text}]({ARTIFACTS_DIR_NAME}/{filename})"

    normalized = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", fix_image_ref, markdown)
    if normalized != markdown:
        markdown_path.write_text(normalized, encoding="utf-8")


def sanitize_path_component(name: str, max_length: int | None = None) -> str:
    cleaned = name.strip().rstrip(".- ")
    for char in '<>:"/\\|?*[]':
        cleaned = cleaned.replace(char, "_")
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip("_")
    return cleaned


def relative_identity(pdf_path: Path, input_root: Path | None) -> str:
    """用于区分输出目录的稳定身份：优先相对 input_root 的路径。"""

    resolved = pdf_path.resolve()
    if input_root is not None:
        try:
            return resolved.relative_to(input_root.resolve()).as_posix()
        except ValueError:
            pass
    return resolved.name


def safe_output_name(
    pdf_path: Path,
    input_root: Path | None = None,
    max_length: int = 48,
    *,
    include_parent_prefix: bool = False,
) -> str:
    """
    生成短且安全的输出目录名。
    digest 基于相对路径（或文件名），避免递归扫描时不同目录同名 PDF 撞车。
    include_parent_prefix：在未镜像目录结构时，把父目录名拼进 stem。
    """

    identity = relative_identity(pdf_path, input_root)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]

    path_obj = Path(identity)
    stem = sanitize_path_component(path_obj.stem) or digest
    if include_parent_prefix and path_obj.parent != Path("."):
        parent = sanitize_path_component(path_obj.parent.name, max_length=24)
        if parent:
            stem = f"{parent}__{stem}"

    if len(stem) > max_length:
        stem = stem[:max_length].rstrip("_")

    # 始终带 digest，保证递归/同 stem 场景唯一
    return f"{stem}_{digest}"


def resolve_output_dir(
    pdf_path: Path,
    output_root: Path,
    input_root: Path | None = None,
) -> Path:
    """
    每个 PDF 使用独立输出目录。
    若提供 input_root，则镜像相对父目录，进一步避免跨目录撞名：
    overseas/Kaspersky/PDFs/a.pdf -> <output>/Kaspersky/PDFs/<safe_name>/
    """

    output_root = Path(output_root)
    resolved = pdf_path.resolve()
    if input_root is not None:
        try:
            rel_parent = resolved.relative_to(Path(input_root).resolve()).parent
            safe_parts = [sanitize_path_component(part) for part in rel_parent.parts]
            safe_parts = [part for part in safe_parts if part]
            # 已镜像父路径，名称不再重复拼 parent
            name = safe_output_name(
                resolved, input_root, include_parent_prefix=False
            )
            return output_root.joinpath(*safe_parts, name)
        except ValueError:
            pass

    return output_root / safe_output_name(
        resolved, input_root, include_parent_prefix=True
    )


def read_source_filename(source_path: Path) -> str | None:
    if not source_path.is_file():
        return None
    try:
        first = source_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except OSError:
        return None
    return first or None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_request(use_ocr: bool, options: OutputOptions) -> dict[str, Any]:
    return {
        "use_ocr": use_ocr,
        "output_options": asdict(options),
    }


def sanitize_error_message(message: object, input_path: Path | None = None) -> str:
    """Collapse an upstream error into a short message without source paths."""

    text = str(message).split("Traceback (most recent call last)", 1)[0]
    if input_path is not None:
        candidates = {
            str(input_path),
            str(input_path.resolve()),
            input_path.name,
        }
        for candidate in sorted(candidates, key=len, reverse=True):
            if candidate:
                text = text.replace(candidate, "[source]")
    text = re.sub(
        r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s,;:()]+"
        r"|\\\\[^\\\s]+[\\/][^\s,;:()]+"
        r"|(?<![A-Za-z0-9:/])/(?:Users|Volumes|home|private|tmp|var|mnt)/[^\s,;:()]+)",
        "[path]",
        text,
    )
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "No safe diagnostic detail was available")[:500]


def error_record(
    *,
    code: str,
    stage: str,
    message: object,
    retryable: bool,
    input_path: Path | None = None,
    page_number: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "code": code,
        "stage": stage,
        "sanitized_message": sanitize_error_message(message, input_path),
        "retryable": retryable,
    }
    if page_number is not None:
        record["page_number"] = page_number
    return record


def classify_parse_error(error: object, input_path: Path, backend: str) -> dict[str, Any]:
    """Classify only well-known retry cases; unknown corruption is not retryable."""

    message = str(error)
    lowered = message.lower()
    if any(token in lowered for token in ("password", "encrypted", "encryption")):
        code = "PDF_ENCRYPTED"
        retryable = False
    elif any(token in lowered for token in ("timeout", "timed out")):
        code = "PARSE_TIMEOUT"
        retryable = True
    elif any(token in lowered for token in ("out of memory", "resource exhausted")):
        code = "RESOURCE_EXHAUSTED"
        retryable = True
    else:
        code = "PDF_PARSE_FAILED"
        retryable = False
    return error_record(
        code=code,
        stage="parse",
        message=f"{backend}: {message}",
        retryable=retryable,
        input_path=input_path,
    )


def read_run_metadata(output_dir: Path) -> dict[str, Any] | None:
    metadata_path = output_dir / RUN_METADATA_FILENAME
    if not metadata_path.is_file():
        return None
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def output_inventory(output_dir: Path) -> list[dict[str, Any]]:
    managed_paths: list[Path] = []
    for filename in (MARKDOWN_FILENAME, SOURCE_FILENAME, "content.json"):
        path = output_dir / filename
        if path.is_file():
            managed_paths.append(path)
    for directory_name in (ARTIFACTS_DIR_NAME, "tables"):
        directory = output_dir / directory_name
        if directory.is_dir():
            managed_paths.extend(path for path in directory.rglob("*") if path.is_file())

    inventory: list[dict[str, Any]] = []
    for path in sorted(managed_paths):
        inventory.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "byte_size": path.stat().st_size,
                "content_sha256": file_sha256(path),
            }
        )
    return inventory


def inventory_matches(output_dir: Path, inventory: Any) -> bool:
    if not isinstance(inventory, list) or not inventory:
        return False
    for item in inventory:
        if not isinstance(item, dict):
            return False
        relative_path = item.get("path")
        expected_size = item.get("byte_size")
        expected_hash = item.get("content_sha256")
        if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
            return False
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            return False
        artifact = output_dir / path
        if not artifact.is_file() or artifact.stat().st_size != expected_size:
            return False
        if file_sha256(artifact) != expected_hash:
            return False
    return True


def clear_previous_generated_outputs(output_dir: Path) -> None:
    """只清理本工具管理的产物；保留输出目录中的其他用户文件。"""

    for filename in (
        MARKDOWN_FILENAME,
        SOURCE_FILENAME,
        "content.json",
        RUN_METADATA_FILENAME,
    ):
        path = output_dir / filename
        if path.is_file() or path.is_symlink():
            path.unlink()

    for directory_name in (ARTIFACTS_DIR_NAME, "tables"):
        path = output_dir / directory_name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def should_skip_existing(
    pdf_path: Path,
    output_dir: Path,
    *,
    force: bool = False,
    use_ocr: bool = False,
    output_options: OutputOptions | None = None,
    source_sha256: str | None = None,
) -> bool:
    """仅当源内容、解析代码、请求配置和全部产物仍一致时跳过。"""

    if force:
        return False

    markdown_path = output_dir / MARKDOWN_FILENAME
    if not markdown_path.is_file() or markdown_path.stat().st_size <= 0:
        return False

    metadata = read_run_metadata(output_dir)
    if metadata is None:
        return False

    options = output_options or OutputOptions.full()
    source_hash = source_sha256 or file_sha256(pdf_path)
    parser_hash = file_sha256(Path(__file__).resolve())
    if metadata.get("output_state_version") != OUTPUT_STATE_VERSION:
        return False
    if metadata.get("source", {}).get("content_sha256") != source_hash:
        return False
    if metadata.get("source", {}).get("byte_size") != pdf_path.stat().st_size:
        return False
    if metadata.get("parser", {}).get("source_sha256") != parser_hash:
        return False
    if metadata.get("request") != output_request(use_ocr, options):
        return False
    if metadata.get("result", {}).get("status") != "succeeded":
        return False
    return inventory_matches(output_dir, metadata.get("output_files"))


def create_converter(
    use_ocr: bool,
    backend: type = DoclingParseDocumentBackend,
    device: AcceleratorDevice | None = None,
    output_options: OutputOptions | None = None,
) -> DocumentConverter:
    accelerator_device = device or resolve_accelerator_device()
    options = output_options or OutputOptions.full()
    pipeline_options = ThreadedPdfPipelineOptions(
        accelerator_options=AcceleratorOptions(device=accelerator_device),
        do_ocr=use_ocr,
        force_backend_text=not use_ocr,
        ocr_options=RapidOcrOptions(backend="torch"),
        ocr_batch_size=8,
        layout_batch_size=8,
        document_timeout=300,
        generate_picture_images=options.export_images,
        images_scale=options.images_scale,
    )

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
                backend=backend,
            )
        }
    )


def convert_pdf(
    input_path: Path,
    use_ocr: bool,
    converter: DocumentConverter | None = None,
    device: AcceleratorDevice | None = None,
    output_options: OutputOptions | None = None,
):
    """先用默认解析器，失败时自动切换到 pypdfium2。"""

    return _convert_pdf_with_backend(
        input_path=input_path,
        use_ocr=use_ocr,
        converter=converter,
        device=device,
        output_options=output_options,
    ).result


def _convert_pdf_with_backend(
    input_path: Path,
    use_ocr: bool,
    converter: DocumentConverter | None = None,
    device: AcceleratorDevice | None = None,
    output_options: OutputOptions | None = None,
) -> ConversionOutcome:
    """Prefer a complete result, but preserve a usable partial result."""

    attempts: list[tuple[str, DocumentConverter | None]] = [
        ("docling-parse", converter),
        ("pypdfium2", None),
    ]

    attempt_errors: list[dict[str, Any]] = []
    partial_outcome: ConversionOutcome | None = None

    for backend_name, shared_converter in attempts:
        try:
            active_converter = shared_converter
            if active_converter is None:
                backend = (
                    DoclingParseDocumentBackend
                    if backend_name == "docling-parse"
                    else PyPdfiumDocumentBackend
                )
                active_converter = create_converter(
                    use_ocr=use_ocr,
                    backend=backend,
                    device=device,
                    output_options=output_options,
                )
            conversion_result = active_converter.convert(input_path)
        except Exception as error:
            attempt_errors.append(classify_parse_error(error, input_path, backend_name))
            print(f"解析器 {backend_name} 失败，尝试下一个...")
            continue

        if conversion_result.status == ConversionStatus.SUCCESS:
            if backend_name != "docling-parse":
                print(f"已切换到备用解析器：{backend_name}")
            return ConversionOutcome(result=conversion_result, backend=backend_name)

        status_errors = list(conversion_result.errors or [])
        if not status_errors:
            status_errors = [f"backend returned status {conversion_result.status.value}"]
        records = [
            classify_parse_error(error, input_path, backend_name)
            for error in status_errors
        ]
        if conversion_result.status == ConversionStatus.PARTIAL_SUCCESS:
            records = [{**record, "code": "PDF_PARTIAL_PARSE"} for record in records]
        if (
            conversion_result.status == ConversionStatus.PARTIAL_SUCCESS
            and conversion_result.document is not None
            and partial_outcome is None
        ):
            partial_outcome = ConversionOutcome(
                result=conversion_result,
                backend=backend_name,
                errors=tuple(records),
            )
        attempt_errors.extend(records)
        print(f"解析器 {backend_name} 失败，尝试下一个...")

    if partial_outcome is not None:
        print(f"完整解析不可用，保留部分成功结果：{partial_outcome.backend}")
        return ConversionOutcome(
            result=partial_outcome.result,
            backend=partial_outcome.backend,
            errors=tuple(attempt_errors),
        )

    if not attempt_errors:
        attempt_errors.append(
            error_record(
                code="PDF_PARSE_FAILED",
                stage="parse",
                message="All PDF parser backends failed without diagnostic detail",
                retryable=False,
            )
        )
    raise PDFConversionFailure(attempt_errors)


def is_temp_office_pdf(path: Path) -> bool:
    """跳过 Office 打开文件时产生的临时 PDF（如 ~$xxx.pdf）。"""

    return path.name.startswith("~$")


def collect_pdf_files(target: Path, recursive: bool = True) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() != ".pdf":
            raise ValueError(f"不是 PDF 文件：{target}")
        return [target]

    if not target.is_dir():
        raise FileNotFoundError(f"路径不存在：{target}")

    pattern_iter = target.rglob("*.pdf") if recursive else target.glob("*.pdf")
    pdf_files = sorted(
        path for path in pattern_iter if path.is_file() and not is_temp_office_pdf(path)
    )
    if not pdf_files:
        raise FileNotFoundError(f"目录中没有 PDF 文件：{target}")

    return pdf_files


def write_run_metadata(
    *,
    input_path: Path,
    output_path: Path,
    options: OutputOptions,
    source_sha256: str,
    use_ocr: bool,
    backend: str,
    status: str,
    errors: list[dict[str, Any]],
    started_at: datetime,
    started_monotonic: float,
) -> None:
    completed_at = datetime.now(timezone.utc)
    run_metadata = {
        "output_state_version": OUTPUT_STATE_VERSION,
        "source": {
            "content_sha256": source_sha256,
            "byte_size": input_path.stat().st_size,
        },
        "parser": {
            "name": "AnalyzePDF",
            "version": PARSER_VERSION,
            "engine_name": "docling",
            "engine_version": ENGINE_VERSION,
            "backend": backend,
            "source_sha256": file_sha256(Path(__file__).resolve()),
        },
        "request": output_request(use_ocr, options),
        "result": {
            "status": status,
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
            "duration_ms": round((time.monotonic() - started_monotonic) * 1000),
        },
        "errors": errors,
        "output_files": output_inventory(output_path),
    }
    (output_path / RUN_METADATA_FILENAME).write_text(
        json.dumps(run_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def publish_failed_run(
    *,
    input_path: Path,
    output_path: Path,
    options: OutputOptions,
    source_sha256: str,
    use_ocr: bool,
    backend: str,
    errors: list[dict[str, Any]],
    started_at: datetime,
    started_monotonic: float,
) -> None:
    """Replace stale managed outputs with one durable failed-run record."""

    output_path.mkdir(parents=True, exist_ok=True)
    clear_previous_generated_outputs(output_path)
    write_run_metadata(
        input_path=input_path,
        output_path=output_path,
        options=options,
        source_sha256=source_sha256,
        use_ocr=use_ocr,
        backend=backend,
        status="failed",
        errors=errors,
        started_at=started_at,
        started_monotonic=started_monotonic,
    )


def write_outputs(
    document,
    input_path: Path,
    output_path: Path,
    options: OutputOptions,
    *,
    source_sha256: str,
    use_ocr: bool,
    backend: str,
    conversion_errors: tuple[dict[str, Any], ...],
    started_at: datetime,
    started_monotonic: float,
) -> None:
    """Write complete or usable partial outputs, followed by durable metadata."""

    output_path.mkdir(parents=True, exist_ok=True)
    clear_previous_generated_outputs(output_path)
    table_dir = output_path / "tables"
    if options.write_tables:
        table_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = (output_path / MARKDOWN_FILENAME).resolve()
    # 图片只由 Docling save_as_markdown 导出一次：
    # artifacts_dir 只传目录名，避免拼成 output/.../output/.../artifacts 双份。
    if options.export_images:
        document.save_as_markdown(
            markdown_path,
            artifacts_dir=Path(ARTIFACTS_DIR_NAME),
            image_mode=ImageRefMode.REFERENCED,
        )
        normalize_markdown_image_paths(markdown_path)
        artifacts_dir: Path | None = markdown_path.parent / ARTIFACTS_DIR_NAME
    else:
        document.save_as_markdown(
            markdown_path,
            image_mode=ImageRefMode.PLACEHOLDER,
        )
        artifacts_dir = None

    if not markdown_path.read_text(encoding="utf-8").strip():
        raise EmptyTextOutputError("Parser exported an empty Markdown document")

    (output_path / SOURCE_FILENAME).write_text(
        f"{input_path.name}\nsha256:{source_sha256}\n",
        encoding="utf-8",
    )

    json_path = output_path / "content.json"
    if options.write_json:
        document_dict = document.export_to_dict()
        json_path.write_text(
            json.dumps(document_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    run_errors = list(conversion_errors)
    if options.write_tables:
        for table_index, table in enumerate(document.tables, start=1):
            try:
                dataframe = table.export_to_dataframe(doc=document)
                csv_path = table_dir / f"table_{table_index:03d}.csv"
                dataframe.to_csv(csv_path, index=False, encoding="utf-8-sig")
                print(
                    f"发现表格 {table_index}："
                    f"{dataframe.shape[0]} 行 × {dataframe.shape[1]} 列"
                )
            except Exception as error:
                print(f"表格 {table_index} 导出失败：{error}")
                run_errors.append(
                    error_record(
                        code="TABLE_EXPORT_FAILED",
                        stage="export",
                        message=f"table {table_index} export failed: {error}",
                        retryable=False,
                        input_path=input_path,
                    )
                )

    status = "partial" if run_errors else "succeeded"
    write_run_metadata(
        input_path=input_path,
        output_path=output_path,
        options=options,
        source_sha256=source_sha256,
        use_ocr=use_ocr,
        backend=backend,
        status=status,
        errors=run_errors,
        started_at=started_at,
        started_monotonic=started_monotonic,
    )

    if status == "partial":
        print(
            f"部分解析完成：保留可用输出，并记录 {len(run_errors)} 条机器可读错误"
        )

    print("解析完成：")
    print(f"Markdown：{markdown_path.resolve()}")
    if options.write_json:
        print(f"JSON：{json_path.resolve()}")
    if artifacts_dir is not None:
        print(f"图片目录：{artifacts_dir.resolve()}")
    if options.write_tables:
        print(f"表格目录：{table_dir.resolve()}")


def parse_pdf(
    pdf_path: str | Path,
    output_root: str | Path = "output",
    use_ocr: bool = False,
    converter: DocumentConverter | None = None,
    device: AcceleratorDevice | None = None,
    output_options: OutputOptions | None = None,
    input_root: str | Path | None = None,
    force: bool = False,
) -> Path:
    """
    使用 Docling 解析 PDF，并按需输出：
    1. Markdown 文档（始终）
    2. 结构化 JSON（可关）
    3. 表格 CSV / 图片 artifacts（可关）
    """

    options = output_options or OutputOptions.full()
    input_path = Path(pdf_path)
    root = Path(input_root) if input_root is not None else None
    output_path = resolve_output_dir(input_path, Path(output_root), root)
    output_name = safe_output_name(input_path, root)

    if not input_path.exists():
        raise FileNotFoundError(f"没有找到 PDF 文件：{input_path.resolve()}")

    source_content_sha256 = file_sha256(input_path)
    if should_skip_existing(
        input_path,
        output_path,
        force=force,
        use_ocr=use_ocr,
        output_options=options,
        source_sha256=source_content_sha256,
    ):
        print(f"已存在且 source 匹配，跳过：{output_path / MARKDOWN_FILENAME}")
        return output_path

    print(f"\n开始解析：{input_path.resolve()}")
    print(f"输出目录：{output_path.resolve()}")
    if output_name != sanitize_path_component(input_path.stem):
        print(f"输出名称：{output_name}（已按路径去重/缩短）")
    print(f"OCR：{'开启' if use_ocr else '关闭（使用 PDF 内嵌文字，更快）'}")
    print(
        "导出："
        f"json={'开' if options.write_json else '关'} / "
        f"tables={'开' if options.write_tables else '关'} / "
        f"images={'开' if options.export_images else '关'}"
    )

    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    try:
        outcome = _convert_pdf_with_backend(
            input_path=input_path,
            use_ocr=use_ocr,
            converter=converter,
            device=device,
            output_options=options,
        )
    except PDFConversionFailure as error:
        publish_failed_run(
            input_path=input_path,
            output_path=output_path,
            options=options,
            source_sha256=source_content_sha256,
            use_ocr=use_ocr,
            backend="docling-parse+pypdfium2",
            errors=list(error.errors),
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        raise
    conversion_result = outcome.result
    document = conversion_result.document

    if document is None:
        errors = [
            error_record(
                code="DOCUMENT_MISSING",
                stage="parse",
                message="Parser completed without producing a document",
                retryable=False,
            )
        ]
        publish_failed_run(
            input_path=input_path,
            output_path=output_path,
            options=options,
            source_sha256=source_content_sha256,
            use_ocr=use_ocr,
            backend=outcome.backend,
            errors=errors,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        raise RuntimeError("PDF 解析未生成文档")

    print(f"转换状态：{conversion_result.status}")
    if conversion_result.errors:
        print("转换过程中有以下提示：")
        for error in conversion_result.errors:
            print(f"- {error}")

    try:
        write_outputs(
            document=document,
            input_path=input_path,
            output_path=output_path,
            options=options,
            source_sha256=source_content_sha256,
            use_ocr=use_ocr,
            backend=outcome.backend,
            conversion_errors=outcome.errors,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
    except EmptyTextOutputError:
        if use_ocr:
            code = "OCR_EMPTY_OUTPUT"
            message = "OCR completed without producing usable text"
            retryable = False
        else:
            code = "OCR_REQUIRED"
            message = "Parser produced no usable text; rerun with OCR enabled"
            retryable = True
        errors = [
            error_record(
                code=code,
                stage="quality",
                message=message,
                retryable=retryable,
            )
        ]
        publish_failed_run(
            input_path=input_path,
            output_path=output_path,
            options=options,
            source_sha256=source_content_sha256,
            use_ocr=use_ocr,
            backend=outcome.backend,
            errors=errors,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        if use_ocr:
            raise RuntimeError("OCR 未生成可用正文") from None
        raise RuntimeError("PDF 没有可用正文，请开启 OCR 后重试") from None
    except Exception as error:
        errors = [
            error_record(
                code="OUTPUT_EXPORT_FAILED",
                stage="export",
                message=error,
                retryable=False,
                input_path=input_path,
            )
        ]
        publish_failed_run(
            input_path=input_path,
            output_path=output_path,
            options=options,
            source_sha256=source_content_sha256,
            use_ocr=use_ocr,
            backend=outcome.backend,
            errors=errors,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        raise
    return output_path


def parse_pdf_batch(
    input_dir: str | Path,
    output_root: str | Path = "output",
    use_ocr: bool = False,
    recursive: bool = True,
    device: AcceleratorDevice | None = None,
    output_options: OutputOptions | None = None,
    force: bool = False,
) -> BatchResult:
    options = output_options or OutputOptions.full()
    accelerator = device or resolve_accelerator_device()
    input_root = Path(input_dir)
    pdf_files = collect_pdf_files(input_root, recursive=recursive)
    converter = create_converter(
        use_ocr=use_ocr,
        device=accelerator,
        output_options=options,
    )
    result = BatchResult()

    print(f"共找到 {len(pdf_files)} 个 PDF 文件（recursive={recursive}）")

    for index, pdf_path in enumerate(pdf_files, start=1):
        print(f"\n[{index}/{len(pdf_files)}] 处理 {pdf_path.name}")
        output_dir = resolve_output_dir(pdf_path, Path(output_root), input_root)
        if should_skip_existing(
            pdf_path,
            output_dir,
            force=force,
            use_ocr=use_ocr,
            output_options=options,
        ):
            print(f"已存在且 source 匹配，跳过：{output_dir / MARKDOWN_FILENAME}")
            result.output_dirs.append(output_dir)
            result.skipped += 1
            continue
        try:
            parsed_output = parse_pdf(
                pdf_path=pdf_path,
                output_root=output_root,
                use_ocr=use_ocr,
                converter=converter,
                device=accelerator,
                output_options=options,
                input_root=input_root,
                force=force,
            )
            result.output_dirs.append(parsed_output)
            metadata = read_run_metadata(parsed_output) or {}
            if metadata.get("result", {}).get("status") == "partial":
                result.partial += 1
        except Exception as error:
            print(f"处理失败：{pdf_path.name}")
            print(f"错误：{error}", file=sys.stderr)
            result.failures.append(f"{pdf_path}: {error}")

    print(
        f"\n批量结束：成功 {len(result.output_dirs) - result.skipped - result.partial}，"
        f"部分成功 {result.partial}，跳过 {result.skipped}，失败 {len(result.failures)}"
    )
    if result.failures:
        print("失败列表：", file=sys.stderr)
        for item in result.failures:
            print(f"- {item}", file=sys.stderr)

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 Docling 解析 PDF，导出 Markdown（及可选 JSON/表格/图片）。",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        help="待处理的 PDF 文件路径",
    )
    parser.add_argument(
        "--input-dir",
        help="批量处理该文件夹下的 PDF（默认递归子目录）",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="输出根目录，默认 output；每个 PDF 会写入独立子目录",
    )
    parser.add_argument(
        "--use-ocr",
        action="store_true",
        help="开启 OCR（扫描版 PDF 使用，速度更慢）",
    )
    parser.add_argument(
        "--device",
        choices=["auto", "cpu", "cuda", "gpu", "mps", "xpu"],
        default=None,
        help="加速设备，默认读 ANALYZEPDF_DEVICE，再不行用 auto",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="批量时不递归子目录，只处理 input-dir 下一层 PDF",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略已有 content.md，强制重新解析",
    )
    parser.add_argument(
        "--slim",
        action="store_true",
        help="瘦输出：只写 content.md + source.txt（不写 json/表格/大图）",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="不导出 content.json",
    )
    parser.add_argument(
        "--no-tables",
        action="store_true",
        help="不导出表格 CSV",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="不导出 artifacts 图片（Markdown 用占位图）",
    )
    return parser


def resolve_output_options(args: argparse.Namespace) -> OutputOptions:
    if args.slim:
        if args.no_json or args.no_tables or args.no_images:
            print(
                "提示：已指定 --slim，忽略 --no-json / --no-tables / --no-images",
                file=sys.stderr,
            )
        return OutputOptions.slim()

    return OutputOptions(
        write_json=not args.no_json,
        write_tables=not args.no_tables,
        export_images=not args.no_images,
        images_scale=1.0 if args.no_images else 3.0,
    )


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pdf and args.input_dir:
        parser.error("请只指定 pdf 文件，或只指定 --input-dir，不能同时使用。")

    if not args.pdf and not args.input_dir:
        parser.error("请提供 PDF 文件路径，或使用 --input-dir 指定文件夹。")

    try:
        device = resolve_accelerator_device(args.device)
        output_options = resolve_output_options(args)
        print(f"加速设备：{device.value}")

        if args.input_dir:
            batch = parse_pdf_batch(
                input_dir=args.input_dir,
                output_root=args.output_dir,
                use_ocr=args.use_ocr,
                recursive=not args.no_recursive,
                device=device,
                output_options=output_options,
                force=args.force,
            )
            return 1 if batch.has_issues else 0

        output_path = parse_pdf(
            pdf_path=args.pdf,
            output_root=args.output_dir,
            use_ocr=args.use_ocr,
            device=device,
            output_options=output_options,
            force=args.force,
        )
        metadata = read_run_metadata(output_path) or {}
        return 1 if metadata.get("result", {}).get("status") == "partial" else 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
