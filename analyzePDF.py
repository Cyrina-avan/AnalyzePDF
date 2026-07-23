from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

# Windows 下 HuggingFace 缓存不支持符号链接，这条警告可以忽略
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

from pathlib import Path

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import RapidOcrOptions, ThreadedPdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.exceptions import ConversionError
from docling_core.types.doc import ImageRefMode

# def export_pictures(document, picture_dir: Path, document_name: str) -> int:
#     """将 Docling 识别出的图片裁剪为独立 PNG 文件。"""
#
#     picture_dir.mkdir(parents=True, exist_ok=True)
#     picture_count = 0
#
#     for element, _level in document.iterate_items():
#         if not isinstance(element, PictureItem):
#             continue
#
#         picture_count += 1
#         picture_path = picture_dir / f"{document_name}_picture_{picture_count:03d}.png"
#
#         try:
#             picture = element.get_image(document)
#             if picture is None:
#                 print(f"图片 {picture_count} 没有可导出的图像数据")
#                 continue
#
#             picture.save(picture_path, "PNG")
#             print(f"已导出图片 {picture_count}：{picture_path}")
#         except Exception as error:
#             print(f"图片 {picture_count} 导出失败：{error}")
#
#     return picture_count


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


def safe_output_name(pdf_path: Path, max_length: int = 48) -> str:
    """生成短且安全的输出目录名，避免空格、特殊字符和超长路径。"""

    stem = pdf_path.stem.strip().rstrip(".- ")
    for char in '<>:"/\\|?*[]':
        stem = stem.replace(char, "_")
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")

    digest = hashlib.sha1(pdf_path.name.encode("utf-8")).hexdigest()[:8]
    if not stem:
        return digest

    if len(stem) <= max_length:
        return stem

    return f"{stem[:max_length]}_{digest}"


ARTIFACTS_DIR_NAME = "artifacts"
MARKDOWN_FILENAME = "content.md"


def resolve_output_dir(pdf_path: Path, output_root: Path) -> Path:
    """每个 PDF 使用独立输出目录，避免互相覆盖。"""

    return output_root / safe_output_name(pdf_path)


def create_converter(
    use_ocr: bool,
    backend: type = DoclingParseDocumentBackend,
) -> DocumentConverter:
    pipeline_options = ThreadedPdfPipelineOptions(
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CUDA),
        do_ocr=use_ocr,
        force_backend_text=not use_ocr,
        ocr_options=RapidOcrOptions(backend="torch"),
        ocr_batch_size=8,
        layout_batch_size=8,
        document_timeout=300,
        generate_picture_images=True,
        images_scale=3.0,
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
            active_converter = create_converter(use_ocr=use_ocr, backend=backend)

        try:
            conversion_result = active_converter.convert(input_path)
        except ConversionError as error:
            last_error = str(error)
            print(f"解析器 {backend_name} 失败，尝试下一个...")
            continue

        last_result = conversion_result

        if conversion_result.status == ConversionStatus.SUCCESS:
            if backend_name != "docling-parse":
                print(f"已切换到备用解析器：{backend_name}")
            return conversion_result

        last_error = "; ".join(str(error) for error in (conversion_result.errors or []))
        print(f"解析器 {backend_name} 失败，尝试下一个...")

    raise RuntimeError(f"PDF 解析失败：{input_path.name}（{last_error}）")


def collect_pdf_files(target: Path) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() != ".pdf":
            raise ValueError(f"不是 PDF 文件：{target}")
        return [target]

    if not target.is_dir():
        raise FileNotFoundError(f"路径不存在：{target}")

    pdf_files = sorted(target.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"目录中没有 PDF 文件：{target}")

    return pdf_files


