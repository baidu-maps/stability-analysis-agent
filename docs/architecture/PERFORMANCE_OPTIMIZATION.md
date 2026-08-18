# 性能优化指南

本文档记录 Stability Analysis Agent 各阶段的性能瓶颈分析与优化措施。

## 优化总览

以 `demo_basic/NullPtr_SIGSEGV` 案例为基准（单函数空指针崩溃）：

| 阶段 | 优化前 | 优化后 | 优化手段 |
|------|--------|--------|----------|
| Phase 2 堆栈符号化 | 65s | 0.5s | 消除 workspace 全量扫描 |
| Phase 4 AI 分析根因 | 23s+ | 8-18s | 流式调用 + max_tokens 降档 |
| Phase 5 应用代码修复 | 21s | 0.01s | fast-path 直接提取 |
| **端到端** | **122s** | **10-20s** | — |

---

## Phase 2：堆栈符号化（65s → 0.5s）

### 瓶颈定位

`add2line_resolver_tool.py` 中 `_find_function_definition_in_workspace` 使用 `os.walk` 遍历所有 C/C++ 文件查找函数定义。在实际环境中（`.venv_pypi_110_test` 包含 PyTorch 头文件等），会扫描 29,388 个文件。

### 优化措施

1. **移除 strategy 2 的 workspace scan**：resolver 的 strategy 2 不再调用 `_infer_file_name_from_symbol` 和 `_calculate_precise_line_number`，因为这些函数内部触发了全量文件遍历。

2. **添加 skip_prefixes**：`_find_function_definition_in_workspace` 新增对 `.venv*`、`node_modules`、`__pycache__` 等目录的跳过逻辑。

3. **quick_mode 旁路**：当调用方已通过其他方式（如 ctags 索引）定位到函数时，跳过 workspace scan。

### 关键代码

```python
# tools/add2line_resolver_tool.py
SKIP_PREFIXES = (".venv", "node_modules", "__pycache__", ".git", "build", "cmake-build")

def _find_function_definition_in_workspace(self, ...):
    if quick_mode:
        return None  # 旁路
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if not any(d.startswith(p) for p in SKIP_PREFIXES)]
        ...
```

---

## Phase 4：AI 分析根因（23s+ → 8-18s）

### 瓶颈定位

原实现使用非流式 API 调用（`stream=False`），等待模型完整生成后才返回。相同 prompt 在厂商 Web UI 中明显更快，因为 Web UI 使用流式。

### 优化措施

#### 1. 非流式 → 流式调用

```python
# tool_system/llm/llm_adapter.py - DirectLLMAdapter.chat()
request_params = {
    "model": self.model,
    "messages": messages,
    "max_tokens": kwargs.get("max_tokens", self.max_tokens),
    "stream": True,  # 流式调用
}
response = self.client.chat.completions.create(**request_params)
chunks = []
for chunk in response:
    if chunk.choices and chunk.choices[0].delta.content:
        chunks.append(chunk.choices[0].delta.content)
content = "".join(chunks)
```

**原理**：流式调用的首 token 延迟（TTFT）通常低于非流式调用。非流式调用需要等待 provider 内部完成全部 token 生成、序列化后才返回；流式调用在第一个 token 生成后即开始传输。

#### 2. stream_options 兼容性缓存

不同 provider 对 `stream_options: {"include_usage": True}`（获取 token 用量统计）的支持不一。通过首次探测 + 实例级缓存避免不支持时的双重请求：

```python
self._stream_options_supported: Optional[bool] = None

# 首次调用时探测
if self._stream_options_supported is not False:
    try:
        params_with_usage = {**request_params, "stream_options": {"include_usage": True}}
        response = self.client.chat.completions.create(**params_with_usage)
        self._stream_options_supported = True
    except Exception:
        self._stream_options_supported = False
        response = self.client.chat.completions.create(**request_params)
else:
    # 后续调用直接跳过
    response = self.client.chat.completions.create(**request_params)
```

#### 3. max_tokens 降档

将 `first_try_tokens` 从 12000 降至 2048：
- 大部分修复分析输出 400-1000 tokens，2048 足够
- 某些 provider 根据 max_tokens 预分配 GPU 资源，较低值可降低调度延迟
- 失败后自动降档重试（1200 → 800 → adapter 默认值）

