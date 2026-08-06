# 大模型连接测试脚本使用指南

## 📋 概述

`test/llm/test_llm_connection.py` 是一个用于测试大模型 API 连接和配置的脚本。它支持测试配置文件中定义的所有大模型，并可以将测试结果保存到本地文件，方便查看和对比。

## 🎯 主要功能

- **默认模型测试**：不指定参数时，自动测试配置中的默认模型
- **指定模型测试**：可以指定测试特定的模型
- **全面测试**：支持测试所有配置的模型
- **结果保存**：自动将测试结果（成功或失败）保存到 `test/llm/test_llm_connection_res.json`
- **错误记录**：详细记录各种错误情况，包括 HTTP 状态码、错误详情等

## 📁 文件说明

### 配置文件

- **`configs/agent_config.local.example.json`**：主配置文件，包含模型配置、默认模型等
- **`configs/agent_config.local.json`**：本地配置文件，用于存放 API 密钥等敏感信息（不入库）

### 测试结果文件

- **`test/llm/test_llm_connection_res.json`**：测试结果文件，包含：
  - `test_prompt`：测试提示词
  - `last_test_response`：最后一次测试的结果（成功或失败）

## 🚀 使用方法

### 1. 基本使用

#### 测试默认模型（推荐）

```bash
cd /path/to/stability-analysis-agent
python3 test/llm/test_llm_connection.py
```

脚本会自动读取 `agent_config.local.json` 中的 `active_provider` 和 `default_model`，测试对应的模型。

#### 测试指定模型

```bash
python3 test/llm/test_llm_connection.py --model zhipu_bigmodel:glm-4.7
python3 test/llm/test_llm_connection.py --model baidu_qianfan:ernie-4.5-turbo-128k
```

模型 ID 格式：`provider:model_name`

#### 测试所有模型

```bash
python3 test/llm/test_llm_connection.py --all
```

### 2. 配置测试提示词

编辑 `test/llm/test_llm_connection_res.json` 文件，修改 `test_prompt` 字段：

```json
{
    "test_prompt": "给我介绍什么是Agent",
    "last_test_response": { ... }
}
```

**提示词说明**：
- `test_prompt` 的内容会直接作为 `user` 消息发送给大模型
- 不添加 `system` 消息，便于与网页端对比结果
- 如果未配置 `test_prompt`，会使用默认提示词："请用一句话介绍你自己，并告诉我你现在的时间。"

### 3. 查看测试结果

测试完成后，结果会自动保存到 `test/llm/test_llm_connection_res.json` 的 `last_test_response` 字段中。

#### 成功时的结果格式

```json
{
    "last_test_response": {
        "success": true,
        "content": "大模型的响应内容...",
        "model_id": "zhipu_bigmodel:glm-4.7",
        "elapsed_time": 42.08,
        "timestamp": "2026-01-22 15:17:28"
    }
}
```

#### 失败时的结果格式

```json
{
    "last_test_response": {
        "success": false,
        "error": "HTTP错误 401",
        "status_code": 401,
        "elapsed_time": 2.5,
        "error_detail": "状态码: 401, 响应: Unauthorized",
        "model_id": "zhipu_bigmodel:glm-4.7",
        "timestamp": "2026-01-22 15:30:00"
    }
}
```

## ⚙️ 配置说明

### 协议与 request_format

脚本已支持多种协议，按 `llm_config.providers.<provider>.request_format` 选择：

- `openai_chat_completions_compatible`（默认）
  - 典型 endpoint：`.../v1/chat/completions`
  - 常用鉴权：`Authorization: Bearer <API_KEY>`
- `anthropic_messages_compatible`
  - 典型 endpoint：`.../v1/messages`
  - 常用鉴权：`x-api-key: <API_KEY>`（配置 `auth_header: x-api-key`、`auth_prefix: ""`）
- `openai_responses_compatible`
  - 典型 endpoint：`.../v1/responses`
  - 请求体为 `model + input`
- `minimax_text_chatcompletion_v2_compatible`
  - 以 Chat Completions 兼容结构发送，适用于部分 MiniMax 兼容路由

注意：`base_url` 会按配置原值使用（仅去除末尾 `/`），请填写完整 endpoint，脚本不会自动补后缀路径。

### 1. API 密钥配置

#### 智谱 BigModel

在 `configs/agent_config.local.json` 中配置：

```json
{
    "llm_config": {
        "providers": {
            "zhipu_bigmodel": {
                "api_key": "your-api-key-here"
            }
        }
    }
}
```

或者通过环境变量设置：

```bash
export ZHIPU_API_KEY="your-api-key-here"
# 或
export BIGMODEL_API_KEY="your-api-key-here"
```

#### 百度千帆

在 `configs/agent_config.local.json` 中配置：

```json
{
    "llm_config": {
        "providers": {
            "baidu_qianfan": {
                "authorization": "Bearer your-token-here"
            }
        }
    }
}
```

或者通过环境变量设置：

```bash
export BAIDU_QIANFAN_AUTHORIZATION="Bearer your-token-here"
```

