# Stability Memory System（RAG）指南

## 概述

本项目中的 RAG 能力不是“纯向量检索”，而是三层组合：

- 规则优先（`RuleStore` / SQLite）
- 向量兜底（`PatternIndex` / ChromaDB）
- 元数据推理（`MetadataStore` / SQLite）

对应实现位于 `rag/` 目录（代码实现，不包含数据库文件）。

## 目录与职责

- `rag/vector_database_integration.py`：总入口，编排规则匹配、向量召回、证据/策略/反馈、快照导入导出。
- `rag/rule_store.py`：规则表读写（`crash_rules`）。
- `rag/metadata_store.py`：模式/证据/策略/反馈/指导片段等元数据表。
- `rag/pattern_index.py`：ChromaDB 向量索引与查询。
- `rag/feature_extractor.py`：从解析结果提取特征并生成检索 query。
- `rag/init_vector_db_data.py`：初始化脚本（先清空再写入内置种子数据）。

## 启用向量数据库：最小步骤

### 1) 安装依赖

```bash
pip install chromadb chroma-hnswlib sentence-transformers numpy
```

说明：
- 若你的环境里已有这些包，可跳过。
- 仓库文档里提到 `requirements_vector_db.txt`，若你本地不存在该文件，请按上面命令手动安装。

### 2) 初始化本地向量库

```bash
python3 rag/init_vector_db_data.py
```

说明：
- 该脚本会执行 `clear_all()`，然后写入规则/模式/证据/策略/指导片段种子。
- 默认持久化目录是 `./vector_db`（SQLite + Chroma）。

### 3) （可选）导入自定义快照

可通过 `AIStabilityAnalyzerWithVectorDB.import_snapshot()` 导入你自己的知识快照，不要求先下载现成数据库文件。

## 是否需要下载模型/数据库

- 不需要下载“向量数据库文件”：默认本地初始化即可。
- 可能需要下载 embedding 模型（可选）：
  - 默认不启用 `SentenceTransformer`，使用本地哈希嵌入（离线可用）。
  - 设置 `AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1` 后，才会尝试加载/下载 `all-MiniLM-L6-v2`。

## 当前接入状态说明

当前仓库中的 `rag/` 已具备完整实现与初始化脚本；但统一入口 `cli/main.py` 尚未暴露历史文档中的 `--init-vector-db`、`--vector-db-stats` 这类参数。  
建议把 RAG 初始化和运维操作定位为：

- Python API（`AIStabilityAnalyzerWithVectorDB`）方式，或
- 单独运行 `rag/init_vector_db_data.py` 方式。

后续如果要统一到 CLI，可在 `cli/main.py` 增加显式子命令（如 `rag init/stats/export/import`）。

