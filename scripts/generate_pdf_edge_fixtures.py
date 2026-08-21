"""生成安全的 PDF 边缘测试样本包。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from analyzepdf.fixtures.pdf_edges import (
    FixtureGenerationError,
    generate_pdf_edge_fixture_pack,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--secret-env",
        default="EMERGENT_KB_FIXTURE_PASSWORD",
        help="Name of the environment variable containing the test-only encryption secret",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    secret = os.environ.get(args.secret_env)
    if not secret:
        print(
            f"缺少环境变量 {args.secret_env}；不会生成或记录默认密码。",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = generate_pdf_edge_fixture_pack(
            args.output_dir,
            encryption_secret=secret,
            overwrite=args.overwrite,
        )
    except (FixtureGenerationError, OSError) as exc:
        print(f"测试文件生成失败：{exc}", file=sys.stderr)
        return 1
    print(f"测试文件生成完成；验收清单：{manifest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
