from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field

# Windows 下 HuggingFace 缓存不支持符号链接，这条警告可以忽略
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from pathlib import Path

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import RapidOcrOptions, ThreadedPdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.exceptions import ConversionError
from docling_core.types.doc import ImageRefMode


ARTIFACTS_DIR_NAME = "artifacts"
MARKDOWN_FILENAME = "content.md"
SOURCE_FILENAME = "source.txt"
DEVICE_ENV_VAR = "ANALYZEPDF_DEVICE"


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

    @property
    def failed(self) -> bool:
        return bool(self.failures)


def configure_stdio() -> None:
    """尽量让中文日志在 Windows / Linux 控制台都能打印，避免 GBK 打断整批任务。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


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


def should_skip_existing(
    pdf_path: Path,
    output_dir: Path,
    *,
    force: bool = False,
) -> bool:
    """仅当 content.md 非空且 source.txt 文件名匹配时跳过。"""

    if force:
        return False

    markdown_path = output_dir / MARKDOWN_FILENAME
    if not markdown_path.is_file() or markdown_path.stat().st_size <= 0:
        return False

    recorded = read_source_filename(output_dir / SOURCE_FILENAME)
    if recorded is None:
        return False

    return recorded == pdf_path.name


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

    attempts: list[tuple[str, DocumentConverter | None]] = [
        ("docling-parse", converter),
        ("pypdfium2", None),
    ]

    last_error = "未知错误"

    for backend_name, shared_converter in attempts:
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

        try:
            conversion_result = active_converter.convert(input_path)
        except ConversionError as error:
            last_error = str(error)
            print(f"解析器 {backend_name} 失败，尝试下一个...")
            continue

        if conversion_result.status == ConversionStatus.SUCCESS:
            if backend_name != "docling-parse":
                print(f"已切换到备用解析器：{backend_name}")
            return conversion_result

        last_error = "; ".join(str(error) for error in (conversion_result.errors or []))
        print(f"解析器 {backend_name} 失败，尝试下一个...")

    raise RuntimeError(f"PDF 解析失败：{input_path.name}（{last_error}）")


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


def write_outputs(
    document,
    input_path: Path,
    output_path: Path,
    options: OutputOptions,
) -> None:
    """在解析成功后再创建目录并写文件，避免失败时留下空壳目录。"""

    output_path.mkdir(parents=True, exist_ok=True)
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

    (output_path / SOURCE_FILENAME).write_text(
        f"{input_path.name}\n{input_path.resolve()}\n",
        encoding="utf-8",
    )

    json_path = output_path / "content.json"
    if options.write_json:
        document_dict = document.export_to_dict()
        json_path.write_text(
            json.dumps(document_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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

    if should_skip_existing(input_path, output_path, force=force):
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

    # 先解析，成功后再落盘，避免失败留下空目录
    conversion_result = convert_pdf(
        input_path=input_path,
        use_ocr=use_ocr,
        converter=converter,
        device=device,
        output_options=options,
    )
    document = conversion_result.document

    if document is None:
        raise RuntimeError(f"PDF 解析未生成文档：{input_path.name}")

    print(f"转换状态：{conversion_result.status}")
    if conversion_result.errors:
        print("转换过程中有以下提示：")
        for error in conversion_result.errors:
            print(f"- {error}")

    write_outputs(
        document=document,
        input_path=input_path,
        output_path=output_path,
        options=options,
    )
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
        if should_skip_existing(pdf_path, output_dir, force=force):
            print(f"已存在且 source 匹配，跳过：{output_dir / MARKDOWN_FILENAME}")
            result.output_dirs.append(output_dir)
            result.skipped += 1
            continue
        try:
            result.output_dirs.append(
                parse_pdf(
                    pdf_path=pdf_path,
                    output_root=output_root,
                    use_ocr=use_ocr,
                    converter=converter,
                    device=accelerator,
                    output_options=options,
                    input_root=input_root,
                    force=force,
                )
            )
        except Exception as error:
            print(f"处理失败：{pdf_path.name}")
            print(f"错误：{error}", file=sys.stderr)
            result.failures.append(f"{pdf_path}: {error}")

    print(
        f"\n批量结束：成功 {len(result.output_dirs) - result.skipped}，"
        f"跳过 {result.skipped}，失败 {len(result.failures)}"
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
            return 1 if batch.failed else 0

        parse_pdf(
            pdf_path=args.pdf,
            output_root=args.output_dir,
            use_ocr=args.use_ocr,
            device=device,
            output_options=output_options,
            force=args.force,
        )
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
