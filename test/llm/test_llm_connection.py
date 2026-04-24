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
from typing import Dict, Any, Optional

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

def get_available_models(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """获取可用的模型列表，兼容单模型和多模型两种配置格式"""
    llm_config = config.get("llm_config", {})
    providers = llm_config.get("providers", {})
    if not isinstance(providers, dict):
        return {}

    models = {}
    for provider_key, provider_info in providers.items():
        if not isinstance(provider_info, dict):
            continue
        provider_name = provider_info.get("name", provider_key)
        provider_models = provider_info.get("models", {})

        if isinstance(provider_models, dict) and provider_models:
            for model_key, model_info in provider_models.items():
                if not isinstance(model_info, dict):
                    model_info = {}
                model_id = f"{provider_key}:{model_key}"
                models[model_id] = {
                    "provider": provider_key,
                    "model": model_key,
                    "display_name": model_info.get("display_name", model_key),
                    "config": model_info
                }
            continue

        model_name = provider_info.get("model")
        if not model_name:
            continue
        model_id = f"{provider_key}:{model_name}"
        single_model_config = dict(provider_info)
        single_model_config.setdefault("model", model_name)
        models[model_id] = {
            "provider": provider_key,
            "model": model_name,
            "display_name": provider_name,
            "config": single_model_config,
        }

    return models

def get_default_model_id(config: Dict[str, Any], models: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """从配置获取默认模型ID，支持仅配置 default_provider 的场景"""
    default_provider = _get_nested(config, ["llm_config", "default_provider"])
    default_model = _get_nested(config, ["llm_config", "default_model"])

    if default_provider and default_model:
        candidate = f"{default_provider}:{default_model}"
        if candidate in models:
            return candidate

    if default_provider:
        provider_cfg = _get_nested(config, ["llm_config", "providers", default_provider], {})
        if isinstance(provider_cfg, dict):
            provider_model = provider_cfg.get("model")
            if provider_model:
                candidate = f"{default_provider}:{provider_model}"
                if candidate in models:
                    return candidate
        for model_id, model_info in models.items():
            if model_info.get("provider") == default_provider:
                return model_id

    # 兜底：取第一个可用模型
    return next(iter(models.keys()), None)



def test_baidu_qianfan_model(config: Dict[str, Any], model_config: Dict[str, Any], model_name: str, model_id: str = None) -> bool:
    """测试百度智能云千帆模型"""
    try:
        import requests

        def _post_with_retry(url: str, headers: dict, data: dict) -> "requests.Response":
            """
            对 429（RPM/TPM 限流）进行短暂退避重试：
            - 默认最多重试 3 次
            - 默认等待 2s/4s/8s
            - 若最终仍 429，默认视为“可达但被限流”（返回最后一次响应，由上层决定是否算成功）
            """
            max_retries = int(os.getenv("QIANFAN_TEST_MAX_RETRIES", "3") or "3")
            sleep_s = int(os.getenv("QIANFAN_TEST_RETRY_SLEEP_SECONDS", "2") or "2")
            timeout_s = int(os.getenv("QIANFAN_TEST_TIMEOUT_SECONDS", "30") or "30")

            resp = None
            for attempt in range(1, max_retries + 1):
                resp = requests.post(url, headers=headers, json=data, timeout=timeout_s)
                if resp.status_code != 429:
                    return resp
                print(f"   ⚠️  命中 429 限流，{sleep_s}s 后重试（{attempt}/{max_retries}）")
                time.sleep(sleep_s)
                sleep_s = min(sleep_s * 2, 30)
            return resp  # type: ignore
        
        # 检查授权信息 - 从环境变量或配置文件中获取
        authorization = get_qianfan_authorization(config)
        
        if not authorization:
            error_msg = "缺少百度智能云授权信息，请设置BAIDU_QIANFAN_AUTHORIZATION环境变量或在tools/configs/agent_config.local.json中配置llm_config.providers.baidu_qianfan.authorization"
            print(f"   ❌ {error_msg}")
            save_test_response_to_config(
                model_id=model_id or f"baidu_qianfan:{model_name}",
                error="缺少授权信息",
                error_detail=error_msg
            )
            return False
        
        # 获取基础URL和模型名称
        base_url = model_config.get("base_url", "https://qianfan.baidubce.com/v2/chat/completions")
        model = model_config.get("model", model_name)
        
        # 准备请求数据
        messages = get_test_messages(config)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json"
        }
        
        data = {
            "model": model,
            "messages": messages,
            "temperature": model_config.get("temperature", 0.1),
            "max_tokens": model_config.get("max_tokens", 1000)
        }
        
        # 发送请求
        test_prompt = next((m.get("content") for m in messages if m.get("role") == "user"), "")
        print(f"   📤 发送测试提示词: {test_prompt}")
        print(f"   🌐 请求URL: {base_url}")
        print(f"   🔑 模型: {model}")
        
        start_time = time.time()
        response = _post_with_retry(base_url, headers, data)
        end_time = time.time()
        
        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0]["message"]["content"]
                elapsed_time = end_time - start_time
                print(f"   📥 收到响应 (耗时: {elapsed_time:.2f}秒):")
                print(f"      {content}")
                # 保存响应结果到配置文件
                save_test_response_to_config(content, model_id or f"baidu_qianfan:{model_name}", elapsed_time)
                return True
            else:
                error_msg = f"响应格式异常: {result}"
                print(f"   ❌ {error_msg}")
                elapsed_time = end_time - start_time
                save_test_response_to_config(
                    model_id=model_id or f"baidu_qianfan:{model_name}",
                    elapsed_time=elapsed_time,
                    error="响应格式异常",
                    status_code=200,
                    error_detail=str(result)
                )
                return False
        else:
            # 429：表示已鉴权/可达，但当前窗口触发 RPM/TPM 限流。默认不判定为“连接失败”。
            elapsed_time = end_time - start_time
            if response.status_code == 429:
                strict = os.getenv("QIANFAN_TEST_STRICT", "").lower() in ("1", "true", "yes")
                error_detail = f"请求被限流（状态码: 429），说明服务可达但当前触发 RPM/TPM 限流窗口。响应: {response.text}"
                print(f"   ⚠️  请求被限流（状态码: 429），说明服务可达但当前触发 RPM/TPM 限流窗口。")
                print(f"      {response.text}")
                if strict:
                    print("   ❌ QIANFAN_TEST_STRICT=1：严格模式下将 429 视为失败。")
                    save_test_response_to_config(
                        model_id=model_id or f"baidu_qianfan:{model_name}",
                        elapsed_time=elapsed_time,
                        error="请求被限流（严格模式）",
                        status_code=429,
                        error_detail=error_detail
                    )
                    return False
                print("   ✅ 默认模式：将 429 视为“可达但被限流”，本次连接测试判定为通过（可稍后重试验证真实返回）。")
                # 429 在非严格模式下视为成功，但也保存限流信息
                save_test_response_to_config(
                    model_id=model_id or f"baidu_qianfan:{model_name}",
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
                model_id=model_id or f"baidu_qianfan:{model_name}",
                elapsed_time=elapsed_time,
                error=f"HTTP错误 {response.status_code}",
                status_code=response.status_code,
                error_detail=error_detail
            )
            return False
        
    except Exception as e:
        error_msg = f"百度智能云千帆模型测试失败: {e}"
        print(f"   ❌ {error_msg}")
        import traceback
        error_detail = traceback.format_exc()
        save_test_response_to_config(
            model_id=model_id or f"baidu_qianfan:{model_name}",
            error="异常错误",
            error_detail=error_detail
        )
        return False

