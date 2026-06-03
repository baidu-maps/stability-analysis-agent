# 安装与依赖排错

## 推荐安装方式

```bash
# 核心能力：崩溃解析、符号化、代码上下文、LLM 分析
pip install stability-analysis-agent

# 含向量库 / 相似案例 RAG（ChromaDB 等）
pip install "stability-analysis-agent[rag]"
```

源码开发：

```bash
pip install -e ".[rag,test]"
```

## `PyTorch >= 2.4 is required but found 2.2.x` / NumPy 2.x 与 torch 不兼容

常见组合错误：**torch 2.2** + **numpy 2.x** + 较新的 **transformers**，会导致：

- transformers 禁用 PyTorch 或报 `Failed to initialize NumPy`
- 后续 `NameError: name 'nn' is not defined`

请安装与 `[rag]` extra 一致的版本（或整体重装 `[rag]`）：

```bash
pip install --upgrade "numpy>=1.24,<2" "torch>=2.4.0" \
  "transformers>=4.36.0,<4.52.0" \
  "sentence-transformers>=2.2.2,<3.0.0" \
  "accelerate>=0.26.0"
# 或
pip install --upgrade "stability-analysis-agent[rag]"
```

## 启动分析时报 `NameError: name 'nn' is not defined`

堆栈若经过 `sentence_transformers` → `transformers`，多为 **torch / transformers / sentence-transformers 版本不兼容** 或 **torch 未正确安装**。

处理建议：

1. 使用带 `[rag]` 的依赖组合重装（已锁定兼容区间）：

   ```bash
   pip install --upgrade "stability-analysis-agent[rag]"
   ```

2. 或在当前 venv 中显式安装（与 `[rag]` extra 约束一致）：

   ```bash
   pip install "numpy>=1.24,<2" "torch>=2.4.0" \
     "transformers>=4.36.0,<4.52.0" \
     "sentence-transformers>=2.2.2,<3.0.0" \
     "accelerate>=0.26.0"
   ```

3. **不需要向量检索时**：可只装核心包；分析主流程会在 RAG 不可用时自动降级（规则+向量记忆跳过），不再因 ML 栈导入失败而整体退出。

4. 默认 **不** 在启动时加载 `SentenceTransformer`（避免拉取 HuggingFace）。仅在使用哈希嵌入 + ChromaDB 时无需 `sentence-transformers`。若需预训练嵌入，设置环境变量 `AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1` 并确保 `[rag]` 依赖完整。

## SSL：`CERTIFICATE_VERIFY_FAILED`

属于本机 Python 的 CA 环境，包无法自动修复。参见联通性检测失败时的 CLI 提示，或 macOS 运行 `Install Certificates.command` / 使用 Homebrew Python。

## 向量库子命令提示「向量数据库不可用」

```bash
pip install "stability-analysis-agent[rag]"
```

然后重试 `sa-agent vector-db ...` 等子命令。
