# 安装与依赖排错

## Python 版本

| 场景 | 建议版本 |
|------|----------|
| 最低支持 | **Python 3.9**（`pyproject.toml` → `requires-python = ">=3.9"`） |
| 推荐（完整 CLI + RAG） | **3.10 – 3.12** |
| 默认完整安装（含 RAG） | **3.10 – 3.12**（torch / transformers 组合在 3.9 上更容易踩坑） |
| 未在 CI 中验证 | 3.13+：可尝试安装，但不保证完整 ML 依赖均有 wheel |

查看当前环境：

```bash
python3 --version
python3 -c "import sys; print(sys.executable)"
sa-agent config doctor
```

**macOS 提示**：优先使用 Homebrew（`brew install python@3.12`）或 pyenv 安装的 Python；python.org 官方包若未运行 `Install Certificates.command`，易出现 SSL 证书错误（见下文）。

## 推荐安装方式

### pip（venv，通用）

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 完整能力：崩溃解析、符号化、代码上下文、LLM 分析和 RAG
pip install stability-analysis-agent
```

源码开发：

```bash
pip install -e ".[rag,test]"
```

### pipx（隔离 CLI，推荐终端用户）

[pipx](https://pipx.pypa.io/) 为每个 CLI 工具创建独立虚拟环境，避免与系统/项目 Python 包冲突。

```bash
# 安装 pipx（macOS 示例）
brew install pipx
pipx ensurepath

# 核心 CLI
pipx install stability-analysis-agent

# 默认包含 RAG（下载体积较大，首次安装可能较慢）
pipx install stability-analysis-agent

# 验证
sa-agent --help
sa-agent config doctor
```

**pipx 说明**：

- 升级：`pipx upgrade stability-analysis-agent`
- 卸载：`pipx uninstall stability-analysis-agent`
- SSL / CA 问题仍取决于 pipx 使用的 **底层 Python 解释器**（与 pip 相同）
- 开发调试（可编辑安装）请用 `pip install -e .`，不要用 pipx

### 预编译二进制（无需 Python）

见 [README.md](../../README.md) 中「使用预编译 CLI 二进制」。

## `PyTorch >= 2.4 is required but found 2.2.x` / NumPy 2.x 与 torch 不兼容

常见组合错误：**torch 2.2** + **numpy 2.x** + 较新的 **transformers**，会导致：

- transformers 禁用 PyTorch 或报 `Failed to initialize NumPy`
- 后续 `NameError: name 'nn' is not defined`

请按项目默认版本范围重装 ML 依赖：

```bash
pip install --upgrade "numpy>=1.24,<2" "torch>=2.4.0" \
  "transformers>=4.36.0,<4.52.0" \
  "sentence-transformers>=2.2.2,<3.0.0" \
  "accelerate>=0.26.0"
# 或
pip install --upgrade stability-analysis-agent
```

## 启动分析时报 `NameError: name 'nn' is not defined`

堆栈若经过 `sentence_transformers` → `transformers`，多为 **torch / transformers / sentence-transformers 版本不兼容** 或 **torch 未正确安装**。

处理建议：

1. 重装项目默认依赖组合（已锁定兼容区间）：

   ```bash
   pip install --upgrade stability-analysis-agent
   ```

2. 或在当前 venv 中显式安装（与项目默认约束一致）：

   ```bash
   pip install "numpy>=1.24,<2" "torch>=2.4.0" \
     "transformers>=4.36.0,<4.52.0" \
     "sentence-transformers>=2.2.2,<3.0.0" \
     "accelerate>=0.26.0"
   ```

3. 即使 RAG 运行时不可用，分析主流程也会自动降级（跳过相似案例检索），不会因此阻断基础解析和符号化。

4. 默认 **不** 在启动时加载 `SentenceTransformer`（避免拉取 HuggingFace 模型）。仅在使用哈希嵌入 + ChromaDB 时无需额外配置；若需预训练嵌入，设置环境变量 `AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1`。

## SSL：`CERTIFICATE_VERIFY_FAILED`

属于本机 Python 的 CA 环境，包无法自动修复。处理建议：

1. macOS 官方 Python：运行安装目录下 **Install Certificates.command**
2. 或改用 Homebrew / pyenv 安装的 Python
3. 交互菜单「检测联通性」会先检查本机 SSL 环境并给出分层提示
4. 企业内网：向 IT 导入公司根证书

## 向量库子命令提示「向量数据库不可用」

```bash
pip install --upgrade stability-analysis-agent
```

然后重试 `sa-agent vector-db ...` 等子命令。
