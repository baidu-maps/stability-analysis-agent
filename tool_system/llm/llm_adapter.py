#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 适配器接口定义 - 支持多种调用方式
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Generator, Union

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


# ========== 三种实现 ==========

class DirectLLMAdapter(BaseLLMAdapter):
    """
    Direct 方式：直接拼装提示词，一次调用
    适用于简单固定流程
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
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
                self.client = OpenAI(api_key=api_key, base_url=base_url)
                logger.info(f"Initialized {provider} client: {base_url}")
            except ImportError:
                logger.warning("openai package not installed, DirectLLMAdapter will not work")
                self.client = None
        elif provider == "deepseek":
            try:
                from openai import OpenAI
                self.client = OpenAI(
                    api_key=config.get("deepseek_api_key"),
                    base_url=config.get("deepseek_base_url", "https://api.deepseek.com/v1")
                )
            except ImportError:
                self.client = None
        else:
            logger.warning(f"Unknown provider: {provider}")
            self.client = None

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        if self.client is None:
            raise RuntimeError("LLM client not initialized")

        # 构建请求参数
        request_params = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        # 如果有工具定义但不使用（Direct 模式），仅作为参考
        # Direct 模式通常直接把工具能力体现在 prompt 中

        try:
            response = self.client.chat.completions.create(**request_params)
            content = response.choices[0].message.content or ""

            return LLMResponse(
                content=content,
                usage={
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                },
                metadata={"provider": self.config.get("provider", "openai")}
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise RuntimeError(f"LLM call failed: {e}")

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
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
    """
    LangChain 方式：使用 LangChain Agent
    适用于需要灵活工具调用的场景
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._init_langchain(config)

    def _init_langchain(self, config: Dict[str, Any]):
        """初始化 LangChain"""
        self.agent_executor = None
        self._langgraph_available = False

        try:
            from langchain_openai import ChatOpenAI
            from langchain.agents import AgentExecutor, create_openai_functions_agent
            from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
            from langchain.schema import HumanMessage, SystemMessage

            # 初始化 LLM
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

            logger.info("LangChain initialized successfully")
            self._langgraph_available = True

        except ImportError as e:
            logger.warning(f"LangChain not available: {e}")
            self.llm = None

    def set_agent_executor(self, agent_executor: Any, tools: List[Any] = None):
        """设置 Agent Executor（外部注入）"""
        self.agent_executor = agent_executor
        self.tools = tools or []

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        if self.agent_executor is None:
            raise RuntimeError("AgentExecutor not set. Call set_agent_executor() first.")

        # 提取最后一条 user 消息作为 input
        user_input = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

        try:
            result = self.agent_executor.invoke({"input": user_input})
            output = result.get("output", "")

            return LLMResponse(
                content=output,
                metadata={"method": "langchain_agent"}
            )
        except Exception as e:
            logger.error(f"LangChain agent failed: {e}")
            raise RuntimeError(f"LangChain agent failed: {e}")

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
        # LangChain Agent 不支持真正的流式，返回空
        # 可以考虑实现假流式
        result = self.chat(messages, tools, **kwargs)
        yield result.content


class LangGraphLLMAdapter(BaseLLMAdapter):
    """
    LangGraph 方式：使用 LangGraph 图结构
    适用于需要复杂流程控制的场景
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._init_langgraph(config)

    def _init_langgraph(self, config: Dict[str, Any]):
        """初始化 LangGraph"""
        self.graph_app = None
        self.checkpointer = None

        try:
            from langgraph.graph import StateGraph, END
            from langgraph.graph.message import add_messages
            from langgraph.checkpoint.memory import MemorySaver

            # 初始化 LLM
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

            # 初始化 checkpointer
            self.checkpointer = MemorySaver()

            logger.info("LangGraph initialized successfully")
            self._langgraph_available = True

        except ImportError as e:
            logger.warning(f"LangGraph not available: {e}")
            self.llm = None

    def set_graph_app(self, graph_app: Any):
        """设置 Graph App（外部注入）"""
        self.graph_app = graph_app

    def chat(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> LLMResponse:
        if self.graph_app is None:
            # 如果没有设置 graph_app，使用简单的 LLM 调用
            if self.llm is None:
                raise RuntimeError("Neither graph_app nor LLM available")

            # 简单调用 LLM
            from langchain.schema import HumanMessage
            langchain_messages = [HumanMessage(content=m["content"]) for m in messages if m["role"] != "system"]
            response = self.llm.invoke(langchain_messages)

            return LLMResponse(
                content=response.content,
                metadata={"method": "langgraph_fallback"}
            )

        # 使用 LangGraph 图执行
        try:
            config = {"configurable": {"thread_id": kwargs.get("thread_id", "default")}}
            result = self.graph_app.invoke(
                {"messages": messages},
                config
            )

            # 提取最后一条消息
            last_message = result.get("messages", [])[-1] if result.get("messages") else None
            content = last_message.content if hasattr(last_message, "content") else str(last_message)

            return LLMResponse(
                content=content,
                metadata={"method": "langgraph"}
            )
        except Exception as e:
            logger.error(f"LangGraph execution failed: {e}")
            raise RuntimeError(f"LangGraph execution failed: {e}")

    def stream(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None, **kwargs) -> Generator[str, None, None]:
        if self.graph_app is None:
            # 回退到简单流式
            result = self.chat(messages, tools, **kwargs)
            yield result.content
            return

        try:
            config = {"configurable": {"thread_id": kwargs.get("thread_id", "default")}}
            for event in self.graph_app.stream(
                {"messages": messages},
                config
            ):
                for node_name, node_output in event.items():
                    if "messages" in node_output:
                        last_msg = node_output["messages"][-1]
                        if hasattr(last_msg, "content"):
                            yield last_msg.content
        except Exception as e:
            logger.error(f"LangGraph stream failed: {e}")
            raise RuntimeError(f"LangGraph stream failed: {e}")


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
        engine = config.get("engine", "direct")  # direct / langchain / langgraph

        if engine == "direct":
            return DirectLLMAdapter(config)
        elif engine == "langchain":
            return LangChainLLMAdapter(config)
        elif engine == "langgraph":
            return LangGraphLLMAdapter(config)
        else:
            raise ValueError(f"Unknown engine: {engine}. Supported: direct, langchain, langgraph")

    @staticmethod
    def create_from_json_file(path: str) -> BaseLLMAdapter:
        """从 JSON 配置文件创建"""
        import json
        with open(path, 'r') as f:
            config = json.load(f)
        return LLMAdapterFactory.create(config)