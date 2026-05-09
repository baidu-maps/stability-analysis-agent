#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大模型连接测试脚本
测试tools/configs/agent_config.local.json中配置的大模型是否能正常连接和使用
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

# 添加tools目录到Python路径
tools_dir = Path(__file__).parent.parent.parent / "tools"
sys.path.insert(0, str(tools_dir))

# 测试结果文件路径
test_result_file = Path(__file__).parent / "test_llm_connection_res.json"
DEFAULT_TEST_RESULT_TEMPLATE: Dict[str, Any] = {
    "test_prompt": "请用一句话介绍你自己，并告诉我你现在的时间。",
    "test_messages": [
        {
            "role": "user",
            "content": "请用一句话介绍你自己，并告诉我你现在的时间。",
        }
    ],
    "last_test_response": {},
}

def load_agent_config() -> Dict[str, Any]:
    """加载AI Agent配置文件"""
    local_path = tools_dir / "configs" / "agent_config.local.json"
    config_path = local_path
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        print(f"✅ 配置文件加载成功: {config_path}")
        return config
    except Exception as e:
        print(f"❌ 配置文件加载失败: {e}")
        sys.exit(1)

def _get_nested(config: Dict[str, Any], keys: list, default=None):
    """安全获取嵌套字典字段"""
    cur = config
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _normalize_test_result_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """确保测试结果数据结构完整，避免强依赖本地结果文件。"""
    merged = json.loads(json.dumps(DEFAULT_TEST_RESULT_TEMPLATE, ensure_ascii=False))
    if isinstance(data, dict):
        merged.update(data)
    if not isinstance(merged.get("test_messages"), list):
        merged["test_messages"] = DEFAULT_TEST_RESULT_TEMPLATE["test_messages"]
    if not isinstance(merged.get("test_prompt"), str):
        merged["test_prompt"] = DEFAULT_TEST_RESULT_TEMPLATE["test_prompt"]
    if not isinstance(merged.get("last_test_response"), dict):
        merged["last_test_response"] = {}
    return merged

def load_test_result_file() -> Dict[str, Any]:
    """从测试结果文件读取数据"""
    try:
        if test_result_file.exists():
            with open(test_result_file, 'r', encoding='utf-8') as f:
                return _normalize_test_result_data(json.load(f))
        return _normalize_test_result_data({})
    except Exception as e:
        print(f"   ⚠️  读取测试结果文件失败: {e}")
        return _normalize_test_result_data({})

