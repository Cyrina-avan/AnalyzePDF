"""Verify the frozen Paddle runtime before any research input is parsed."""

from __future__ import annotations

from runtime_bootstrap import configure_cuda_dll_directories


def main() -> int:
    directories = configure_cuda_dll_directories()
    import paddle
    import paddleocr
    import paddlex

    print(f"paddle={paddle.__version__}")
    print(f"paddleocr={paddleocr.__version__}")
    print(f"paddlex={paddlex.__version__}")
    print(f"cuda_compiled={paddle.device.is_compiled_with_cuda()}")
    print(f"device={paddle.device.get_device()}")
    print(f"runtime_dll_directory_count={len(directories)}")
    paddle.utils.run_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