def parse_pdf(
    pdf_path: str | Path,
    output_root: str | Path = "output",
    use_ocr: bool = False,
    converter: DocumentConverter | None = None,
) -> Path:
    """
    使用 Docling 解析 PDF，并输出：
    1. Markdown 文档
    2. 完整结构化 JSON
    3. 每张表格的 CSV 文件
    """

    input_path = Path(pdf_path)
    output_path = resolve_output_dir(input_path, Path(output_root))
    output_name = safe_output_name(input_path)
    table_dir = output_path / "tables"

    if not input_path.exists():
        raise FileNotFoundError(f"没有找到 PDF 文件：{input_path.resolve()}")

    output_path.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n开始解析：{input_path.resolve()}")
    print(f"输出目录：{output_path.resolve()}")
    if output_name != input_path.stem:
        print(f"输出名称：{output_name}（原文件名过长或含特殊字符，已自动缩短）")
    print(f"OCR：{'开启' if use_ocr else '关闭（使用 PDF 内嵌文字，更快）'}")

    conversion_result = convert_pdf(
        input_path=input_path,
        use_ocr=use_ocr,
        converter=converter,
    )
    document = conversion_result.document

    if document is None:
        raise RuntimeError(f"PDF 解析未生成文档：{input_path.name}")

    print(f"转换状态：{conversion_result.status}")
    if conversion_result.errors:
        print("转换过程中有以下提示：")
        for error in conversion_result.errors:
            print(f"- {error}")

    markdown_path = (output_path / MARKDOWN_FILENAME).resolve()
    # 只传目录名，避免 Docling 把相对路径再拼一次导致 output/.../output/.../artifacts
    document.save_as_markdown(
        markdown_path,
        artifacts_dir=Path(ARTIFACTS_DIR_NAME),
        image_mode=ImageRefMode.REFERENCED,
    )
    normalize_markdown_image_paths(markdown_path)
    artifacts_dir = markdown_path.parent / ARTIFACTS_DIR_NAME

    (output_path / "source.txt").write_text(
        f"{input_path.name}\n{input_path.resolve()}\n",
        encoding="utf-8",
    )

    document_dict = document.export_to_dict()
    json_path = output_path / "content.json"
    json_path.write_text(
        json.dumps(document_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # picture_count = export_pictures(
    #     document=document,
    #     picture_dir=picture_dir,
    #     document_name=output_name,
    # )

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
    print(f"JSON：{json_path.resolve()}")
    print(f"图片目录：{artifacts_dir.resolve()}")
    print(f"表格目录：{table_dir.resolve()}")

    return output_path


def parse_pdf_batch(
    input_dir: str | Path,
    output_root: str | Path = "output",
    use_ocr: bool = False,
) -> list[Path]:
    pdf_files = collect_pdf_files(Path(input_dir))
    converter = create_converter(use_ocr=use_ocr)
    output_dirs: list[Path] = []

    print(f"共找到 {len(pdf_files)} 个 PDF 文件")

    for index, pdf_path in enumerate(pdf_files, start=1):
        print(f"\n[{index}/{len(pdf_files)}] 处理 {pdf_path.name}")
        try:
            output_dirs.append(
                parse_pdf(
                    pdf_path=pdf_path,
                    output_root=output_root,
                    use_ocr=use_ocr,
                    converter=converter,
                )
            )
        except (OSError, RuntimeError) as error:
            print(f"处理失败：{pdf_path.name}")
            print(f"错误：{error}", file=sys.stderr)

    return output_dirs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 Docling 解析 PDF，导出 Markdown、JSON、表格和图片。",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        help="待处理的 PDF 文件路径",
    )
    parser.add_argument(
        "--input-dir",
        help="批量处理该文件夹下的所有 PDF",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="输出根目录，默认 output；每个 PDF 会写入 output/<文件名>/",
    )
    parser.add_argument(
        "--use-ocr",
        action="store_true",
        help="开启 OCR（扫描版 PDF 使用，速度更慢）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pdf and args.input_dir:
        parser.error("请只指定 pdf 文件，或只指定 --input-dir，不能同时使用。")

    if not args.pdf and not args.input_dir:
        parser.error("请提供 PDF 文件路径，或使用 --input-dir 指定文件夹。")

    try:
        if args.input_dir:
            parse_pdf_batch(
                input_dir=args.input_dir,
                output_root=args.output_dir,
                use_ocr=args.use_ocr,
            )
        else:
            parse_pdf(
                pdf_path=args.pdf,
                output_root=args.output_dir,
                use_ocr=args.use_ocr,
            )
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
