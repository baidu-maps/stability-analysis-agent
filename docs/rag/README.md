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
pip install stability-analysis-agent
# 源码开发：pip install -e .
```

说明：
- 核心包默认不强制安装 ChromaDB / sentence-transformers，避免 ML 栈导入失败阻断基础分析。
- 默认使用简单哈希嵌入，**无需**下载 HuggingFace 模型；预训练嵌入需设置 `AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1`。
- ML 栈版本：`numpy<2`、`torch>=2.4`、`transformers<4.52`、`sentence-transformers<3`、`accelerate>=0.26`（默认由 `pyproject.toml` 安装，也可参考 `requirements-rag.txt`）。
- 版本冲突排错见 [../cli/INSTALL_TROUBLESHOOTING.md](../cli/INSTALL_TROUBLESHOOTING.md)。

### 2) 初始化本地向量库

```bash
python3 rag/init_vector_db_data.py
```

说明：
- 该脚本会执行 `clear_all()`，然后写入规则/模式/证据/策略/指导片段种子。
- 默认持久化目录是 `./vector_db`（SQLite + Chroma）。

### 2.1) 不指定路径时，DB 文件如何创建

当你不显式指定 `vector_db_path` 时，`AIStabilityAnalyzerWithVectorDB` 默认使用 `./vector_db`。  
在以下任一操作发生时，会自动创建本地数据库文件与向量索引目录：

- 运行 `python3 rag/init_vector_db_data.py`（初始化内置种子）
- 调用 `import_snapshot(...)`（从 JSON 快照导入）
- 通过 CLI 执行导入（例如 `--import-vector-db`）

典型落盘结果包括：

- `./vector_db/metadata.sqlite3`（规则、模式、证据、策略、反馈、指导片段等元数据）
- `./vector_db/chroma.sqlite3` 及 Chroma 向量索引目录

如果你希望把 DB 放到其他目录，请在实例化时传入 `vector_db_path`：

```python
from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB
analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path="/path/to/your/vector_db")
```

### 3) （可选）导入自定义快照

可通过 `AIStabilityAnalyzerWithVectorDB.import_snapshot()` 导入你自己的知识快照，不要求先下载现成数据库文件。

示例（从快照创建本地 DB）：

```python
import json
from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB

analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path="./vector_db")
with open("vector_db_snapshot_latest.json", "r", encoding="utf-8") as f:
    snapshot = json.load(f)
result = analyzer.import_snapshot(snapshot)
print(result)
```

## 是否需要下载模型/数据库

- 不需要下载“向量数据库文件”：默认本地初始化即可。
- 可能需要下载 embedding 模型（可选）：
  - 默认不启用 `SentenceTransformer`，使用本地哈希嵌入（离线可用）。
  - 设置 `AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1` 后，才会尝试加载/下载 `all-MiniLM-L6-v2`。

## 当前支持的向量数据库类型

当前实现是固定技术栈：

- 向量索引：本地持久化 **ChromaDB**
- 元数据存储：本地 **SQLite**（`metadata.sqlite3`）

目前不支持在配置中直接切换到其他向量后端（如 Milvus、Weaviate、Pinecone 等）。

## 修复后写入向量知识库

**默认不自动写库**。仅在 `scope=full` 且 `08_apply_ai_fixes.json` 中 `success=true` 时，由用户确认是否将案例写入向量库。

| 入口 | 行为 |
|------|------|
| **CLI 交互** | 修复成功后提示「是否将此次修复案例写入向量知识库？」（默认否） |
| **CLI 非交互** | 默认跳过；`--save-to-vector-db` 强制写入，`--no-save-to-vector-db` 显式跳过 |
| **Web 面板** | 改码完成后结果区展示「写入 / 暂不」，调用 Daemon API 写库 |
| **Daemon 子进程** | 固定 `--no-interactive --no-save-to-vector-db`，不在子进程弹窗 |

写库实现：`rag/case_writer.py`（从报告目录构建 pattern + evidence）+ `rag/vector_store_config.py`（`local` / `remote` 配置）。

- **local**（本期）：ChromaDB + SQLite，默认路径 `~/.config/stability-analysis-agent/vector_db`
- **remote**（预留）：配置 `mode=remote` 时返回 501 / 明确报错

路径优先级：`--vector-db-path` > `STABILITY_AGENT_VECTOR_DB_PATH` > `web_preferences.vector_db.local_path` > 默认目录。

审计落盘：`reports/.../09_vector_db_commit.json`（`status`、`pattern_id`、`vector_db_path`）。

CLI 示例：

```bash
# 交互确认（TTY）
python3 cli/main.py --crash-log-file ... --library-dir ... --code-roots ... --scope full

# 脚本强制写入
python3 cli/main.py ... --scope full --save-to-vector-db --no-interactive
```

Web / Daemon：`POST /runs/<run_id>/vector-db/commit`（见 [DAEMON_SERVER_GUIDE.md](../cli/DAEMON_SERVER_GUIDE.md)）。

## 当前接入状态说明

当前仓库中的 `rag/` 已具备完整实现与初始化脚本；但统一入口 `cli/main.py` 尚未暴露历史文档中的 `--init-vector-db`、`--vector-db-stats` 这类参数。  
建议把 RAG 初始化和运维操作定位为：

- Python API（`AIStabilityAnalyzerWithVectorDB`）方式，或
- 单独运行 `rag/init_vector_db_data.py` 方式。

后续如果要统一到 CLI，可在 `cli/main.py` 增加显式子命令（如 `rag init/stats/export/import`）。
