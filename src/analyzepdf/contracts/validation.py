"""在 AnalyzePDF 发布统一结果前进行独立检查。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


CONTRACT_VERSION = "1.0"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_STATUSES = {"succeeded", "partial", "failed"}


class ContractValidationError(ValueError):
    """统一结果不完整或不符合发布要求。"""


@dataclass(frozen=True, slots=True)
class ContractDocument:
    """质量检查和测试需要使用的统一结果摘要。"""

    document_id: str
    source_ref: str
    source_sha256: str
    media_type: str
    status: str
    text: str | None
    text_sha256: str | None
    language: str | None
    parser_name: str
    parser_version: str
    engine_name: str
    engine_version: str
    page_numbers: tuple[int, ...]
    artifact_paths: tuple[str, ...]


def load_contract(
    path: str | Path,
    *,
    verify_artifacts: bool = True,
) -> ContractDocument:
    """读取并检查 AnalyzePDF 即将发布的统一结果。"""

    contract_path = Path(path)
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractValidationError("Cannot read parsed document") from exc
    except json.JSONDecodeError as exc:
        raise ContractValidationError("Invalid parsed document JSON") from exc
    if not isinstance(payload, dict):
        raise ContractValidationError("Parsed document must be an object")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise ContractValidationError("Unsupported contract_version")

    document_id = _identifier(payload.get("document_id"), "document_id")
    source = _mapping(payload.get("source"), "source")
    source_ref = _identifier(source.get("source_ref"), "source.source_ref")
    source_sha256 = _digest(source.get("content_sha256"), "source.content_sha256")
    media_type = _nonempty_string(source.get("media_type"), "source.media_type")

    parser = _mapping(payload.get("parser_run"), "parser_run")
    parser_name = _nonempty_string(parser.get("parser_name"), "parser_name")
    parser_version = _nonempty_string(parser.get("parser_version"), "parser_version")
    engine_name = _nonempty_string(parser.get("engine_name"), "engine_name")
    engine_version = _nonempty_string(parser.get("engine_version"), "engine_version")

    status = payload.get("status")
    if status not in _STATUSES:
        raise ContractValidationError("Unsupported status")

    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise ContractValidationError("pages must be an array")
    page_numbers: list[int] = []
    for index, page in enumerate(pages):
        item = _mapping(page, f"pages[{index}]")
        number = item.get("page_number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ContractValidationError("Invalid page_number")
        page_numbers.append(number)
    if len(set(page_numbers)) != len(page_numbers):
        raise ContractValidationError("Duplicate page_number")

    text: str | None = None
    text_sha256: str | None = None
    language: str | None = None
    if "text" in payload:
        text_record = _mapping(payload["text"], "text")
        text = _nonempty_string(text_record.get("content"), "text.content")
        text_sha256 = _digest(text_record.get("content_sha256"), "text.content_sha256")
        if sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            raise ContractValidationError("text.content_sha256 mismatch")
        language = _nonempty_string(text_record.get("language"), "text.language")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise ContractValidationError("artifacts must be an array")
    artifact_paths: list[str] = []
    for index, artifact in enumerate(artifacts):
        item = _mapping(artifact, f"artifacts[{index}]")
        relative = _safe_relative_path(item.get("path"), f"artifacts[{index}].path")
        expected_hash = _digest(
            item.get("content_sha256"), f"artifacts[{index}].content_sha256"
        )
        artifact_paths.append(relative)
        if verify_artifacts:
            artifact_path = contract_path.parent / PurePosixPath(relative)
            if not artifact_path.is_file():
                raise ContractValidationError(f"Artifact is missing: {relative}")
            if sha256(artifact_path.read_bytes()).hexdigest() != expected_hash:
                raise ContractValidationError(f"content_sha256 mismatch: {relative}")

    if status == "failed" and (text is not None or page_numbers):
        raise ContractValidationError("Failed result must not publish parsed content")
    if status != "failed" and (text is None or not page_numbers):
        raise ContractValidationError("Successful result must contain text and pages")

    return ContractDocument(
        document_id=document_id,
        source_ref=source_ref,
        source_sha256=source_sha256,
        media_type=media_type,
        status=status,
        text=text,
        text_sha256=text_sha256,
        language=language,
        parser_name=parser_name,
        parser_version=parser_version,
        engine_name=engine_name,
        engine_version=engine_version,
        page_numbers=tuple(sorted(page_numbers)),
        artifact_paths=tuple(artifact_paths),
    )


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractValidationError(f"{context} must be an object")
    return value


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ContractValidationError(f"Invalid {context}")
    return value


def _digest(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractValidationError(f"Invalid {context}")
    return value


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"Invalid {context}")
    return value


def _safe_relative_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"Invalid {context}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ContractValidationError(f"Unsafe {context}")
    return path.as_posix()
