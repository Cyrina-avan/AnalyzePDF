"""生成不依赖主解析器的逐页 PDF 原生文字证据。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

import pypdf
from pypdf import PdfReader


EVIDENCE_VERSION = "1.0"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class NativeTextEvidenceError(ValueError):
    """原生文字证据无法安全生成或读取。"""


def extract_native_text_evidence(
    source_pdf: str | Path,
    output_path: str | Path,
    *,
    document_id: str,
    source_ref: str,
) -> Path:
    """提取逐页文字层；读取失败也发布机器可读的 failed 证据。"""

    source = Path(source_pdf)
    destination = Path(output_path)
    if destination.exists():
        raise NativeTextEvidenceError("原生文字证据输出已经存在")
    payload_bytes = source.read_bytes()
    source_hash = sha256(payload_bytes).hexdigest()
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    status = "failed"
    try:
        reader = PdfReader(source)
        if reader.is_encrypted:
            errors.append({"code": "PDF_ENCRYPTED", "stage": "input"})
        else:
            for page_number, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text(extraction_mode="layout") or ""
                except Exception:
                    errors.append(
                        {
                            "code": "PAGE_TEXT_EXTRACTION_FAILED",
                            "stage": "extract",
                            "page_number": page_number,
                        }
                    )
                    continue
                pages.append(
                    {
                        "page_number": page_number,
                        "text": text,
                        "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
                    }
                )
            status = "partial" if errors else "succeeded"
    except Exception:
        errors.append({"code": "PDF_TEXT_LAYER_UNREADABLE", "stage": "input"})

    payload = {
        "evidence_version": EVIDENCE_VERSION,
        "document_id": document_id,
        "source_ref": source_ref,
        "source_sha256": source_hash,
        "engine": {"name": "pypdf", "version": pypdf.__version__},
        "status": status,
        "pages": pages,
        "errors": errors,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    load_native_text_evidence(destination, expected_source_sha256=source_hash)
    return destination


def load_native_text_evidence(
    path: str | Path,
    *,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """读取并检查逐页原生文字证据。"""

    evidence_path = Path(path)
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeTextEvidenceError("无法读取原生文字证据") from exc
    if not isinstance(payload, dict) or payload.get("evidence_version") != EVIDENCE_VERSION:
        raise NativeTextEvidenceError("原生文字证据版本不受支持")
    source_hash = payload.get("source_sha256")
    if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
        raise NativeTextEvidenceError("原生文字证据源哈希无效")
    if expected_source_sha256 is not None and source_hash != expected_source_sha256:
        raise NativeTextEvidenceError("原生文字证据与统一结果源哈希不一致")
    engine = payload.get("engine")
    if not isinstance(engine, dict) or not all(
        isinstance(engine.get(key), str) and engine[key]
        for key in ("name", "version")
    ):
        raise NativeTextEvidenceError("原生文字证据引擎信息无效")
    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise NativeTextEvidenceError("原生文字证据页面必须是数组")
    seen: set[int] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise NativeTextEvidenceError("原生文字证据页面无效")
        number = page.get("page_number")
        text = page.get("text")
        digest = page.get("text_sha256")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or number in seen
        ):
            raise NativeTextEvidenceError("原生文字证据页码无效或重复")
        if not isinstance(text, str) or not isinstance(digest, str):
            raise NativeTextEvidenceError("原生文字证据页面内容无效")
        if sha256(text.encode("utf-8")).hexdigest() != digest:
            raise NativeTextEvidenceError("原生文字证据页面哈希不一致")
        seen.add(number)
    if payload.get("status") not in {"succeeded", "partial", "failed"}:
        raise NativeTextEvidenceError("原生文字证据状态无效")
    if not isinstance(payload.get("errors"), list):
        raise NativeTextEvidenceError("原生文字证据错误列表无效")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args(argv)
    try:
        path = extract_native_text_evidence(
            args.source_pdf,
            args.output,
            document_id=args.document_id,
            source_ref=args.source_ref,
        )
        payload = load_native_text_evidence(path)
    except (NativeTextEvidenceError, OSError) as exc:
        parser.error(str(exc))
    print(
        f"原生文字证据完成：status={payload['status']} pages={len(payload['pages'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