```python
# workflows/crash_analysis_workflow.py
first_try_tokens = 2048
token_attempts = [first_try_tokens]
for candidate in [1200, 800]:
    if candidate < first_try_tokens:
        token_attempts.append(candidate)
token_attempts.append(None)  # 兜底走适配器默认值
```

#### 4. 可配置的流式开关

在 `agent_config.local.json` 中支持按 provider 关闭流式：

```json
{
  "llm_config": {
    "provider_defaults": {
      "stream": true
    },
    "providers": {
      "some_provider": {
        "stream": false
      }
    }
  }
}
```

#### 5. OpenAI Client 超时

传入配置的 timeout 值（而非依赖 SDK 默认的 600s）：

```python
self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=float(self.timeout or 120))
```

### 厂商兼容性

| 厂商 | request_format | 流式支持 | stream_options |
|------|---------------|---------|---------------|
| OpenAI | openai_chat_completions_compatible | ✓ | ✓ |
| DeepSeek | openai_chat_completions_compatible | ✓ | ✓ |
| GLM (智谱) | openai_chat_completions_compatible | ✓ | ✓ |
| 百度千帆 | openai_chat_completions_compatible | ✓ | 需验证 |
| 通义千问 | openai_chat_completions_compatible | ✓ | ✓ |
| Kimi | openai_chat_completions_compatible | ✓ | 需验证 |
| MiniMax | openai_chat_completions_compatible | ✓ | 需验证 |
| Claude | anthropic_messages_compatible | 独立路径 | N/A |

所有 `openai_chat_completions_compatible` 厂商都支持标准 SSE 流式协议。`anthropic_messages_compatible` 和 `openai_responses_compatible` 走各自的 HTTP 调用路径，不受 `stream` 参数影响。

---

## Phase 5：应用代码修复（21s → 0.01s）

### Fast-Path 提取（仅依赖 Phase 4 输出）

为了减少额外的 LLM 调用，当前自动改码阶段**只依赖 Phase 4 的分析输出文本**来提取修复代码，不再在 Phase 5 再次向大模型请求 JSON 形式的修复计划。

在 `CodeFixer.generate_and_apply` 中，始终通过 `_try_extract_fix_plan_from_analysis()` 和 `FixCodeExtractorTool` 从 `analysis_text` 中扫描代码块、匹配函数签名并构造 `fix_plan`。如果提取失败，则直接跳过自动改码，让人工根据 `06_ai_res` 与源码判断是否需要手动修改。

### 提取鲁棒性增强

基于 Phase 4 文本提取依赖 LLM 输出的格式与内容质量。针对 3 类提取失败场景做了增强：

#### 场景 1：注释中的 `...` 被误判为占位符

```python
def _contains_placeholder_code(text: str) -> bool:
    for m in re.finditer(r"\.{3}", s):
        prefix = ...  # 同行左侧文本
        # 跳过字符串中的 ...
        if prefix.count('"') % 2 == 1:
            continue
        # 跳过 // 注释中的 ...
        dslash = prefix.rfind("//")
        if dslash >= 0 and prefix[:dslash].count('"') % 2 == 0:
            continue
        # 跳过 /* */ 块注释中的 ...
        if prefix.rfind("/*") > prefix.rfind("*/"):
            continue
        return True
    return False
```

#### 场景 2：无围栏代码块

当 LLM 输出修复代码不带 ``` 围栏时，后备提取按函数签名模式扫描：

```python
def _extract_unfenced_code_blocks(analysis_text: str) -> List[str]:
    """后备：按 C++ 函数定义模式（返回值+函数名+参数+大括号配对）提取。"""
    func_pattern = re.compile(r"^[ \t]*(?:...修饰符...)?(?:返回值\s+)?函数名\s*\(参数\)\s*\{?", re.MULTILINE)
    # 过滤 if/for/while 等控制语句
    # 大括号配对提取完整函数体
```

#### 场景 3：required_targets 匹配失败

当 analysis 中提到的函数名在 candidate_nodes 中找不到匹配时（例如代码定位偏差），直接从 LLM 输出的代码块中扫描函数定义作为 target：

```python
if not required_targets:
    # 扫描代码块中的函数签名作为 target
    for code in blocks:
        for m in func_pattern.finditer(code):
            block = _extract_function_block_from_code(code, m.start())
            if block and _is_valid_replacement_code(block):
                targets.append({"file": default_file, "function_signature": sig})
