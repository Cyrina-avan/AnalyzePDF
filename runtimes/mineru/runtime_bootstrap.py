"""Make CUDA runtime wheels discoverable without changing machine-wide settings."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
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


def configure_fasttext_model_path() -> Path:
    """Expose fastText's language model through an ASCII-only Windows path.

    The native fastText loader cannot open its bundled model when this runtime
    lives below a directory containing non-ASCII characters.  Keep the Python
    environment where it is, copy only the small read-only model into the
    per-user application cache, and patch the process-local library constant.
    """

    import fast_langdetect.ft_detect.infer as infer

    source = Path(infer.__file__).resolve().parent / "resources" / "lid.176.ftz"
    if not source.is_file():
        raise RuntimeError("Bundled fastText language model is missing")
    if sys.platform != "win32" or source.as_posix().isascii():
        infer.LOCAL_SMALL_MODEL_PATH = source
        return source

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data or not local_app_data.isascii():
        raise RuntimeError("An ASCII-only local application cache is required")
    cache_dir = Path(local_app_data) / "emergent-kb" / "fast-langdetect"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "lid.176.ftz"
    if not target.is_file() or target.stat().st_size != source.stat().st_size:
        temporary = cache_dir / ".lid.176.ftz.tmp"
        shutil.copy2(source, temporary)
        temporary.replace(target)
    infer.LOCAL_SMALL_MODEL_PATH = target
    return target
