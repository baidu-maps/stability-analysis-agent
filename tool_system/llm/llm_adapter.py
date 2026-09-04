#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 适配器接口定义 - 支持多种调用方式
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Generator, Union

from tool_system.llm.http_ssl import urllib_urlopen

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """统一响应格式"""
    content: str
    tool_calls: Optional[List[Dict]] = None  # 如果有工具调用
    usage: Optional[Dict] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class BaseLLMAdapter(ABC):
    """LLM 适配器基类 - 抽象三种调用方式"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 LLM 适配器

        Args:
            config: 配置信息
        """
        self.config = config
        self.model = config.get("model", "glm-4")
        self.timeout = config.get("timeout", 120)
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 4096)

    @abstractmethod
    def chat(self,
             messages: List[Dict[str, str]],
             tools: Optional[List[Dict]] = None,
             **kwargs) -> LLMResponse:
        """
        同步调用

        Args:
            messages: 消息列表
            tools: 可用的工具定义列表
            **kwargs: 其他参数

        Returns:
            LLM 响应
        """
        pass

    @abstractmethod
    def stream(self,
               messages: List[Dict[str, str]],
               tools: Optional[List[Dict]] = None,
               **kwargs) -> Generator[str, None, None]:
        """
        流式调用

        Args:
            messages: 消息列表
            tools: 可用的工具定义列表
            **kwargs: 其他参数

        Yields:
            流式输出的内容片段
        """
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.model}>"


class ReplayLLMAdapter(BaseLLMAdapter):
    """Deterministic, network-free adapter for CI and report replay."""

    @staticmethod
    def _load_responses_from_report(report_dir: Path) -> List[Any]:
        root = Path(report_dir).expanduser().resolve()
        responses: List[Any] = []
        for round_dir in sorted(root.glob("round_*")):
            for name in ("07_ai_gen_res.md", "07_ai_gen_res.txt"):
                path = round_dir / name
                if path.is_file():
                    responses.append({"content": path.read_text(encoding="utf-8")})
                    break
        fixture = root / "offline_replay_responses.json"
        if not responses and fixture.is_file():
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                responses = list(payload.get("responses") or [])
            elif isinstance(payload, list):
                responses = payload
        return responses

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        responses = config.get("responses")
        response_file = str(config.get("response_file") or "").strip()
        report_dir = str(config.get("report_dir") or "").strip()
        if response_file:
            payload = json.loads(Path(response_file).expanduser().resolve().read_text(encoding="utf-8"))
            responses = payload.get("responses") if isinstance(payload, dict) else payload
        if report_dir and not responses:
            responses = self._load_responses_from_report(Path(report_dir))
        if not isinstance(responses, list) or not responses:
            raise ValueError("offline_replay requires a non-empty responses list or response_file")
        self._responses = list(responses)
        self._cursor = 0
        self._lock = threading.Lock()
        self.provider = "offline_replay"

    def _next(self) -> LLMResponse:
        with self._lock:
            if self._cursor >= len(self._responses):
                raise RuntimeError("offline replay responses exhausted")
            raw = self._responses[self._cursor]
            index = self._cursor
            self._cursor += 1
        if isinstance(raw, str):
            return LLMResponse(content=raw, metadata={"provider": self.provider, "replay_index": index})
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), str):
            raise ValueError(f"offline replay response {index} must be a string or object with content")
        metadata = dict(raw.get("metadata") or {}) if isinstance(raw.get("metadata"), dict) else {}
        metadata.update(provider=self.provider, replay_index=index)
        if raw.get("estimated_cost") is not None:
            metadata["estimated_cost"] = float(raw["estimated_cost"])
        return LLMResponse(
            content=raw["content"],
            tool_calls=list(raw.get("tool_calls") or []) or None,
            usage=dict(raw.get("usage") or {}) or None,
            metadata=metadata,
        )

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        del messages, tools, kwargs
        return self._next()

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
        yield self.chat(messages, tools, **kwargs).content


# ========== 三种实现 ==========