```

### 提示词侧约束

在 `_build_prompt_final_tip` 的关键约束中添加格式硬性要求，从源头减少提取失败：

```
- **修复代码格式**：每个修复函数必须使用独立的 ```cpp 围栏包裹；
  函数体必须完整，禁止用 `...` 或省略号代替任何代码行。
```

---

## 性能诊断方法

### 快速定位瓶颈

运行带阶段计时的分析：

```bash
python3 cli/main.py \
  --crash-log <path> --library-dir <path> --code-root <path> \
  --scope full 2>&1 | grep -E "^\[阶段"
```

输出示例：
```
[阶段 1/5] 解析崩溃日志 ✓ 0.01s
[阶段 2/5] 堆栈符号化 ✓ 0.50s
[阶段 3/5] 定位崩溃源码 ✓ 0.42s
[阶段 4/5] AI 分析根因 ✓ 15.7s (输入 2,830 / 输出 429 tokens)
[阶段 5/5] 应用代码修复 ✓ 0.01s
```

### Phase 4 延迟诊断

Phase 4 耗时波动大（8-38s）时，通常是 LLM API 服务端问题：

1. **TTFT（首 token 延迟）不稳定**：provider 排队/负载高
2. **输出 tokens 过多**：检查 max_tokens 配置
3. **双重请求**：检查 stream_options 兼容性缓存是否生效

诊断命令（多次运行对比）：

```bash
for i in 1 2 3; do
  python3 cli/main.py --crash-log <path> --library-dir <path> --code-root <path> \
    --scope full 2>&1 | grep "阶段 4"
done
```

### Phase 5 未命中 fast-path

如果 Phase 5 耗时 > 1s，说明 fast-path 未命中，fallback 到了 LLM 调用。可能原因：

1. Phase 4 输出被 max_tokens 截断（检查输出 tokens 是否接近 max_tokens）
2. 修复代码未使用 ```cpp 围栏（检查 `06_ai_gen_res.md`）
3. 代码中包含 `...` 占位符（检查 `_contains_placeholder_code` 判定）
4. candidate_nodes 中无目标函数匹配（检查 `03_code_content_provider.json`）

调试命令：

```bash
python3 -c "
from services.code_fixer import _extract_code_blocks, _extract_replacement_from_analysis
text = open('reports/<latest>/round_0/06_ai_gen_res.md').read()
blocks = _extract_code_blocks(text)
print(f'代码块数: {len(blocks)}')
result = _extract_replacement_from_analysis(text, '<target_function>(')
print(f'提取结果: {result is not None}')
"
```

---

## 不可优化的因素

以下耗时由外部系统决定，代码层面无法进一步优化：

| 因素 | 典型影响 | 建议 |
|------|---------|------|
| LLM API TTFT | 2-30s | 换用延迟更低的 provider 或模型 |
| LLM 输出速率 | 30-80 tokens/s | 降低 max_tokens 限制输出长度 |
| provider 排队 | 0-20s 随机 | 避免高峰时段；使用专享实例 |
| 网络延迟 | 100-500ms | 选择地理位置近的 endpoint |

### 推荐模型配置（按延迟排序）

| 模型 | 典型 TTFT | 输出速率 | 适合场景 |
|------|----------|---------|---------|
| deepseek-chat | 1-3s | 60+ tokens/s | 日常使用，性价比最高 |
| qwen-plus | 2-5s | 50+ tokens/s | 中文场景优化 |
| gpt-4o | 2-5s | 80+ tokens/s | 复杂多线程分析 |
| glm-4 | 2-30s | 30-50 tokens/s | 波动大，TTFT 不稳定 |

---

## 配置参考

### 最优性能配置

```json
{
  "llm_config": {
    "provider_defaults": {
      "stream": true,
      "request_timeout": 60
    }
  }
}
```

### 兼容性优先配置（流式有问题时）

```json
{
  "llm_config": {
    "providers": {
      "problematic_provider": {
        "stream": false
      }
    }
  }
}
```
