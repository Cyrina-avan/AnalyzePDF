"""生成安全、可重复的 PDF 边缘测试样本。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from PIL import Image, ImageFilter
from pypdf import PdfReader, PdfWriter
import pypdfium2 as pdfium
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


PACK_VERSION = 1
MANAGED_FILENAMES = (
    "base-text.pdf",
    "scan-clear.pdf",
    "scan-tilted.pdf",
    "scan-blurred.pdf",
    "scan-low-resolution.pdf",
    "timeout-long.pdf",
    "encrypted.pdf",
    "corrupted-truncated.pdf",
    "expected.json",
)
EXPECTED_ANCHORS = {
    "1": [
        "可控边缘文件验收基准",
        "EDGE-PDF-BASE-001",
        "第一部分：项目概况",
        "本文件只用于公开、非敏感的软件验收。",
    ],
    "2": [
        "第二部分：处理清单",
        "ROW-A-021",
        "ROW-B-022",
        "ROW-C-023",
    ],
    "3": [
        "第三部分：顺序核验",
        "ORDER-ALPHA-031",
        "ORDER-BETA-032",
        "ORDER-OMEGA-033",
    ],
}


class FixtureGenerationError(RuntimeError):
    """Raised when the controlled PDF fixture pack cannot be generated safely."""


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    fixture_id: str
    path: str
    kind: str
    page_count: int
    content_sha256: str
    byte_size: int
    expected_default_result: str
    use_ocr: bool


def generate_pdf_edge_fixture_pack(
    output_dir: str | Path,
    *,
    encryption_secret: str,
    overwrite: bool = False,
) -> Path:
    """Generate the complete fixture pack and return its expectation manifest."""

    if not isinstance(encryption_secret, str) or len(encryption_secret) < 8:
        raise FixtureGenerationError("Encryption secret must contain at least 8 characters")

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    existing = [name for name in MANAGED_FILENAMES if (root / name).exists()]
    if existing and not overwrite:
        raise FixtureGenerationError(
            "Managed fixture files already exist; use overwrite explicitly"
        )

    with tempfile.TemporaryDirectory(prefix=".pdf-edge-staging-", dir=root) as staging_raw:
        staging = Path(staging_raw)
        base = staging / "base-text.pdf"
        _write_base_pdf(base)

        clear_pages = _render_pdf_pages(base, dpi=200)
        _write_image_pdf(clear_pages, staging / "scan-clear.pdf", resolution=200, quality=95)
        tilted_pages = [
            image.rotate(
                1.8,
                resample=Image.Resampling.BICUBIC,
                expand=False,
                fillcolor="white",
            )
            for image in clear_pages
        ]
        _write_image_pdf(
            tilted_pages, staging / "scan-tilted.pdf", resolution=200, quality=92
        )
        blurred_pages = [
            image.filter(ImageFilter.GaussianBlur(radius=1.4)) for image in clear_pages
        ]
        _write_image_pdf(
            blurred_pages, staging / "scan-blurred.pdf", resolution=200, quality=82
        )
        low_resolution_pages = _render_pdf_pages(base, dpi=72)
        _write_image_pdf(
            low_resolution_pages,
            staging / "scan-low-resolution.pdf",
            resolution=72,
            quality=40,
        )

        _write_timeout_pdf(staging / "timeout-long.pdf", page_count=120)

        _write_encrypted_pdf(base, staging / "encrypted.pdf", encryption_secret)
        _write_truncated_pdf(base, staging / "corrupted-truncated.pdf")

        records = [
            _record("base-text", staging / "base-text.pdf", "text_pdf", "succeeded", False),
            _record(
                "scan-clear",
                staging / "scan-clear.pdf",
                "image_pdf",
                "unusable_without_ocr",
                True,
            ),
            _record(
                "scan-tilted",
                staging / "scan-tilted.pdf",
                "image_pdf",
                "unusable_without_ocr",
                True,
            ),
            _record(
                "scan-blurred",
                staging / "scan-blurred.pdf",
                "image_pdf",
                "unusable_without_ocr",
                True,
            ),
            _record(
                "scan-low-resolution",
                staging / "scan-low-resolution.pdf",
                "image_pdf",
                "unusable_without_ocr",
                True,
            ),
            _record(
                "timeout-long",
                staging / "timeout-long.pdf",
                "text_pdf",
                "succeeded",
                False,
            ),
            _record(
                "encrypted",
                staging / "encrypted.pdf",
                "encrypted_pdf",
                "failed",
                False,
                page_count=3,
            ),
            _record(
                "corrupted-truncated",
                staging / "corrupted-truncated.pdf",
                "corrupted_pdf",
                "failed",
                False,
                page_count=3,
            ),
        ]
        manifest = {
            "fixture_pack_version": PACK_VERSION,
            "description": "Known-content, non-sensitive PDF parser edge fixtures",
            "page_count": 3,
            "expected_anchors_by_page": EXPECTED_ANCHORS,
            "expected_reading_order": [
                "ORDER-ALPHA-031",
                "ORDER-BETA-032",
                "ORDER-OMEGA-033",
            ],
            "timeout_experiment": {
                "fixture_id": "timeout-long",
                "page_count": 120,
                "purpose": "Controlled partial-success evaluation with a short processing timeout",
                "fixed_timeout_seconds": False,
            },
            "encryption": {
                "secret_env_var": "EMERGENT_KB_FIXTURE_PASSWORD",
                "secret_value_recorded": False,
            },
            "fixtures": [asdict(record) for record in records],
        }
        (staging / "expected.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        for filename in MANAGED_FILENAMES:
            source = staging / filename
            if not source.exists():
                raise FixtureGenerationError(f"Managed output missing: {filename}")
        for filename in MANAGED_FILENAMES:
            (staging / filename).replace(root / filename)

    return root / "expected.json"


def _write_base_pdf(path: Path) -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    width, height = A4
    document = canvas.Canvas(
        str(path), pagesize=A4, pageCompression=1, invariant=1
    )
    document.setTitle("EmergentKB controlled PDF edge fixture")
    document.setAuthor("EmergentKB fixture generator")
    document.setSubject("Non-sensitive parser evaluation fixture")

    _page_header(document, "可控边缘文件验收基准", "EDGE-PDF-BASE-001")
    _draw_lines(
        document,
        92,
        height - 190,
        [
            "第一部分：项目概况",
            "本文件只用于公开、非敏感的软件验收。",
            "页面总数固定为三页，页面顺序不得改变。",
            "清晰扫描件应恢复本页标题、编号和主要正文。",
            "质量不足时，系统必须明确报告，不得假装完整成功。",
        ],
        leading=30,
    )
    _page_footer(document, 1)
    document.showPage()

    _page_header(document, "第二部分：处理清单", "EDGE-PDF-TABLE-002")
    _draw_table(document, 72, height - 220)
    _draw_lines(
        document,
        92,
        height - 420,
        [
            "表格后正文：三行记录的顺序必须保持为 A、B、C。",
            "若表格无法恢复，正文仍可保留，但结果应标记为部分成功。",
        ],
        leading=30,
    )
    _page_footer(document, 2)
    document.showPage()

    _page_header(document, "第三部分：顺序核验", "EDGE-PDF-ORDER-003")
    _draw_lines(
        document,
        92,
        height - 200,
        [
            "ORDER-ALPHA-031 第一条顺序锚点。",
            "ORDER-BETA-032 第二条顺序锚点。",
            "ORDER-OMEGA-033 第三条顺序锚点。",
            "验收要求：三个锚点必须按从上到下的顺序出现。",
            "文件结束标记：EDGE-PDF-END-039。",
        ],
        leading=38,
    )
    _page_footer(document, 3)
    document.showPage()
    document.save()


def _page_header(document: canvas.Canvas, title: str, code: str) -> None:
    width, height = A4
    document.setFont("STSong-Light", 22)
    document.drawCentredString(width / 2, height - 90, title)
    document.setFont("Helvetica", 11)
    document.drawCentredString(width / 2, height - 122, code)


def _page_footer(
    document: canvas.Canvas, page_number: int, *, total_pages: int = 3
) -> None:
    width, _ = A4
    document.setFont("STSong-Light", 10)
    document.drawCentredString(
        width / 2, 44, f"第 {page_number} 页 / 共 {total_pages} 页"
    )


def _write_timeout_pdf(path: Path, *, page_count: int) -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    width, height = A4
    document = canvas.Canvas(
        str(path), pagesize=A4, pageCompression=1, invariant=1
    )
    document.setTitle("EmergentKB controlled timeout fixture")
    document.setAuthor("EmergentKB fixture generator")
    document.setSubject("Non-sensitive partial-success evaluation")
    for page_number in range(1, page_count + 1):
        _page_header(
            document,
            "受控超时测试长文档",
            f"EDGE-PDF-TIMEOUT-{page_number:03d}",
        )
        _draw_lines(
            document,
            92,
            height - 200,
            [
                f"第 {page_number} 页：本页仅用于非敏感的软件验收。",
                "处理时限触发后，已经完成的页面和正文应当保留。",
                "尚未处理的页面不得被伪造为解析成功。",
                f"本页唯一校验标记：TIMEOUT-PAGE-{page_number:03d}。",
                "整批任务必须留下可重试的机器错误，并继续处理其他文件。",
            ],
            leading=34,
        )
        _page_footer(document, page_number, total_pages=page_count)
        document.showPage()
    document.save()


def _draw_lines(
    document: canvas.Canvas,
    x: float,
    y: float,
    lines: list[str],
    *,
    leading: float,
) -> None:
    document.setFont("STSong-Light", 14)
    for line in lines:
        document.drawString(x, y, line)
        y -= leading


def _draw_table(document: canvas.Canvas, x: float, y: float) -> None:
    widths = (88, 128, 128, 116)
    row_height = 42
    rows = (
        ("编号", "处理阶段", "负责组", "状态"),
        ("ROW-A-021", "文件接收", "资料组", "已完成"),
        ("ROW-B-022", "版面解析", "解析组", "处理中"),
        ("ROW-C-023", "质量复核", "评测组", "待复核"),
    )
    total_width = sum(widths)
    total_height = row_height * len(rows)
    document.setLineWidth(0.8)
    document.rect(x, y - total_height, total_width, total_height)
    current_x = x
    for width in widths[:-1]:
        current_x += width
        document.line(current_x, y, current_x, y - total_height)
    for index in range(1, len(rows)):
        current_y = y - row_height * index
        document.line(x, current_y, x + total_width, current_y)
    document.setFont("STSong-Light", 11)
    for row_index, row in enumerate(rows):
        current_x = x
        baseline = y - row_height * row_index - 26
        for cell, width in zip(row, widths, strict=True):
            document.drawCentredString(current_x + width / 2, baseline, cell)
            current_x += width


def _render_pdf_pages(path: Path, *, dpi: int) -> list[Image.Image]:
    document = pdfium.PdfDocument(str(path))
    pages: list[Image.Image] = []
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = page.render(scale=dpi / 72)
            try:
                pages.append(bitmap.to_pil().convert("RGB").copy())
            finally:
                bitmap.close()
                page.close()
    finally:
        document.close()
    return pages


def _write_image_pdf(
    pages: list[Image.Image],
    path: Path,
    *,
    resolution: int,
    quality: int,
) -> None:
    if not pages:
        raise FixtureGenerationError("Cannot write an image PDF without pages")
    first, *rest = [page.convert("RGB") for page in pages]
    first.save(
        path,
        "PDF",
        save_all=True,
        append_images=rest,
        resolution=resolution,
        quality=quality,
        subsampling=0,
    )


def _write_encrypted_pdf(source: Path, target: Path, secret: str) -> None:
    reader = PdfReader(source)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": "EmergentKB encrypted test fixture"})
    writer.encrypt(secret, algorithm="RC4-128")
    with target.open("wb") as stream:
        writer.write(stream)


def _write_truncated_pdf(source: Path, target: Path) -> None:
    payload = source.read_bytes()
    remove_bytes = min(512, max(128, len(payload) // 20))
    if len(payload) <= remove_bytes + 16:
        raise FixtureGenerationError("Base PDF is too small to truncate safely")
    target.write_bytes(payload[:-remove_bytes])


def _record(
    fixture_id: str,
    path: Path,
    kind: str,
    expected_default_result: str,
    use_ocr: bool,
    *,
    page_count: int | None = None,
) -> FixtureRecord:
    payload = path.read_bytes()
    if page_count is None:
        reader = PdfReader(path)
        page_count = len(reader.pages)
    return FixtureRecord(
        fixture_id=fixture_id,
        path=path.name,
        kind=kind,
        page_count=page_count,
        content_sha256=sha256(payload).hexdigest(),
        byte_size=len(payload),
        expected_default_result=expected_default_result,
        use_ocr=use_ocr,
    )
