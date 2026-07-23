# AnalyzePDF

使用 Docling 解析 PDF，导出 Markdown、JSON、表格 CSV 和图片。

## 环境准备

```powershell
uv sync
```

## 使用方法

处理单个 PDF：

```powershell
uv run python analyzePDF.py your-file.pdf
```

批量处理某个文件夹下的所有 PDF：

```powershell
uv run python analyzePDF.py --input-dir pdfs
```

扫描版 PDF 需要 OCR 时：

```powershell
uv run python analyzePDF.py your-file.pdf --use-ocr
uv run python analyzePDF.py --input-dir pdfs --use-ocr
```

自定义输出根目录：

```powershell
uv run python analyzePDF.py your-file.pdf --output-dir results
```

## 输出说明

每个 PDF 会写入独立目录，不会互相覆盖：

```text
output/
  Report_Bitsight_State_of_the_Underground_2026/
    content.md
    content.json
    artifacts/
    tables/
    source.txt
```

例如处理 `sample.pdf` 后，结果在 `output/sample/`。Markdown 中的图片引用为 `artifacts/image_xxx.png`。

文件名过长或含 `[]`、空格等特殊字符时，会自动整理输出目录名，并在 `source.txt` 记录原始 PDF 名称。批量处理时，单个文件失败不会中断后续文件。部分结构异常的 PDF 会自动切换到备用解析器 `pypdfium2`。
