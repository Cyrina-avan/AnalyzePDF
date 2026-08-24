# AnalyzePDF

AnalyzePDF 是独立的企业文档解析项目。它负责把 PDF 变成统一、可检查、可回溯的文档结果；它不负责权限、知识抽取、索引或问答。

## 能力边界

AnalyzePDF 负责：

- 运行 Docling（文档解析工具）；
- 检查页数、正文、表格、图片、坐标和导出附件是否异常；
- Docling 异常时自动运行 MinerU（复杂文档解析工具）；
- 两条路线都不可靠时明确降级，不发布不可信正文；
- 输出 Parsed Document Contract v1（统一文档结果）和机器可读诊断；
- 生成解析器专用的 PDF 边缘测试样本。

AnalyzePDF 不负责：

- 用户、组织、权限和文件可见范围；
- 正文清洗、段落边界修复和跨页逻辑对象重建；
- 企业知识的抽取、合并和分类；
- 向量索引、检索、问答和下载授权。

正文清洗和文档结构修复属于 DocumentNormalization；知识抽取、检索和问答属于 EmergentKB。三个项目不共享 Python 环境，只通过有版本的机器可读结果连接。

## 当前路线

```text
PDF
 │
 ▼
Docling
 │
 ▼
程序质量检查
 ├─ 通过 ───────────────► 选择 Docling 结果
 └─ 可疑 / 失败
          │
          ▼
        MinerU
          │
          ▼
       同一套质量检查
          ├─ 通过 ──────► 选择 MinerU 结果
          └─ 不通过 ────► 安全降级，不发布正文
```

PP-StructureV3（飞桨文档结构解析第三版）只保留为历史研究路线，不进入当前自动解析链路。

## 目录

```text
src/analyzepdf/
  parsers/docling.py       Docling 主解析器
  quality.py               解析质量检查
  routing.py               主路线、备用路线和降级选择
  pipeline.py              完整自动解析入口
  native_text.py           PDF 原生文字旁证
  native_labels.py         PDF 定位标签旁证
  page_visual.py           PDF 原页图片旁证
  text_evidence.py         主解析正文与 PDF 原生文字比较
  contracts/               三种解析结果转统一格式
  fixtures/                可控 PDF 测试样本
runtimes/
  mineru/                  MinerU 独立 uv 环境
  ppstructure/             历史研究路线的独立 uv 环境
tests/                     不下载模型的自动化测试
analyzePDF.py              旧 Docling 命令的兼容入口
```

## 使用

首次准备主环境：

```powershell
uv sync
```

完整自动路线：

```powershell
uv run analyzepdf-pipeline document.pdf `
  --output-dir output/document-001 `
  --document-id document-001 `
  --source-ref source-001 `
  --language zh-CN
```

扫描件可加 `--use-ocr`。Docling 结果触发异常时，完整入口会自动使用 `runtimes/mineru` 中的独立环境。

只运行旧 Docling 路线：

```powershell
uv run python analyzePDF.py document.pdf --output-dir output
```

## 输出怎么看

完整入口生成：

```text
output/document-001/
  routing.json
  native-text-evidence.json  供下游清洗层交叉核对的逐页文字证据
  native-label-evidence.json 带归一化位置的精确表号、续表和图号
  page-visual-evidence/      逐页原页图片、尺寸和哈希
  routes/
    docling/
      native/              Docling 原始安全输出
      contract/            Docling 统一结果
    mineru/                只有触发备用路线时才出现
      native/              MinerU 原始安全输出
      contract/            MinerU 统一结果
```

`routing.json` 中：

- `selected_primary`：Docling 通过；
- `selected_fallback`：Docling 异常，MinerU 通过；
- `degraded`：两边都不可靠，不应把正文送进知识库；
- `selected_contract`：DocumentNormalization 输入适配器应读取的统一结果相对路径。

## 出问题时去哪里查

| 现象 | 负责位置 |
|---|---|
| Docling 解析失败或导出错误 | `src/analyzepdf/parsers/docling.py` |
| MinerU 没启动或运行环境异常 | `runtimes/mineru/` |
| 首行缩进、空正文、表格附件丢失没有被发现 | `src/analyzepdf/quality.py` |
| 明明异常却没有切换路线 | `src/analyzepdf/routing.py` |
| 两条路线的结果没有正确发布 | `src/analyzepdf/pipeline.py` |
| 统一结果字段、页码、坐标或附件映射错误 | `src/analyzepdf/contracts/` |
| 下游段落修复缺少原生文字证据 | `src/analyzepdf/native_text.py` |
| 主解析正文与 PDF 原生文字是否一致 | `src/analyzepdf/text_evidence.py` |
| 跨页表格和图注归属缺少带坐标标签 | `src/analyzepdf/native_labels.py` |
| 下游受约束识图缺少原页图片 | `src/analyzepdf/page_visual.py` |
| 空格、断行、段落边界或跨页逻辑对象错误 | 不在本仓库，去 DocumentNormalization |
| 权限、知识抽取、检索或问答错误 | 不在本仓库，去 EmergentKB |

## 实现状态

- Docling 原有解析能力：已实现并保留回归测试；
- Docling / MinerU 结果统一：已实现；
- 程序质量检查：已实现第一版；
- Docling 异常自动调用 MinerU：代码已接通并通过模拟路线测试；
- 真实中文电子 PDF 冒烟：质量警告与阻断错误已分开，`XZFG-0200.pdf` 真实运行退出码为 `0` 并选中 Docling；
- PDF 原生文字、定位标签和原页图片旁证：已接入统一入口；
- 主解析正文与 PDF 原生文字比较：代码和测试已归入本仓库，尚未改变正式路线选择；
- 逐页切换和逐页拼接：尚未实现，目前按整份文档切换；
- PP-StructureV3：只作历史研究保留。

任何文档和交接记录都必须区分“已经实现”“测试中”“计划”，不能把路线决定写成已经运行的能力。

## 测试

```powershell
uv run pytest -q
```

测试默认使用合成数据，不调用真实模型。模型、缓存、PDF 原件、输出、密钥和虚拟环境均不得提交 Git。
