"""从 PDF 自带文字层提取带位置的表号、续表标记和图号证据。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

import pypdf
from pypdf import PdfReader


EVIDENCE_VERSION = "1.0"
_TABLE_LABEL_RE = re.compile(
    r"^\s*table\s+(\d+)(?:"
    r"(?P<punctuated>\s*[.:])|"
    r"(?P<continued>\s+(?:continued|cont\.?)"
    r"(?:\s+on\s+(?:the\s+)?next\s+page)?\s*$)|"
    r"(?P<bare>\s*$)"
    r")",
    re.IGNORECASE,
)
_FIGURE_LABEL_RE = re.compile(
    r"^\s*fig(?:ure)?\.?\s*(\d+)\s*[.:]", re.IGNORECASE
)


class NativeLabelEvidenceError(ValueError):
    """原生标签证据无法安全生成或读取。"""


def extract_native_label_evidence(
    source_pdf: str | Path,
    output_path: str | Path,
    *,
    document_id: str,
    source_ref: str,
) -> Path:
    """提取精确标签及其 PDF 坐标；读取失败时发布安全失败状态。"""

    source = Path(source_pdf)
    destination = Path(output_path)
    if destination.exists():
        raise NativeLabelEvidenceError("原生标签证据输出已经存在")
    source_hash = sha256(source.read_bytes()).hexdigest()
    labels: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    status = "failed"
    try:
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted:
            errors.append({"code": "PDF_ENCRYPTED", "stage": "input"})
        else:
            for page_number, page in enumerate(reader.pages, start=1):
                page_width = float(page.mediabox.width)
                page_height = float(page.mediabox.height)

                def visit(
                    text: str,
                    _cm: list[float],
                    tm: list[float],
                    _font: dict[str, Any] | None,
                    font_size: float,
                ) -> None:
                    compact_text = " ".join(text.split())
                    for object_type, pattern in (
                        ("table", _TABLE_LABEL_RE),
                        ("figure", _FIGURE_LABEL_RE),
                    ):
                        match = pattern.match(compact_text)
                        if match is None:
                            continue
                        row: dict[str, Any] = {
                            "object_type": object_type,
                            "label_number": int(match.group(1)),
                            "matched_text": match.group(0),
                            "page_number": page_number,
                            "page_width": page_width,
                            "page_height": page_height,
                            "x": float(tm[4]),
                            "y": float(tm[5]),
                            "x_normalized": float(tm[4]) / page_width,
                            "y_normalized": (page_height - float(tm[5])) / page_height,
                            "font_size": float(font_size),
                            "coordinate_origin": "bottom-left",
                            "coordinate_unit": "pt",
                        }
                        label_width = max(
                            float(font_size),
                            min(240.0, len(match.group(0)) * float(font_size) * 0.6),
                        )
                        row["bbox_normalized"] = {
                            "x0": max(0.0, float(tm[4]) / page_width),
                            "y0": max(
                                0.0,
                                (page_height - (float(tm[5]) + float(font_size)))
                                / page_height,
                            ),
                            "x1": min(1.0, (float(tm[4]) + label_width) / page_width),
                            "y1": min(
                                1.0,
                                (
                                    page_height
                                    - (float(tm[5]) - max(1.0, float(font_size) * 0.25))
                                )
                                / page_height,
                            ),
                        }
                        if object_type == "table":
                            row["label_form"] = next(
                                name
                                for name in ("punctuated", "continued", "bare")
                                if match.groupdict().get(name) is not None
                            )
                        labels.append(row)
                        break

                try:
                    page.extract_text(visitor_text=visit)
                except Exception:
                    errors.append(
                        {
                            "code": "PAGE_LABEL_EXTRACTION_FAILED",
                            "stage": "extract",
                            "page_number": page_number,
                        }
                    )
            status = "partial" if errors else "succeeded"
    except Exception:
        errors.append({"code": "PDF_LABEL_LAYER_UNREADABLE", "stage": "input"})

    labels.sort(
        key=lambda item: (
            item["page_number"],
            -item["y"],
            item["x"],
            item["object_type"],
            item["label_number"],
        )
    )
    payload = {
        "evidence_version": EVIDENCE_VERSION,
        "document_id": document_id,
        "source_ref": source_ref,
        "source_sha256": source_hash,
        "engine": {"name": "pypdf", "version": pypdf.__version__},
        "status": status,
        "labels": labels,
        "errors": errors,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(destination)
    load_native_label_evidence(destination, expected_source_sha256=source_hash)
    return destination


def load_native_label_evidence(
    path: str | Path, *, expected_source_sha256: str | None = None
) -> dict[str, Any]:
    """验证标签字段、页码、坐标、来源身份和有限标签类型。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeLabelEvidenceError("无法读取原生标签证据") from exc
    if not isinstance(payload, dict) or payload.get("evidence_version") != EVIDENCE_VERSION:
        raise NativeLabelEvidenceError("原生标签证据版本不受支持")
    source_hash = payload.get("source_sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise NativeLabelEvidenceError("原生标签证据源哈希无效")
    if expected_source_sha256 is not None and source_hash != expected_source_sha256:
        raise NativeLabelEvidenceError("原生标签证据与源文件哈希不一致")
    labels = payload.get("labels")
    if not isinstance(labels, list):
        raise NativeLabelEvidenceError("原生标签证据列表无效")
    for label in labels:
        if not isinstance(label, dict) or label.get("object_type") not in {"table", "figure"}:
            raise NativeLabelEvidenceError("原生标签类型无效")
        if label.get("object_type") == "table" and label.get("label_form") not in {
            "punctuated",
            "continued",
            "bare",
        }:
            raise NativeLabelEvidenceError("原生表格标签形式无效")
        if (
            not isinstance(label.get("label_number"), int)
            or label["label_number"] < 1
            or not isinstance(label.get("page_number"), int)
            or label["page_number"] < 1
            or label.get("coordinate_origin") != "bottom-left"
            or label.get("coordinate_unit") != "pt"
        ):
            raise NativeLabelEvidenceError("原生标签身份或坐标系无效")
        for key in (
            "x",
            "y",
            "x_normalized",
            "y_normalized",
            "font_size",
            "page_width",
            "page_height",
        ):
            if not isinstance(label.get(key), (int, float)):
                raise NativeLabelEvidenceError("原生标签坐标无效")
        if not (
            0 <= float(label["x"]) <= float(label["page_width"])
            and 0 <= float(label["y"]) <= float(label["page_height"])
            and float(label["font_size"]) > 0
        ):
            raise NativeLabelEvidenceError("原生标签坐标超出页面")
        expected_x = float(label["x"]) / float(label["page_width"])
        expected_y = (float(label["page_height"]) - float(label["y"])) / float(
            label["page_height"]
        )
        if (
            not 0 <= float(label["x_normalized"]) <= 1
            or not 0 <= float(label["y_normalized"]) <= 1
            or abs(float(label["x_normalized"]) - expected_x) > 1e-9
            or abs(float(label["y_normalized"]) - expected_y) > 1e-9
        ):
            raise NativeLabelEvidenceError("原生标签归一化坐标不一致")
        bbox = label.get("bbox_normalized")
        if not isinstance(bbox, dict) or not {"x0", "y0", "x1", "y1"} <= set(bbox):
            raise NativeLabelEvidenceError("原生标签缺少归一化范围")
        try:
            x0, y0, x1, y1 = (float(bbox[key]) for key in ("x0", "y0", "x1", "y1"))
        except (TypeError, ValueError) as exc:
            raise NativeLabelEvidenceError("原生标签归一化范围无效") from exc
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise NativeLabelEvidenceError("原生标签归一化范围越界")
    if payload.get("status") not in {"succeeded", "partial", "failed"}:
        raise NativeLabelEvidenceError("原生标签证据状态无效")
    if not isinstance(payload.get("errors"), list):
        raise NativeLabelEvidenceError("原生标签证据错误列表无效")
    return payload
