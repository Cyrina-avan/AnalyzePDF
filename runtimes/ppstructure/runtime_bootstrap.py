"""Make CUDA runtime wheels discoverable without changing machine-wide settings."""

from __future__ import annotations

import os
from pathlib import Path
import sys


_DLL_DIRECTORY_HANDLES: list[object] = []


def configure_cuda_dll_directories() -> tuple[str, ...]:
    """Add runtime-local NVIDIA DLL folders for this process only."""

    if sys.platform != "win32":
        return ()
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    directories = sorted(
        path for path in root.glob("*/bin") if path.is_dir()
    )
    if not directories:
        raise RuntimeError("Runtime-local NVIDIA DLL directories are missing")
    for directory in directories:
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(directory)))
    os.environ["PATH"] = os.pathsep.join(
        [*(str(path) for path in directories), os.environ.get("PATH", "")]
    )
    return tuple(path.relative_to(root).as_posix() for path in directories)
