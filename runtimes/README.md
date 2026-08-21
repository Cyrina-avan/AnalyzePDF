# 独立解析器环境

大型解析器各自使用独立的 `uv` 环境，避免 PyTorch（深度学习运行库）、PaddlePaddle（飞桨深度学习框架）和主 Docling 环境互相覆盖。

| 目录 | 当前身份 | 是否进入自动路线 |
|---|---|---|
| `mineru/` | Docling 异常时的备用解析器 | 是 |
| `ppstructure/` | 已结束三路线实验的历史环境 | 否 |

每个目录中的 `pyproject.toml` 和 `uv.lock` 负责跨机器恢复；`.venv/`、模型、缓存和解析结果不进入 Git。

`export_native.py` 负责把解析器输出收窄为安全原始包，不保存本机绝对路径、原文件名或原始错误堆栈。`self_check.py` 必须完成真实 GPU（图形处理器）小计算，不能只检查“看见显卡”或“成功导入软件包”。

生产入口只能从 `analyzepdf.pipeline` 调用 MinerU。EmergentKB 不得直接调用本目录中的任何脚本。
