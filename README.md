# AnalyzePDF

使用 Docling 解析 PDF，导出 Markdown（可选 JSON、表格 CSV、图片）。  
支持 Windows / Linux；设备默认 `auto`（有 CUDA 用 GPU，否则 CPU）。

## 环境准备

需要 Python >= 3.12，推荐 [uv](https://github.com/astral-sh/uv)。

```bash
# Windows / Linux 通用
uv sync
```

当前 `pyproject.toml` 默认拉取 **CUDA 版** PyTorch（本仓库开发机常用）。若目标机无 NVIDIA GPU：

1. 按目标平台改 `pyproject.toml` 里的 torch 源（或去掉 CUDA index，改用官方 CPU wheel）
2. 重新 `uv sync`
3. 运行时用 `--device cpu`（或 `ANALYZEPDF_DEVICE=cpu`）

首次运行会从 HuggingFace 拉布局/表格等模型；国内可设：

```bash
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1
```

PowerShell：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_HUB_DISABLE_XET = "1"
```

离线环境请预先打包模型缓存，并设置 `HF_HUB_OFFLINE=1`。

## 使用方法

单个 PDF：

```bash
uv run python analyzePDF.py your-file.pdf
```

批量（**默认递归**子目录中的 `*.pdf`）：

```bash
uv run python analyzePDF.py --input-dir pdfs --output-dir results
```

只处理一层目录：

```bash
uv run python analyzePDF.py --input-dir pdfs --no-recursive
```

扫描版开 OCR：

```bash
uv run python analyzePDF.py your-file.pdf --use-ocr
```

指定设备：

```bash
uv run python analyzePDF.py --input-dir pdfs --device auto   # 默认
uv run python analyzePDF.py --input-dir pdfs --device cuda
uv run python analyzePDF.py --input-dir pdfs --device cpu
# 或：export ANALYZEPDF_DEVICE=cpu
```

瘦输出（适合后续只抽正文 / 回传 Linux，体积小很多）：

```bash
uv run python analyzePDF.py --input-dir pdfs --output-dir results --slim
```

也可单独关闭产物：

```bash
uv run python analyzePDF.py report.pdf --no-json --no-images --no-tables
```

强制重转（忽略已有 `content.md`）：

```bash
uv run python analyzePDF.py --input-dir pdfs --output-dir results --force
```

## 输出说明

完整模式（递归时会镜像相对父目录，避免不同子目录同名 PDF 撞车）：

```text
output/
  Kaspersky/
    PDFs/
      report_ab12cd34/
        content.md
        content.json
        artifacts/          # 仅一份；由 Docling 写出，不再二次导出
        tables/
        source.txt          # 原始文件名 + 绝对路径
```

`--slim` 模式仅保留：

```text
output/
  sample_ab12cd34/
    content.md
    source.txt
```

文件名过长或含特殊字符时会缩短目录名，并附带路径摘要 hash。  
断点续跑：仅当 `content.md` 非空且 `source.txt` 第一行等于当前 PDF 文件名时才跳过；否则用 `--force` 重转。  
单个文件失败不中断整批；若有失败，进程退出码为 `1`。  
部分异常 PDF 会自动改用 `pypdfium2`。  
会自动忽略 Office 临时文件（`~$*.pdf`）。

## 注意

- 不要设置 `DOCLING_DEVICE=CPU`（大写）这类变量，可能在 import 阶段触发 pydantic 问题；请用本工具的 `--device` / `ANALYZEPDF_DEVICE`。
- 解析只忠实还原 PDF 内容；源文件本身错档/串文无法在本层自动纠正。