def test_zhipu_bigmodel(config: Dict[str, Any], model_config: Dict[str, Any], model_name: str, model_id: str = None) -> bool:
    """测试智谱 BigModel"""
    try:
        import requests

        # 检查授权信息 - 从环境变量或配置文件中获取
        api_key = get_zhipu_authorization(config)
        if not api_key:
            error_msg = "缺少智谱鉴权信息，请设置ZHIPU_API_KEY/BIGMODEL_API_KEY或在配置中填写llm_config.providers.zhipu_bigmodel.api_key"
            print(f"   ❌ {error_msg}")
            save_test_response_to_config(
                model_id=model_id or f"zhipu_bigmodel:{model_name}",
                error="缺少授权信息",
                error_detail=error_msg
            )
            return False
        if not str(api_key).startswith("Bearer "):
            api_key = f"Bearer {api_key}"

        # 统一 base_url（支持传入到根路径，自动补 /chat/completions）
        base_url = model_config.get("base_url") or _get_nested(config, ["llm_config", "providers", "zhipu_bigmodel", "base_url"]) or "https://open.bigmodel.cn/api/paas/v4"
        if str(base_url).endswith("/"):
            base_url = str(base_url)[:-1]
        if not str(base_url).endswith("/chat/completions"):
            base_url = f"{base_url}/chat/completions"

        model = model_config.get("model", model_name)
        messages = get_test_messages(config)

        headers = {
            "Authorization": api_key,
            "Content-Type": "application/json"
        }

        data = {
            "model": model,
            "messages": messages,
            "temperature": model_config.get("temperature", 0.2),
            "max_tokens": model_config.get("max_tokens", 1000),
            "stream": False
        }

        if model_config.get("do_sample") is not None:
            data["do_sample"] = bool(model_config.get("do_sample"))
        if model_config.get("top_p") is not None:
            data["top_p"] = model_config.get("top_p")
        if model_config.get("thinking") is not None:
            data["thinking"] = model_config.get("thinking")

        test_prompt = next((m.get("content") for m in messages if m.get("role") == "user"), "")
        print(f"   📤 发送测试提示词: {test_prompt}")
        print(f"   🌐 请求URL: {base_url}")
        print(f"   🔑 模型: {model}")

        start_time = time.time()
        response = requests.post(base_url, headers=headers, json=data, timeout=model_config.get("request_timeout", 60))
        end_time = time.time()

        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0]["message"]["content"]
                elapsed_time = end_time - start_time
                print(f"   📥 收到响应 (耗时: {elapsed_time:.2f}秒):")
                print(f"      {content}")
                # 保存响应结果到配置文件
                save_test_response_to_config(content, model_id or f"zhipu_bigmodel:{model_name}", elapsed_time)
                return True
            error_msg = f"响应格式异常: {result}"
            print(f"   ❌ {error_msg}")
            elapsed_time = end_time - start_time
            save_test_response_to_config(
                model_id=model_id or f"zhipu_bigmodel:{model_name}",
                elapsed_time=elapsed_time,
                error="响应格式异常",
                status_code=200,
                error_detail=str(result)
            )
            return False

        elapsed_time = end_time - start_time
        error_msg = f"请求失败，状态码: {response.status_code}"
        error_detail = f"状态码: {response.status_code}, 响应: {response.text}"
        print(f"   ❌ {error_msg}")
        print(f"      {response.text}")
        save_test_response_to_config(
            model_id=model_id or f"zhipu_bigmodel:{model_name}",
            elapsed_time=elapsed_time,
            error=f"HTTP错误 {response.status_code}",
            status_code=response.status_code,
            error_detail=error_detail
        )
        return False

    except Exception as e:
        error_msg = f"智谱 BigModel 测试失败: {e}"
        print(f"   ❌ {error_msg}")
        import traceback
        error_detail = traceback.format_exc()
        save_test_response_to_config(
            model_id=model_id or f"zhipu_bigmodel:{model_name}",
            error="异常错误",
            error_detail=error_detail
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
    
    # 根据提供商选择测试方法
    if provider == "baidu_qianfan":
        return test_baidu_qianfan_model(config, model_info["config"], model_name, model_id)
    if provider == "zhipu_bigmodel":
        return test_zhipu_bigmodel(config, model_info["config"], model_name, model_id)
    print(f"   ❌ 不支持的提供商: {provider}")
    return False

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
