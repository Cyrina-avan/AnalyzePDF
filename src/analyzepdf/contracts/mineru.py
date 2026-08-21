"""把 MinerU 解析结果转换为统一文档结果。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from analyzepdf.contracts.validation import ContractValidationError
from analyzepdf.contracts.ppstructure import (
    PPStructureAdapterError,
    adapt_ppstructure_output,
)


class MinerUAdapterError(ValueError):
    """Raised when a MinerU native package cannot safely become Contract v1."""


def adapt_mineru_output(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    document_id: str,
    source_ref: str,
    language: str,
) -> Path:
    """Validate and adapt one normalized MinerU package.

    MinerU and PP-StructureV3 deliberately publish the same narrow intermediate
    schema.  Reusing the already-hardened schema adapter keeps inventory,
    privacy, partial-result, coordinate, OCR, and atomic-publication semantics
    identical across the two research routes.
    """

    input_path = Path(input_dir).resolve()
    try:
        run = json.loads((input_path / "run.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise MinerUAdapterError("MinerU run.json is missing or unreadable") from exc
    except json.JSONDecodeError as exc:
        raise MinerUAdapterError("MinerU run.json is not valid JSON") from exc
    if not isinstance(run, dict):
        raise MinerUAdapterError("MinerU run.json must contain an object")
    parser = run.get("parser")
    if not isinstance(parser, dict) or parser.get("name") != "MinerU":
        raise MinerUAdapterError("Input is not a MinerU normalized native package")

    try:
        return adapt_ppstructure_output(
            input_path,
            output_dir,
            document_id=document_id,
            source_ref=source_ref,
            language=language,
        )
    except PPStructureAdapterError as exc:
        message = str(exc).replace("PP-Structure", "normalized MinerU")
        raise MinerUAdapterError(message) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--language", required=True)
    args = parser.parse_args(argv)
    try:
        contract_path = adapt_mineru_output(
            args.input_dir,
            args.output_dir,
            document_id=args.document_id,
            source_ref=args.source_ref,
            language=args.language,
        )
    except (MinerUAdapterError, ContractValidationError, OSError) as exc:
        print(f"MinerU 适配失败：{exc}", file=sys.stderr)
        return 1
    print(f"统一契约包已生成：{contract_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