class DirectLLMAdapter(BaseLLMAdapter):
    """
    Direct 方式：直接拼装提示词，一次调用
    适用于简单固定流程
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._request_format = str(config.get("request_format") or "openai_chat_completions_compatible").strip().lower()
        self._auth_header = str(config.get("auth_header") or "Authorization").strip() or "Authorization"
        self._auth_prefix = str(config.get("auth_prefix") or "")
        self._stream_options_supported: Optional[bool] = None  # 缓存 stream_options 兼容性
        # 是否使用流式调用（默认 True，可通过配置关闭）
        self._use_stream = config.get("stream", True)
        if isinstance(self._use_stream, str):
            self._use_stream = self._use_stream.lower() not in ("false", "0", "no")
        if self._request_format in ("anthropic_messages_compatible", "openai_responses_compatible"):
            self.client = None
            return
        self._init_client(config)

    def _init_client(self, config: Dict[str, Any]):
        """初始化 LLM 客户端"""
        # 根据不同的 provider 初始化不同的客户端
        provider = config.get("provider", "openai")  # openai/deepseek/baidu_qianfan/wenxin

        if provider in ("openai", "glm", "baidu_qianfan"):
            try:
                from openai import OpenAI
                api_key = (
                    config.get("api_key")
                    or config.get("authorization")
                    or config.get("openai_api_key")
                    or config.get("glm_api_key")
                    or config.get("baidu_qianfan_authorization")
                )
                default_base_url = (
                    "https://qianfan.baidubce.com/v2"
                    if provider == "baidu_qianfan"
                    else "https://open.bigmodel.cn/api/paas/v4"
                )
                base_url = config.get("base_url") or config.get("openai_base_url") or default_base_url
                # 使用 httpx.Timeout 实现细粒度超时：
                # - connect: 连接超时（快速检测网络不通）
                # - read: 读取超时（流式时为 chunk 间隔，需要足够长以容纳 TTFT）
                # 对流式调用：首 token 可能需要较长等待（复杂 prompt 时 LLM 思考时间长），
                # 但建立连接应该很快。
                raw_timeout = float(self.timeout or 180)
                try:
                    import httpx
                    client_timeout = httpx.Timeout(
                        connect=min(30.0, raw_timeout),
                        read=max(raw_timeout, 300.0),  # 读取至少 300s，防止大 prompt TTFT 超时
                        write=30.0,
                        pool=30.0,
                    )
                except ImportError:
                    client_timeout = raw_timeout
                self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=client_timeout)
                logger.info(f"Initialized {provider} client: {base_url}")
            except ImportError:
                logger.warning("openai package not installed, DirectLLMAdapter will not work")
                self.client = None
        elif provider == "deepseek":
            try:
                from openai import OpenAI
                raw_timeout = float(self.timeout or 180)
                try:
                    import httpx
                    client_timeout = httpx.Timeout(
                        connect=min(30.0, raw_timeout),
                        read=max(raw_timeout, 300.0),
                        write=30.0,
                        pool=30.0,
                    )
                except ImportError:
                    client_timeout = raw_timeout
                self.client = OpenAI(
                    api_key=config.get("deepseek_api_key"),
                    base_url=config.get("deepseek_base_url", "https://api.deepseek.com/v1"),
                    timeout=client_timeout,
                )
            except ImportError:
                self.client = None
        else:
            logger.warning(f"Unknown provider: {provider}")
            self.client = None

    def _secret_for_http(self) -> str:
        return str(
            self.config.get("api_key")
            or self.config.get("authorization")
            or self.config.get("openai_api_key")
            or ""
        ).strip()

    @staticmethod
    def _extract_meaningful_error(error_obj: Any) -> str:
        """
        一些网关会返回成功响应，同时附带空错误对象：{"type":"","message":""}。
        仅当错误对象包含有效信息时才视为失败。
        """
        if not error_obj:
            return ""
        if isinstance(error_obj, str):
            return error_obj.strip()
        if isinstance(error_obj, dict):
            for key in ("message", "type", "code", "param"):
                val = str(error_obj.get(key) or "").strip()
                if val:
                    return val
            return ""
        return str(error_obj).strip()

    def _anthropic_messages_http_chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        base_url = (self.config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("anthropic_messages_compatible 需要配置 base_url（完整 /v1/messages 地址）")
        secret = self._secret_for_http()
        if not secret:
            raise RuntimeError("anthropic_messages_compatible 需要 api_key 或 authorization")

        url = base_url if base_url.endswith("/messages") else f"{base_url}/messages"
        headers = {"Content-Type": "application/json"}
        headers[self._auth_header] = f"{self._auth_prefix}{secret}"
        if "anthropic.com" in url.lower():
            headers.setdefault("anthropic-version", "2023-06-01")

        # Anthropic Messages API 要求 system 放在顶层字段，不能作为 messages 里的 role。
        system_parts: List[str] = []
        normalized_messages: List[Dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "")
            if role == "system":
                if content.strip():
                    system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                # 兜底按 user 处理，避免网关因未知角色拒绝。
                role = "user"
            normalized_messages.append({"role": role, "content": content})
        if not normalized_messages:
            normalized_messages = [{"role": "user", "content": "pong"}]

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": normalized_messages,
            "max_tokens": int(kwargs.get("max_tokens", self.max_tokens) or 1024),
        }
        # Thinking 模型需要更大的 max_tokens 预算（thinking + text 共用）
        if "thinking" in (self.model or "").lower():
            body["max_tokens"] = max(body["max_tokens"], 32000)
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if kwargs.get("temperature") is not None:
            body["temperature"] = float(kwargs["temperature"])
        elif self.temperature is not None:
            body["temperature"] = float(self.temperature)
        # Thinking 模型或经网关转发的 Anthropic 请求：使用非流式调用更可靠
        # 1. thinking 模型流式调用时 thinking 阶段长时间无数据，网关可能导致 socket read timeout
        # 2. 非流式让服务端完整生成后返回，避免中间超时
        # 3. 对 anthropic_messages_compatible 默认非流式（除非 config 显式设 stream=true），
        #    因为 Anthropic 非流式延迟本身合理，而网关流式转发有兼容性风险
        is_thinking_model = "thinking" in (self.model or "").lower()
        # 只有在配置显式设置 stream 且不是 thinking 模型时才用流式
        explicit_stream = self.config.get("stream")  # None 表示未显式配置
        use_stream_for_this_request = (
            (explicit_stream is True or (isinstance(explicit_stream, str) and explicit_stream.lower() in ("true", "1", "yes")))
            and not is_thinking_model
        )
        if use_stream_for_this_request:
            body["stream"] = True

        req = urllib.request.Request(
            url=url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        # 流式调用只需等首个 chunk，但 thinking 模型 TTFT 可能很长（数分钟思考后才出首 token）
        base_timeout = max(float(self.timeout or 180), 300.0)
        if "thinking" in (self.model or "").lower():
            http_timeout = max(base_timeout, 600.0)
        else:
            http_timeout = base_timeout
        try:
            resp = urllib_urlopen(req, timeout=http_timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"LLM call failed: Error code: {exc.code} - {detail or exc.reason}") from exc

        if self._use_stream and body.get("stream"):
            return self._parse_anthropic_sse(resp)
        else:
            raw = resp.read().decode("utf-8", errors="ignore")
            resp.close()
            return self._parse_anthropic_response(raw)

    def _parse_anthropic_sse(self, resp) -> LLMResponse:
        """解析 Anthropic Messages SSE 流式响应。

        支持 Claude thinking 模型：先输出 thinking content_block，再输出 text content_block。
        如果 text 为空但 thinking 有内容，使用 thinking 作为 fallback。
        """
        content_parts: List[str] = []
        thinking_parts: List[str] = []
        usage_dict = None
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").rstrip("\n\r")
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
                event_type = event.get("type", "")
                # Anthropic 原生格式
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type", "")
                    if delta_type == "text_delta":
                        content_parts.append(delta.get("text", ""))
                    elif delta_type == "thinking_delta":
                        thinking_parts.append(delta.get("thinking", ""))
                elif event_type == "message_delta":
                    usage_info = event.get("usage")
                    if isinstance(usage_info, dict):
                        usage_dict = {
                            "prompt_tokens": usage_info.get("input_tokens", 0),
                            "completion_tokens": usage_info.get("output_tokens", 0),
                        }
                # OpenAI 兼容网关格式（OneAPI 等）
                elif "choices" in event:
                    choices = event.get("choices", [])
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            content_parts.append(text)
                    # usage
                    if event.get("usage"):
                        u = event["usage"]
                        usage_dict = {
                            "prompt_tokens": u.get("prompt_tokens", 0),
                            "completion_tokens": u.get("completion_tokens", 0),
                        }
        finally:
            resp.close()
        content = "".join(content_parts)
        if not content.strip() and thinking_parts:
            # thinking 模型的 text 为空说明 max_tokens 被 thinking 消耗殆尽，
            # 不能用 thinking 内容替代（它是内部推理，非结构化输出）。
            # 记录 warning，返回空内容让调用方重试。
            logger.warning(
                "[LLM] thinking model text output is empty (thinking used %d chars). "
                "Consider increasing max_tokens or using non-thinking model.",
                len("".join(thinking_parts)),
            )
        return LLMResponse(
            content=content,
            usage=usage_dict,
            metadata={"provider": self.config.get("provider", "openai"), "request_format": self._request_format},
        )

    def _parse_anthropic_response(self, raw: str) -> LLMResponse:
        """解析 Anthropic Messages 非流式响应。"""
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            err_text = self._extract_meaningful_error(data.get("error"))
            if err_text:
                raise RuntimeError(f"LLM call failed: {err_text}")

        content = ""
        usage_dict = None
        if isinstance(data, dict):
            blocks = data.get("content")
            if isinstance(blocks, list):
                parts: List[str] = []
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                content = "".join(parts)
            if not content and isinstance(data.get("choices"), list) and data["choices"]:
                ch0 = data["choices"][0]
                if isinstance(ch0, dict):
                    msg = ch0.get("message") or {}
                    if isinstance(msg, dict):
                        content = str(msg.get("content") or "")
            # 提取 usage
            usage_info = data.get("usage")
            if isinstance(usage_info, dict):
                usage_dict = {
                    "prompt_tokens": usage_info.get("input_tokens", 0) or usage_info.get("prompt_tokens", 0),
                    "completion_tokens": usage_info.get("output_tokens", 0) or usage_info.get("completion_tokens", 0),
                }

        return LLMResponse(
            content=content,
            usage=usage_dict,
            metadata={"provider": self.config.get("provider", "openai"), "request_format": self._request_format},
        )

    def _openai_responses_http_chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        base_url = (self.config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("openai_responses_compatible 需要配置 base_url")
        secret = self._secret_for_http()
        if not secret:
            raise RuntimeError("openai_responses_compatible 需要 api_key")

        url = base_url if base_url.endswith("/responses") else f"{base_url}/responses"
        headers = {"Content-Type": "application/json"}
        headers[self._auth_header] = f"{self._auth_prefix}{secret}"

        user_text = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
        if not user_text.strip():
            user_text = "\n".join(str(m.get("content") or "") for m in messages)

        body = {
            "model": self.model,
            "input": user_text,
            "max_output_tokens": int(kwargs.get("max_tokens", self.max_tokens) or 1024),
        }

        req = urllib.request.Request(
            url=url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib_urlopen(req, timeout=max(float(self.timeout or 180), 300.0)) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"LLM call failed: Error code: {exc.code} - {detail or exc.reason}") from exc

        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            err_text = self._extract_meaningful_error(data.get("error"))
            if err_text:
                raise RuntimeError(f"LLM call failed: {err_text}")

        content = ""
        if isinstance(data, dict):
            out = data.get("output")
            if isinstance(out, list):
                for item in out:
                    if isinstance(item, dict) and item.get("type") == "message":
                        inner = item.get("content")
                        if isinstance(inner, list):
                            for block in inner:
                                if isinstance(block, dict) and block.get("type") == "output_text":
                                    content += str(block.get("text") or "")

        return LLMResponse(
            content=content,
            usage=None,
            metadata={"provider": self.config.get("provider", "openai"), "request_format": self._request_format},
        )

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        if self._request_format == "anthropic_messages_compatible":
            try:
                return self._anthropic_messages_http_chat(messages, **kwargs)
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                raise
        if self._request_format == "openai_responses_compatible":
            try:
                return self._openai_responses_http_chat(messages, **kwargs)
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                raise

        if self.client is None:
            raise RuntimeError("LLM client not initialized")

        # 构建请求参数
        request_params = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        # 根据配置选择流式或非流式调用
        if not self._use_stream:
            return self._chat_non_stream(request_params)

        request_params["stream"] = True
        # 使用流式调用拼接完整响应：
        # - 大部分 provider 的流式响应比非流式更快完成（减少序列化开销）
        # - 与 provider Web UI 行为一致
        try:
            # stream_options 兼容性检测（只在首次调用时探测）
            if self._stream_options_supported is not False:
                try:
                    params_with_usage = {**request_params, "stream_options": {"include_usage": True}}
                    response = self.client.chat.completions.create(**params_with_usage)
                    self._stream_options_supported = True
                except Exception:
                    self._stream_options_supported = False
                    response = self.client.chat.completions.create(**request_params)
            else:
                response = self.client.chat.completions.create(**request_params)

            chunks: list = []
            usage_info = None
            for chunk in response:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage_info = chunk.usage
                if chunk.choices and chunk.choices[0].delta.content:
                    chunks.append(chunk.choices[0].delta.content)
            content = "".join(chunks)

            usage_dict = None
            if usage_info:
                usage_dict = {
                    "prompt_tokens": getattr(usage_info, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage_info, "completion_tokens", 0) or 0,
                }
            elif chunks:
                # 无 usage 信息时粗估
                usage_dict = {
                    "prompt_tokens": 0,
                    "completion_tokens": len(content) // 4,
                }

            return LLMResponse(
                content=content,
                usage=usage_dict,
                metadata={"provider": self.config.get("provider", "openai")}
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise RuntimeError(f"LLM call failed: {e}")

    def _chat_non_stream(self, request_params: Dict[str, Any]) -> LLMResponse:
        """非流式调用（用户可通过 stream=false 配置选择此路径）。"""
        try:
            response = self.client.chat.completions.create(**request_params)
            content = response.choices[0].message.content if response.choices else ""
            usage_dict = None
            if hasattr(response, "usage") and response.usage:
                usage_dict = {
                    "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                }
            return LLMResponse(
                content=content or "",
                usage=usage_dict,
                metadata={"provider": self.config.get("provider", "openai")}
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise RuntimeError(f"LLM call failed: {e}")

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
        if self._request_format in ("anthropic_messages_compatible", "openai_responses_compatible"):
            resp = self.chat(messages, tools, **kwargs)
            if resp.content:
                yield resp.content
            return

        if self.client is None:
            raise RuntimeError("LLM client not initialized")

        request_params = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": True,
        }

        try:
            response = self.client.chat.completions.create(**request_params)
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error(f"LLM stream failed: {e}")
            raise RuntimeError(f"LLM stream failed: {e}")


class LangChainLLMAdapter(BaseLLMAdapter):
    """Pure LangChain model backend; orchestration belongs to AgentRuntime."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._init_langchain(config)

    def _init_langchain(self, config: Dict[str, Any]):
        """初始化 LangChain"""
        try:
            from langchain_openai import ChatOpenAI
            provider = config.get("provider", "openai")
            if provider in ("openai", "glm", "baidu_qianfan"):
                api_key = (
                    config.get("api_key")
                    or config.get("authorization")
                    or config.get("openai_api_key")
                    or config.get("glm_api_key")
                    or config.get("baidu_qianfan_authorization")
                )
                default_base_url = (
                    "https://qianfan.baidubce.com/v2"
                    if provider == "baidu_qianfan"
                    else "https://open.bigmodel.cn/api/paas/v4"
                )
                base_url = config.get("base_url") or config.get("openai_base_url") or default_base_url

                self.llm = ChatOpenAI(
                    model=self.model,
                    api_key=api_key,
                    base_url=base_url,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout
                )
            elif provider == "deepseek":
                from langchain_openai import ChatOpenAI
                self.llm = ChatOpenAI(
                    model=self.model,
                    api_key=config.get("deepseek_api_key"),
                    base_url=config.get("deepseek_base_url", "https://api.deepseek.com/v1"),
                    temperature=self.temperature,
                    max_tokens=self.max_tokens
                )

            else:
                raise ValueError(f"unsupported LangChain provider: {provider}")
            self.provider = str(provider)
            logger.info("LangChain model backend initialized successfully")
        except ImportError as e:
            logger.warning(f"LangChain not available: {e}")
            self.llm = None

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        del tools
        if self.llm is None:
            raise RuntimeError("LangChain model backend is unavailable")
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        role_types = {"system": SystemMessage, "assistant": AIMessage, "user": HumanMessage}
        converted = [role_types.get(str(item.get("role") or "user"), HumanMessage)(content=str(item.get("content") or "")) for item in messages]
        response = self.llm.invoke(converted, **kwargs)
        usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get("token_usage")
        return LLMResponse(content=str(getattr(response, "content", "")), usage=usage,
                           metadata={"method": "langchain_model"})

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
        del tools
        if self.llm is None:
            raise RuntimeError("LangChain model backend is unavailable")
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        role_types = {"system": SystemMessage, "assistant": AIMessage, "user": HumanMessage}
        converted = [role_types.get(str(item.get("role") or "user"), HumanMessage)(content=str(item.get("content") or "")) for item in messages]
        for chunk in self.llm.stream(converted, **kwargs):
            content = getattr(chunk, "content", "")
            if content:
                yield str(content)


