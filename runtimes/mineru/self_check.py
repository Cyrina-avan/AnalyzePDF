"""Verify the frozen MinerU runtime before any research input is parsed."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys

from runtime_bootstrap import configure_cuda_dll_directories, configure_fasttext_model_path


def main() -> int:
    directories = configure_cuda_dll_directories()
    fasttext_model = configure_fasttext_model_path()
    from fast_langdetect import detect_language
    import lmdeploy
    import mineru
    import torch
    import torchvision
    import transformers

    print(f"mineru={importlib.metadata.version('mineru')}")
    print(f"lmdeploy={lmdeploy.__version__}")
    print(f"torch={torch.__version__}")
    print(f"torchvision={torchvision.__version__}")
    print(f"transformers={transformers.__version__}")
    print(f"cuda_built={torch.backends.cuda.is_built()}")
    print(f"cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"device={torch.cuda.get_device_name(0)}")
        left = torch.tensor([[1.0, 2.0], [3.0, 4.0]], device="cuda")
        right = torch.tensor([[5.0, 6.0], [7.0, 8.0]], device="cuda")
        actual = left @ right
        expected = torch.tensor([[19.0, 22.0], [43.0, 50.0]], device="cuda")
        if not torch.equal(actual, expected):
            raise RuntimeError(f"Unexpected CUDA matrix result: {actual.cpu().tolist()}")
        print(f"cuda_matrix_multiply={actual.cpu().tolist()}")
    else:
        raise RuntimeError("CUDA GPU is required for the MinerU runtime self-check")
    print(f"runtime_dll_directory_count={len(directories)}")
    if detect_language("可控边缘文件验收基准") != "ZH":
        raise RuntimeError("fastText language detection returned an unexpected result")
    print(f"fasttext_model_ascii_path={fasttext_model.as_posix().isascii()}")
    print("fasttext_language_detection=ZH")

    cli = subprocess.run(
        [sys.executable, "-m", "mineru.cli.client", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"cli_version={cli.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
