# AnalyzePDF

使用 Docling 解析 PDF，导出 Markdown（可选 JSON、表格 CSV、图片）。  
支持 Windows / Linux / macOS；设备默认 `auto`（有 CUDA/MPS 时使用可用加速，否则 CPU）。

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

macOS 会使用 PyTorch 官方平台 wheel，不会从 CUDA index 安装；如果当前进程无法使用 MPS，请显式传 `--device cpu`。

首次运行会从 HuggingFace 拉布局/表格等模型。国内网络若直连较慢，可耐心等待；**不要**全局设置 `HF_ENDPOINT=https://hf-mirror.com`，Docling 所需模型在部分镜像上不可用，会导致下载失败。

AnalyzePDF 默认会忽略 shell 里的 `HF_ENDPOINT`，改从官方 Hub 下载。若你确认镜像完整可用，可显式保留：

```bash
export ANALYZEPDF_USE_HF_MIRROR=1
export HF_ENDPOINT=https://hf-mirror.com
```

如需加速其他 HuggingFace 资源，可在 AnalyzePDF 之外单独使用镜像；解析 PDF 时建议不设 `HF_ENDPOINT`。

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

单份文档默认最多处理 300 秒。可用 `--document-timeout` 调整；超时配置会进入
运行指纹，修改时限后不会误用旧产物。若超时时已经产生可用正文，结果记为
`partial` 并保留内容，同时返回非零退出码：

```powershell
uv run python analyzePDF.py your-file.pdf --document-timeout 120
```

大量逐页超时提示在控制台只显示前 10 条，完整机器错误仍全部保存在 `run.json`。

超时结果会受文档复杂度、硬件和当时负载影响。很短的时限只适合可控实验，不能直接当作跨机器性能标准。

在 Windows 上，`docling-parse` 使用原生组件读取 PDF。当前已验证的版本在虚拟环境或源 PDF 路径含非 ASCII 字符（例如中文目录名）时可能无法读取本应存在的资源或源文件。AnalyzePDF 会继续使用 `pypdfium2` 备用方法，并把主方法的去敏诊断保存在 `run.json` 的 `parser.diagnostics` 中；成功的备用解析不会因此被误标为部分成功。需要验证主方法时，应让 uv 虚拟环境与待测 PDF 都位于纯英文路径。仓库文档不得记录具体本机路径。

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
        source.txt          # 原始文件名 + 源内容 SHA-256；不记录绝对路径
        run.json            # 成功/部分成功/失败状态、去敏错误、指纹与产物哈希
```

`--slim` 模式仅保留：

```text
output/
  sample_ab12cd34/
    content.md
    source.txt
```

文件名过长或含特殊字符时会缩短目录名，并附带路径摘要 hash。  
断点续跑：仅当状态为 `succeeded`，且源内容 SHA-256、解析脚本 SHA-256、OCR/输出配置和全部产物哈希均匹配时才跳过；`partial` / `failed` 会在下次运行时重新处理。旧版输出没有 `run.json`，首次会自动重转。仍可用 `--force` 强制重转。
重新解析会先清理本工具管理的旧 Markdown、JSON、表格、图片和运行元数据，避免输出配置变化后残留过期 Artifact；输出目录中的其他用户文件不会被删除。
解析失败仍会发布仅含源内容哈希、请求指纹、处理时间和去敏机器错误的 `run.json`；不会把源文件名、绝对路径或 traceback 写入运行记录。若 Docling 返回可用文档但状态为 `PARTIAL_SUCCESS`，或个别表格导出失败，则保留可用输出并将运行状态记为 `partial`。
如果解析后 Markdown 正文为空，即使底层解析器声称成功也会改记为 `failed` 并清除空产物。未开启 OCR 时错误码为 `OCR_REQUIRED`，提示用 `--use-ocr` 重试；开启 OCR 后仍为空则记为 `OCR_EMPTY_OUTPUT`。
单个文件失败不中断整批；若存在部分成功或失败，进程退出码为 `1`。

部分异常 PDF 会自动改用 `pypdfium2`。
会自动忽略 Office 临时文件（`~$*.pdf`）。

## 注意

- 不要设置 `DOCLING_DEVICE=CPU`（大写）这类变量，可能在 import 阶段触发 pydantic 问题；请用本工具的 `--device` / `ANALYZEPDF_DEVICE`。
- 解析只忠实还原 PDF 内容；源文件本身错档/串文无法在本层自动纠正。

## 自检

回归测试只使用标准库测试框架，不会新增依赖或下载模型：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests -v
```
