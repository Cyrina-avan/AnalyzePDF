"""为下游受约束识图判断生成可核验的 PDF 原页图片证据。"""

from __future__ import annotations

from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

import pypdfium2 as pdfium


EVIDENCE_VERSION = "1.0"


class PageVisualEvidenceError(ValueError):
    """原页图片证据无法安全生成或验证。"""


def extract_page_visual_evidence(
    source_pdf: str | Path,
    output_dir: str | Path,
    *,
    document_id: str,
    source_ref: str,
    render_scale: float = 2.0,
) -> Path:
    """渲染全部 PDF 页面；失败只进入证据状态，不伪装成成功。"""

    if render_scale <= 0 or render_scale > 4:
        raise PageVisualEvidenceError("原页图片缩放必须大于 0 且不超过 4")
    source = Path(source_pdf)
    destination = Path(output_dir)
    if destination.exists():
        raise PageVisualEvidenceError("原页图片证据输出已经存在")
    source_hash = sha256(source.read_bytes()).hexdigest()
    temporary = destination.with_name(f".{destination.name}.tmp")
    if temporary.exists():
        raise PageVisualEvidenceError("原页图片证据暂存目录已经存在")
    image_dir = temporary / "pages"
    image_dir.mkdir(parents=True)
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    status = "failed"
    document = None
    try:
        document = pdfium.PdfDocument(str(source))
        for page_number in range(1, len(document) + 1):
            try:
                page = document[page_number - 1]
                width_points, height_points = page.get_size()
                image = page.render(scale=render_scale).to_pil().convert("RGB")
                relative = f"pages/page-{page_number:04d}.png"
                path = temporary.joinpath(*PurePosixPath(relative).parts)
                image.save(path, format="PNG")
                payload = path.read_bytes()
                pages.append(
                    {
                        "page_number": page_number,
                        "image_path": relative,
                        "image_sha256": sha256(payload).hexdigest(),
                        "width_pixels": image.width,
                        "height_pixels": image.height,
                        "width_points": width_points,
                        "height_points": height_points,
                    }
                )
                page.close()
            except Exception:
                errors.append(
                    {
                        "code": "PAGE_RENDER_FAILED",
                        "stage": "render",
                        "page_number": page_number,
                    }
                )
        status = "partial" if errors else "succeeded"
    except Exception:
        errors.append({"code": "PDF_PAGE_RENDER_UNREADABLE", "stage": "input"})
    finally:
        if document is not None:
            document.close()

    manifest = {
        "evidence_version": EVIDENCE_VERSION,
        "document_id": document_id,
        "source_ref": source_ref,
        "source_sha256": source_hash,
        "engine": {"name": "pypdfium2", "version": version("pypdfium2")},
        "render_scale": render_scale,
        "status": status,
        "pages": pages,
        "errors": errors,
    }
    manifest_path = temporary / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        load_page_visual_evidence(
            manifest_path, expected_source_sha256=source_hash
        )
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination / "manifest.json"


def load_page_visual_evidence(
    manifest_path: str | Path,
    *,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """验证清单、相对路径、页码、尺寸和每张原页图片哈希。"""

    path = Path(manifest_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PageVisualEvidenceError("无法读取原页图片证据") from exc
    if not isinstance(value, dict) or value.get("evidence_version") != EVIDENCE_VERSION:
        raise PageVisualEvidenceError("原页图片证据版本不受支持")
    source_hash = value.get("source_sha256")
    if not isinstance(source_hash, str) or len(source_hash) != 64:
        raise PageVisualEvidenceError("原页图片证据源哈希无效")
    if expected_source_sha256 is not None and source_hash != expected_source_sha256:
        raise PageVisualEvidenceError("原页图片证据与源文件哈希不一致")
    pages = value.get("pages")
    if not isinstance(pages, list):
        raise PageVisualEvidenceError("原页图片证据页面必须是数组")
    seen: set[int] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise PageVisualEvidenceError("原页图片记录无效")
        number = page.get("page_number")
        if not isinstance(number, int) or number < 1 or number in seen:
            raise PageVisualEvidenceError("原页图片页码无效或重复")
        seen.add(number)
        relative = page.get("image_path")
        if not isinstance(relative, str) or "\\" in relative:
            raise PageVisualEvidenceError("原页图片路径无效")
        posix = PurePosixPath(relative)
        if posix.is_absolute() or ".." in posix.parts:
            raise PageVisualEvidenceError("原页图片路径越界")
        image_path = path.parent.joinpath(*posix.parts)
        if not image_path.is_file():
            raise PageVisualEvidenceError("原页图片文件缺失")
        if sha256(image_path.read_bytes()).hexdigest() != page.get("image_sha256"):
            raise PageVisualEvidenceError("原页图片哈希不一致")
        if any(
            not isinstance(page.get(field), (int, float)) or page[field] <= 0
            for field in (
                "width_pixels",
                "height_pixels",
                "width_points",
                "height_points",
            )
        ):
            raise PageVisualEvidenceError("原页图片尺寸无效")
    if value.get("status") not in {"succeeded", "partial", "failed"}:
        raise PageVisualEvidenceError("原页图片证据状态无效")
    if not isinstance(value.get("errors"), list):
        raise PageVisualEvidenceError("原页图片证据错误列表无效")
    return value