def save_test_result_file(data: Dict[str, Any]) -> bool:
    """保存数据到测试结果文件"""
    try:
        with open(test_result_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print(f"   💾 测试结果已保存到: {test_result_file}")
        return True
    except Exception as e:
        print(f"   ⚠️  保存测试结果失败: {e}")
        return False

def save_test_response_to_config(content: str = None, model_id: str = None, elapsed_time: float = None, 
                                  error: str = None, status_code: int = None, error_detail: str = None):
    """
    将测试响应结果或错误信息保存到 test_llm_connection_res.json
    
    Args:
        content: 成功时的响应内容
        model_id: 模型ID
        elapsed_time: 响应耗时（秒）
        error: 错误类型/消息
        status_code: HTTP状态码（失败时）
        error_detail: 错误详情
    """
    try:
        # 读取现有数据
        result_data = load_test_result_file()
        
        # 构建保存的数据
        response_data = {
            "model_id": model_id or "unknown",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "success": error is None
        }
        
        if error is None:
            # 成功情况
            response_data["content"] = content or ""
            response_data["elapsed_time"] = elapsed_time or 0.0
        else:
            # 失败情况
            response_data["error"] = error
            if status_code is not None:
                response_data["status_code"] = status_code
            if elapsed_time is not None:
                response_data["elapsed_time"] = elapsed_time
            if error_detail:
                response_data["error_detail"] = error_detail
        
        # 保存测试响应结果
        result_data["last_test_response"] = response_data
        
        # 写回文件
        return save_test_result_file(result_data)
    except Exception as e:
        print(f"   ⚠️  保存测试结果失败: {e}")
        return False

def get_qianfan_authorization(config: Dict[str, Any]) -> Optional[str]:
    """
    获取百度千帆 authorization。
    优先级：
    1) 环境变量 BAIDU_QIANFAN_AUTHORIZATION
    2) 配置文件 tools/configs/agent_config.local.json 的 llm_config.providers.baidu_qianfan.authorization
    """
    env_auth = os.getenv("BAIDU_QIANFAN_AUTHORIZATION")
    if env_auth:
        return env_auth
    cfg_auth = _get_nested(config, ["llm_config", "providers", "baidu_qianfan", "authorization"])
    return cfg_auth

def get_zhipu_authorization(config: Dict[str, Any]) -> Optional[str]:
    """
    获取智谱 BigModel authorization。
    优先级：
    1) 环境变量 ZHIPU_API_KEY / BIGMODEL_API_KEY
    2) 配置文件 tools/configs/agent_config.local.json 的 llm_config.providers.zhipu_bigmodel.api_key
    """
    env_key = os.getenv("ZHIPU_API_KEY") or os.getenv("BIGMODEL_API_KEY")
    if env_key:
        return env_key
    cfg_key = _get_nested(config, ["llm_config", "providers", "zhipu_bigmodel", "api_key"])
    return cfg_key


def _normalize_base_url(base_url: str) -> str:
    """直接使用配置中的 base_url（仅去除首尾空白和末尾斜杠）。"""
    normalized = (base_url or "").strip()
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _provider_env_candidates(provider_key: str, provider_cfg: Dict[str, Any], auth_type: str) -> List[str]:
    env_candidates: List[str] = []
    cfg_env = provider_cfg.get("api_key_env")
    if isinstance(cfg_env, list):
        env_candidates.extend([str(x).strip() for x in cfg_env if str(x).strip()])
    elif isinstance(cfg_env, str):
        env_candidates.extend([x.strip() for x in cfg_env.split(",") if x.strip()])

    # 兼容历史 provider 约定
    if provider_key == "zhipu_bigmodel":
        env_candidates.extend(["ZHIPU_API_KEY", "BIGMODEL_API_KEY"])
    elif provider_key == "baidu_qianfan":
        env_candidates.append("BAIDU_QIANFAN_AUTHORIZATION")
    elif provider_key == "openai":
        env_candidates.append("OPENAI_API_KEY")
    elif auth_type == "api_key":
        env_candidates.append(f"{provider_key.upper()}_API_KEY")
    elif auth_type == "authorization":
        env_candidates.append(f"{provider_key.upper()}_AUTHORIZATION")

    # 去重并保持顺序
    dedup: List[str] = []
    for item in env_candidates:
        if item and item not in dedup:
            dedup.append(item)
    return dedup


def _build_auth_headers(provider_key: str, provider_cfg: Dict[str, Any]) -> Dict[str, str]:
    """根据 provider 配置动态构建鉴权请求头。"""
    auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
    if not auth_type:
        auth_type = "authorization" if (provider_key == "baidu_qianfan" or provider_cfg.get("authorization")) else "api_key"

    if auth_type == "none":
        return {}

    auth_header = str(provider_cfg.get("auth_header") or "Authorization").strip() or "Authorization"
    auth_prefix = provider_cfg.get("auth_prefix")
    if auth_prefix is None:
        auth_prefix = "Bearer "
    auth_prefix = str(auth_prefix)

    env_candidates = _provider_env_candidates(provider_key, provider_cfg, auth_type)
    env_secret = next((os.getenv(key) for key in env_candidates if os.getenv(key)), None)
    cfg_secret = provider_cfg.get("authorization") if auth_type == "authorization" else provider_cfg.get("api_key")
    secret = env_secret or cfg_secret
    if not secret:
        raise RuntimeError(
            f"缺少鉴权信息，请设置环境变量（建议: {', '.join(env_candidates[:3])}）"
            f"或在 llm_config.providers.{provider_key} 中配置"
        )

    secret_str = str(secret)
    if auth_prefix and not secret_str.startswith(auth_prefix):
        secret_str = f"{auth_prefix}{secret_str}"
    return {auth_header: secret_str}

def get_test_messages(config: Dict[str, Any]) -> list:
    """
    从测试结果文件获取测试提示词。
    优先级：
    1) test_llm_connection_res.json 中的 test_messages（完整 messages 列表，用于多轮对话场景）
    2) test_llm_connection_res.json 中的 test_prompt（单个提示词，直接作为 user 消息发送，便于与网页端对比）
    3) 默认提示词
    """
    # 从测试结果文件读取
    result_data = load_test_result_file()
    
    # 检查是否有 test_messages
    messages = result_data.get("test_messages")
    if isinstance(messages, list) and messages:
        return messages

    # 如果配置了 test_prompt，直接作为 user 消息发送（不加 system，便于与网页端完全一致）
    test_prompt = result_data.get("test_prompt")
    if isinstance(test_prompt, str) and test_prompt.strip():
        return [
            {
                "role": "user",
                "content": test_prompt.strip()
            }
        ]

    # 默认提示词（用于连通性测试）
    return [
        {
            "role": "user",
            "content": "请用一句话介绍你自己，并告诉我你现在的时间。"
        }
    ]


def _get_provider_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    llm_config = config.get("llm_config", {})
    if not isinstance(llm_config, dict):
        return {}
    provider_defaults = llm_config.get("provider_defaults", {})
    return provider_defaults if isinstance(provider_defaults, dict) else {}

def get_available_models(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """获取可用的模型列表，兼容单模型和多模型两种配置格式"""
    llm_config = config.get("llm_config", {})
    providers = llm_config.get("providers", {})
    if not isinstance(providers, dict):
        return {}
    provider_defaults = _get_provider_defaults(config)

    models = {}
    for provider_key, provider_info in providers.items():
        if not isinstance(provider_info, dict):
            continue
        merged_provider_info = {**provider_defaults, **provider_info}
        provider_name = merged_provider_info.get("name", provider_key)
        provider_models = merged_provider_info.get("models", {})

        if isinstance(provider_models, dict) and provider_models:
            for model_key, model_info in provider_models.items():
                if not isinstance(model_info, dict):
                    model_info = {}
                model_id = f"{provider_key}:{model_key}"
                merged_model_info = {**merged_provider_info, **model_info}
                models[model_id] = {
                    "provider": provider_key,
                    "model": model_key,
                    "display_name": merged_model_info.get("display_name", model_key),
                    "config": merged_model_info
                }
            continue

        model_name = merged_provider_info.get("model")
        if not model_name:
            continue
        model_id = f"{provider_key}:{model_name}"
        single_model_config = dict(merged_provider_info)
        single_model_config.setdefault("model", model_name)
        models[model_id] = {
            "provider": provider_key,
            "model": model_name,
            "display_name": provider_name,
            "config": single_model_config,
        }

    return models

def get_default_model_id(config: Dict[str, Any], models: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """从配置获取默认模型ID，使用 active_provider。"""
    active_provider = _get_nested(config, ["llm_config", "active_provider"])
    default_model = _get_nested(config, ["llm_config", "default_model"])

    if active_provider and default_model:
        candidate = f"{active_provider}:{default_model}"
        if candidate in models:
            return candidate

    if active_provider:
        provider_cfg = _get_nested(config, ["llm_config", "providers", active_provider], {})
        if isinstance(provider_cfg, dict):
            provider_model = provider_cfg.get("model")
            if provider_model:
                candidate = f"{active_provider}:{provider_model}"
                if candidate in models:
                    return candidate
        for model_id, model_info in models.items():
            if model_info.get("provider") == active_provider:
                return model_id

    # 兜底：取第一个可用模型
    return next(iter(models.keys()), None)



def test_openai_compatible_model(config: Dict[str, Any], model_config: Dict[str, Any], model_name: str, model_id: str = None) -> bool:
    """测试 OpenAI Chat Completions 兼容协议模型。"""
    try:
        import requests
        provider_key = str(model_config.get("_provider_key") or "unknown_provider")
        base_url_raw = model_config.get("base_url")
        if not base_url_raw:
            raise RuntimeError(f"{provider_key} 未配置 base_url")
        base_url = _normalize_base_url(str(base_url_raw))
        model = model_config.get("model", model_name)

        # 准备请求数据和请求头
        messages = get_test_messages(config)
        headers = {
            "Content-Type": "application/json",
            **_build_auth_headers(provider_key, model_config),
        }
        timeout_s = int(model_config.get("request_timeout", 120) or 120)
        headers = {
            **headers
        }

        data = {
            "model": model,
            "messages": messages,
            "temperature": model_config.get("temperature", 0.1),
            "max_tokens": model_config.get("max_tokens", 1000),
            "stream": False,
        }

        if model_config.get("top_p") is not None:
            data["top_p"] = model_config.get("top_p")
        if model_config.get("do_sample") is not None:
            data["do_sample"] = bool(model_config.get("do_sample"))
        if model_config.get("thinking") is not None:
            data["thinking"] = model_config.get("thinking")
        if model_config.get("enable_thinking") is not None:
            data["enable_thinking"] = bool(model_config.get("enable_thinking"))

        # 发送请求（429 退避重试）
        max_retries = int(os.getenv("LLM_TEST_MAX_RETRIES", "3") or "3")
        retry_sleep_s = int(os.getenv("LLM_TEST_RETRY_SLEEP_SECONDS", "2") or "2")
        test_prompt = next((m.get("content") for m in messages if m.get("role") == "user"), "")
        print(f"   📤 发送测试提示词: {test_prompt}")
        print(f"   🌐 请求URL: {base_url}")
        print(f"   🔑 模型: {model}")

        start_time = time.time()
        response = None
        for attempt in range(1, max_retries + 1):
            response = requests.post(base_url, headers=headers, json=data, timeout=timeout_s)
            if response.status_code != 429:
                break
            print(f"   ⚠️  命中 429 限流，{retry_sleep_s}s 后重试（{attempt}/{max_retries}）")
            time.sleep(retry_sleep_s)
            retry_sleep_s = min(retry_sleep_s * 2, 30)
        if response is None:
            raise RuntimeError("请求失败：未获得响应")
        end_time = time.time()

        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                msg = result["choices"][0].get("message", {}) if isinstance(result["choices"][0], dict) else {}
                content = ""
                if isinstance(msg, dict):
                    content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                elapsed_time = end_time - start_time
                print(f"   📥 收到响应 (耗时: {elapsed_time:.2f}秒):")
                print(f"      {content}")
                save_test_response_to_config(content, model_id or f"{provider_key}:{model_name}", elapsed_time)
                return True
            else:
                error_msg = f"响应格式异常: {result}"
                print(f"   ❌ {error_msg}")
                elapsed_time = end_time - start_time
                save_test_response_to_config(
                    model_id=model_id or f"{provider_key}:{model_name}",
                    elapsed_time=elapsed_time,
                    error="响应格式异常",
                    status_code=200,
                    error_detail=str(result)
                )
                return False

        elapsed_time = end_time - start_time
        if response.status_code == 429:
            strict = os.getenv("LLM_TEST_STRICT_429", "").lower() in ("1", "true", "yes")
            error_detail = f"请求被限流（429）：服务可达但当前触发限流窗口。响应: {response.text}"
            print("   ⚠️  请求被限流（429）：服务可达但当前触发限流窗口。")
            print(f"      {response.text}")
            if not strict:
                print("   ✅ 默认模式：429 视为可达通过（可稍后重试验证业务返回）。")
                save_test_response_to_config(
                    model_id=model_id or f"{provider_key}:{model_name}",
                    elapsed_time=elapsed_time,
                    error="请求被限流（非严格模式，视为成功）",
                    status_code=429,
                    error_detail=error_detail
                )
                return True

        error_msg = f"请求失败，状态码: {response.status_code}"
        error_detail = f"状态码: {response.status_code}, 响应: {response.text}"
        print(f"   ❌ {error_msg}")
        print(f"      {response.text}")
        save_test_response_to_config(
            model_id=model_id or f"{provider_key}:{model_name}",
            elapsed_time=elapsed_time,
            error=f"HTTP错误 {response.status_code}",
            status_code=response.status_code,
            error_detail=error_detail
        )
        return False

    except Exception as e:
        error_msg = f"模型测试失败: {e}"
        print(f"   ❌ {error_msg}")
        import traceback
        error_detail = traceback.format_exc()
        save_test_response_to_config(
            model_id=model_id or f"unknown:{model_name}",
            error="异常错误",
            error_detail=error_detail
        )
        return False


def test_anthropic_compatible_model(config: Dict[str, Any], model_config: Dict[str, Any], model_name: str, model_id: str = None) -> bool:
    """测试 Anthropic Messages 兼容协议模型。"""
    try:
        import requests
        provider_key = str(model_config.get("_provider_key") or "unknown_provider")
        base_url_raw = model_config.get("base_url")
        if not base_url_raw:
            raise RuntimeError(f"{provider_key} 未配置 base_url")
        base_url = _normalize_base_url(str(base_url_raw))
        model = model_config.get("model", model_name)
        messages = get_test_messages(config)
        user_content = next((m.get("content") for m in messages if m.get("role") == "user"), "") or "Hello"

        # Anthropic 常见头：x-api-key + anthropic-version
        auth_header = str(model_config.get("auth_header") or "x-api-key").strip() or "x-api-key"
        auth_prefix = model_config.get("auth_prefix")
        if auth_prefix is None:
            auth_prefix = ""
        model_config = {**model_config, "auth_header": auth_header, "auth_prefix": str(auth_prefix)}

        headers = {
            "Content-Type": "application/json",
            "anthropic-version": str(model_config.get("anthropic_version") or "2023-06-01"),
            **_build_auth_headers(provider_key, model_config),
        }

        data = {
            "model": model,
            "max_tokens": int(model_config.get("max_tokens", 1024) or 1024),
            "messages": [
                {"role": "user", "content": user_content}
            ],
        }

        print(f"   📤 发送测试提示词: {user_content}")
        print(f"   🌐 请求URL: {base_url}")
        print(f"   🔑 模型: {model}")

        start_time = time.time()
        response = requests.post(base_url, headers=headers, json=data, timeout=int(model_config.get("request_timeout", 120) or 120))
        end_time = time.time()
        elapsed_time = end_time - start_time

        if response.status_code == 200:
            result = response.json()
            content = ""
            if isinstance(result, dict):
                content_blocks = result.get("content")
                if isinstance(content_blocks, list) and content_blocks:
                    first = content_blocks[0]
                    if isinstance(first, dict):
                        content = str(first.get("text") or "").strip()
            if not content:
                content = str(result)
            print(f"   📥 收到响应 (耗时: {elapsed_time:.2f}秒):")
            print(f"      {content}")
            save_test_response_to_config(content, model_id or f"{provider_key}:{model_name}", elapsed_time)
            return True

        error_msg = f"请求失败，状态码: {response.status_code}"
        error_detail = f"状态码: {response.status_code}, 响应: {response.text}"
        print(f"   ❌ {error_msg}")
        print(f"      {response.text}")
        save_test_response_to_config(
            model_id=model_id or f"{provider_key}:{model_name}",
            elapsed_time=elapsed_time,
            error=f"HTTP错误 {response.status_code}",
            status_code=response.status_code,
            error_detail=error_detail
        )
        return False
    except Exception as e:
        error_msg = f"模型测试失败: {e}"
        print(f"   ❌ {error_msg}")
        import traceback
        error_detail = traceback.format_exc()
        save_test_response_to_config(
            model_id=model_id or f"unknown:{model_name}",
            error="异常错误",
            error_detail=error_detail
        )
        return False


def test_openai_responses_compatible_model(config: Dict[str, Any], model_config: Dict[str, Any], model_name: str, model_id: str = None) -> bool:
    """测试 OpenAI Responses API 兼容协议模型。"""
    try:
        import requests
        provider_key = str(model_config.get("_provider_key") or "unknown_provider")
        base_url_raw = model_config.get("base_url")
        if not base_url_raw:
            raise RuntimeError(f"{provider_key} 未配置 base_url")
        base_url = _normalize_base_url(str(base_url_raw))
        model = model_config.get("model", model_name)
        messages = get_test_messages(config)
        user_content = next((m.get("content") for m in messages if m.get("role") == "user"), "") or "Hello"
        timeout_s = int(model_config.get("request_timeout", 120) or 120)
        headers = {
            "Content-Type": "application/json",
            **_build_auth_headers(provider_key, model_config),
        }
        data = {
            "model": model,
            "input": [
                {"role": "user", "content": user_content}
            ],
            "max_output_tokens": int(model_config.get("max_tokens", 1024) or 1024),
            "temperature": model_config.get("temperature", 0.1),
        }

        print(f"   📤 发送测试提示词: {user_content}")
        print(f"   🌐 请求URL: {base_url}")
        print(f"   🔑 模型: {model}")
        start_time = time.time()
        response = requests.post(base_url, headers=headers, json=data, timeout=timeout_s)
        end_time = time.time()
        elapsed_time = end_time - start_time

        if response.status_code == 200:
            result = response.json()
            content = ""
            if isinstance(result, dict):
                if isinstance(result.get("output_text"), str):
                    content = result.get("output_text", "").strip()
                if not content and isinstance(result.get("output"), list):
                    texts = []
                    for item in result["output"]:
                        if not isinstance(item, dict):
                            continue
                        blocks = item.get("content")
                        if not isinstance(blocks, list):
                            continue
                        for blk in blocks:
                            if isinstance(blk, dict) and blk.get("type") in ("output_text", "text"):
                                texts.append(str(blk.get("text") or ""))
                    content = "".join(texts).strip()
            if not content:
                content = str(result)
            print(f"   📥 收到响应 (耗时: {elapsed_time:.2f}秒):")
            print(f"      {content}")
            save_test_response_to_config(content, model_id or f"{provider_key}:{model_name}", elapsed_time)
            return True

        error_detail = f"状态码: {response.status_code}, 响应: {response.text}"
        print(f"   ❌ 请求失败，状态码: {response.status_code}")
        print(f"      {response.text}")
        save_test_response_to_config(
            model_id=model_id or f"{provider_key}:{model_name}",
            elapsed_time=elapsed_time,
            error=f"HTTP错误 {response.status_code}",
            status_code=response.status_code,
            error_detail=error_detail
        )
        return False
    except Exception as e:
        print(f"   ❌ 模型测试失败: {e}")
        import traceback
        save_test_response_to_config(
            model_id=model_id or f"unknown:{model_name}",
            error="异常错误",
            error_detail=traceback.format_exc()
        )
        return False

def test_model_connection(config: Dict[str, Any], model_id: str, model_info: Dict[str, Any]) -> bool:
    """测试指定模型的连接"""
    provider = model_info["provider"]
    model_name = model_info["model"]
    display_name = model_info["display_name"]
    
    print(f"\n🔍 测试模型: {display_name} ({model_id})")
    print(f"   提供商: {provider}")
    print(f"   模型: {model_name}")
    
    model_cfg = dict(model_info["config"]) if isinstance(model_info.get("config"), dict) else {}
    model_cfg["_provider_key"] = provider

    request_format = str(model_cfg.get("request_format") or "openai_chat_completions_compatible").strip().lower()
    if request_format == "anthropic_messages_compatible":
        return test_anthropic_compatible_model(config, model_cfg, model_name, model_id)
    if request_format == "openai_responses_compatible":
        return test_openai_responses_compatible_model(config, model_cfg, model_name, model_id)
    if request_format == "custom_unsupported_need_adapter":
        print("   ❌ 当前 provider 标记为非 OpenAI 兼容协议，需要新增 adapter 后再测试。")
        save_test_response_to_config(
            model_id=model_id,
            error="请求协议不兼容",
            error_detail="request_format=custom_unsupported_need_adapter，当前测试脚本仅支持 OpenAI Chat Completions 兼容协议。"
        )
        return False

    return test_openai_compatible_model(config, model_cfg, model_name, model_id)

def run_comprehensive_test():
    """运行全面的模型测试"""
    print("=== 大模型连接全面测试 ===")
    print()
    
    # 加载配置
    config = load_agent_config()
    models = get_available_models(config)
    
    print(f"📋 发现 {len(models)} 个可用模型:")
    for model_id, model_info in models.items():
        print(f"   - {model_info['display_name']} ({model_id})")
    
    print()
    
    # 测试所有模型
    results = {}
    for model_id, model_info in models.items():
        success = test_model_connection(config, model_id, model_info)
        results[model_id] = success
    
    # 输出测试结果摘要
    print("\n" + "="*50)
    print("📊 测试结果摘要")
    print("="*50)
    
    success_count = 0
    for model_id, success in results.items():
        status = "✅ 成功" if success else "❌ 失败"
        model_name = models[model_id]["display_name"]
        print(f"{model_name} ({model_id}): {status}")
        if success:
            success_count += 1
    
    print(f"\n总计: {success_count}/{len(models)} 个模型测试成功")
    
    if success_count == len(models):
        print("🎉 所有模型测试成功！")
    elif success_count > 0:
        print("⚠️  部分模型测试成功，请检查失败的模型配置")
    else:
        print("💥 所有模型测试失败，请检查配置和网络连接")

def test_specific_model(target_model_id: str):
    """测试指定的模型"""
    print(f"=== 测试指定模型: {target_model_id} ===")
    print()
    
    # 加载配置
    config = load_agent_config()
    models = get_available_models(config)
    
    if target_model_id not in models:
        print(f"❌ 找不到模型: {target_model_id}")
        print("可用的模型:")
        for model_id in models.keys():
            print(f"   - {model_id}")
        return
    
    # 测试指定模型
    model_info = models[target_model_id]
    success = test_model_connection(config, target_model_id, model_info)
    
    if success:
        print(f"\n✅ 模型 {target_model_id} 测试成功！")
    else:
        print(f"\n❌ 模型 {target_model_id} 测试失败！")

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='大模型连接测试脚本')
    parser.add_argument('--model', '-m', 
                       help='指定要测试的模型ID (例如: openai:gpt-4)')
    parser.add_argument('--all', '-a', action='store_true',
                       help='测试所有可用模型')
    
    args = parser.parse_args()
    
    try:
        if args.model:
            # 测试指定模型
            test_specific_model(args.model)
        elif args.all:
            # 测试所有模型
            run_comprehensive_test()
        else:
            # 默认测试配置中的默认模型（优先智谱）
            config = load_agent_config()
            models = get_available_models(config)
            default_model_id = get_default_model_id(config, models)
            if not default_model_id:
                print("❌ 未找到可用模型，请检查配置文件")
                return
            print(f"未指定参数，默认测试模型: {default_model_id}")
            test_specific_model(default_model_id)
            
    except KeyboardInterrupt:
        print("\n\n⏹️  用户中断测试")
    except Exception as e:
        print(f"\n💥 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