class LangGraphLLMAdapter(BaseLLMAdapter):
    """Pure LangGraph-stack model backend without a business state graph."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._init_langgraph(config)

    def _init_langgraph(self, config: Dict[str, Any]):
        """初始化 LangGraph"""
        try:
            # Importing langgraph is intentional: this backend verifies that
            # the selected stack is installed, but AgentRuntime owns the graph.
            import langgraph  # noqa: F401
            from langchain_openai import ChatOpenAI

            provider = config.get("provider", "openai")
            if provider in ("openai", "glm", "baidu_qianfan"):
                api_key = (
                    config.get("api_key")
                    or config.get("authorization")
                    or config.get("openai_api_key")
                    or config.get("glm_api_key")
                    or config.get("baidu_qianfan_authorization")
                )
                default_base_url = (
                    "https://qianfan.baidubce.com/v2"
                    if provider == "baidu_qianfan"
                    else "https://open.bigmodel.cn/api/paas/v4"
                )
                base_url = config.get("base_url") or config.get("openai_base_url") or default_base_url

                self.llm = ChatOpenAI(
                    model=self.model,
                    api_key=api_key,
                    base_url=base_url,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout
                )
            elif provider == "deepseek":
                self.llm = ChatOpenAI(
                    model=self.model,
                    api_key=config.get("deepseek_api_key"),
                    base_url=config.get("deepseek_base_url", "https://api.deepseek.com/v1"),
                    temperature=self.temperature,
                    max_tokens=self.max_tokens
                )

            else:
                raise ValueError(f"unsupported LangGraph provider: {provider}")
            self.provider = str(provider)
            logger.info("LangGraph model backend initialized successfully")
        except ImportError as e:
            logger.warning(f"LangGraph not available: {e}")
            self.llm = None

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        del tools
        if self.llm is None:
            raise RuntimeError("LangGraph model backend is unavailable")
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        role_types = {"system": SystemMessage, "assistant": AIMessage, "user": HumanMessage}
        converted = [role_types.get(str(item.get("role") or "user"), HumanMessage)(content=str(item.get("content") or "")) for item in messages]
        response = self.llm.invoke(converted, **kwargs)
        usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get("token_usage")
        return LLMResponse(content=str(getattr(response, "content", "")), usage=usage,
                           metadata={"method": "langgraph_model"})

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
        del tools
        if self.llm is None:
            raise RuntimeError("LangGraph model backend is unavailable")
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        role_types = {"system": SystemMessage, "assistant": AIMessage, "user": HumanMessage}
        converted = [role_types.get(str(item.get("role") or "user"), HumanMessage)(content=str(item.get("content") or "")) for item in messages]
        for chunk in self.llm.stream(converted, **kwargs):
            content = getattr(chunk, "content", "")
            if content:
                yield str(content)


# ========== 工厂类 ==========

class LLMAdapterFactory:
    """LLM 适配器工厂"""

    @staticmethod
    def create(config: Dict[str, Any]) -> BaseLLMAdapter:
        """
        创建 LLM 适配器

        Args:
            config: 配置信息，需要包含 engine 字段

        Returns:
            LLM 适配器实例
        """
        engine = config.get("engine", "direct")
        if str(config.get("provider") or "").strip().lower() == "offline_replay":
            return ReplayLLMAdapter(config)
        if engine == "direct":
            return DirectLLMAdapter(config)
        if engine == "langchain":
            return LangChainLLMAdapter(config)
        if engine == "langgraph":
            return LangGraphLLMAdapter(config)
        raise ValueError(f"Unknown engine: {engine}")

    @staticmethod
    def create_from_json_file(path: str) -> BaseLLMAdapter:
        """从 JSON 配置文件创建"""
        import json
        with open(path, 'r') as f:
            config = json.load(f)
        return LLMAdapterFactory.create(config)
