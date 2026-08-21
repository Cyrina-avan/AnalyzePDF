"""兼容旧命令；核心实现位于 analyzepdf.parsers.docling。"""

from analyzepdf.parsers.docling import main


if __name__ == "__main__":
    raise SystemExit(main())
