"""
stability_analyzer_agent.rag — 向量数据库集成（仅代码实现层）

【目录说明】
  本目录只包含 Python 实现代码，不存储任何数据库文件：
    - vector_database_integration.py  主集成类 StabilityMemorySystem / AICrashAnalyzerWithVectorDB
    - rule_store.py                    SQLite 规则/证据/修复策略存储
    - metadata_store.py                SQLite 元数据存储
    - pattern_index.py                 ChromaDB 向量索引
    - feature_extractor.py             崩溃特征提取
    - init_vector_db_data.py           数据库初始化（内置种子数据）

【数据存储位置】
  运行时 ChromaDB 持久化目录由调用方通过 vector_db_path 参数指定，
  与本开源库代码分离，不提交到此仓库。

  如需导入自定义知识库，可通过 init_vector_db_data.py 初始化种子数据，
  或自行准备 JSON 快照并调用 AICrashAnalyzerWithVectorDB.import_snapshot() 导入。
"""