### 2. 默认模型配置

在 `configs/agent_config.local.json` 中配置：

```json
{
    "llm_config": {
        "active_provider": "zhipu_bigmodel",
        "default_model": "glm-4.7"
    }
}
```

## 🔍 错误处理

脚本会记录所有错误情况，包括：

1. **缺少授权信息**：API 密钥未配置
2. **HTTP 错误**：各种 HTTP 状态码（401、403、429、500 等）
3. **响应格式异常**：返回 200 但响应格式不正确
4. **网络异常**：连接超时、网络错误等
5. **限流处理**：429 限流错误（默认视为成功，可通过环境变量改为严格模式）

### 限流处理

对于 429 限流错误，脚本默认将其视为"可达但被限流"，测试判定为通过。如需严格模式，设置环境变量：

```bash
QIANFAN_TEST_STRICT=1 python3 test/llm/test_llm_connection.py
```

## 📊 测试结果对比

### 与网页端对比

1. 在 `test_llm_connection_res.json` 中设置 `test_prompt`
2. 运行测试脚本
3. 在网页端发送相同的提示词
4. 对比 `last_test_response.content` 与网页端的返回结果

这样可以验证工具请求和网页端请求是否一致。

## 🛠️ 高级用法

### 多轮对话测试

在 `test_llm_connection_res.json` 中配置 `test_messages`：

```json
{
    "test_messages": [
        {
            "role": "user",
            "content": "第一轮问题"
        },
        {
            "role": "assistant",
            "content": "第一轮回答"
        },
        {
            "role": "user",
            "content": "第二轮问题"
        }
    ]
}
```

`test_messages` 的优先级高于 `test_prompt`。

### 环境变量控制

- `LLM_TEST_STRICT_429=1`：严格模式，将 429 限流视为失败
- `LLM_TEST_MAX_RETRIES=3`：429 限流时的最大重试次数（默认 3）
- `LLM_TEST_RETRY_SLEEP_SECONDS=2`：重试等待时间（秒，默认 2）

## 📝 示例

### 示例 1：测试默认模型

```bash
# 1. 确保已配置 API 密钥
# 2. 运行测试
python3 test/llm/test_llm_connection.py

# 输出示例：
# ✅ 配置文件加载成功: .../agent_config.json
# 未指定参数，默认测试模型: zhipu_bigmodel:glm-4.7
# === 测试指定模型: zhipu_bigmodel:glm-4.7 ===
# 
# 🔍 测试模型: GLM-4.7 (zhipu_bigmodel:glm-4.7)
#    提供商: zhipu_bigmodel
#    模型: glm-4.7
#    📤 发送测试提示词: 给我介绍什么是Agent
#    📥 收到响应 (耗时: 42.08秒): ...
#    💾 测试结果已保存到: .../test_llm_connection_res.json
# ✅ 模型 zhipu_bigmodel:glm-4.7 测试成功！
```

### 示例 2：测试指定模型

```bash
python3 test/llm/test_llm_connection.py --model baidu_qianfan:ernie-4.5-turbo-128k
```

### 示例 3：测试所有模型

```bash
python3 test/llm/test_llm_connection.py --all
```

## 🔧 故障排除

### 问题 1：缺少授权信息

**错误信息**：
```
❌ 缺少智谱鉴权信息，请设置ZHIPU_API_KEY/BIGMODEL_API_KEY或在配置中填写...
```

**解决方法**：
1. 检查 `agent_config.local.json` 中是否配置了 API 密钥
2. 或设置环境变量 `ZHIPU_API_KEY` 或 `BIGMODEL_API_KEY`

### 问题 2：HTTP 401/403 错误

**错误信息**：
```
❌ 请求失败，状态码: 401
```

**解决方法**：
1. 检查 API 密钥是否正确
2. 检查 API 密钥是否过期
3. 检查是否有权限访问该模型

### 问题 3：429 限流错误

**错误信息**：
```
⚠️  请求被限流（状态码: 429）
```

**解决方法**：
1. 等待一段时间后重试
2. 检查 API 配额是否用完
3. 如需严格模式，设置 `QIANFAN_TEST_STRICT=1`

### 问题 4：响应格式异常

**错误信息**：
```
❌ 响应格式异常: {...}
```

**解决方法**：
1. 检查模型 API 是否正常
2. 检查响应格式是否符合预期
3. 查看 `error_detail` 字段了解详情

## 📚 相关文档

- [测试目录说明](../../TEST_README.md)
- [CLI 指南](../../cli/CLI_GUIDE.md)
- [配置文件说明](../../../configs/agent_config.local.example.json)

## 🔄 更新日志

- **2026-01-22**：将测试结果从 `agent_config.local.json` 迁移到 `test_llm_connection_res.json`
- **2026-01-22**：支持保存错误信息到结果文件
- **2026-01-22**：支持从结果文件读取测试提示词

---

*最后更新: 2026年1月*  
*维护者: Stability Analysis Agent 团队*
