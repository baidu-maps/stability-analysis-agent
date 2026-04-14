#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置系统 - 配置驱动的执行
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolConfig:
    """工具配置"""
    name: str
    enabled: bool = True
    implementation: Optional[str] = None  # 指定使用哪个实现类
    params: Dict[str, Any] = field(default_factory=dict)  # 初始化参数


@dataclass
class SkillConfig:
    """技能配置"""
    name: str
    enabled: bool = True
    implementation: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfig:
    """LLM 配置"""
    engine: str = "direct"  # direct / langchain / langgraph
    provider: str = "openai"  # openai / deepseek / wenxin
    model: str = "glm-4"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout: int = 120
    temperature: float = 0.7
    max_tokens: int = 4096
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "engine": self.engine,
            "provider": self.provider,
            "model": self.model,
            "timeout": self.timeout,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_key:
            result["api_key"] = self.api_key
        if self.base_url:
            result["base_url"] = self.base_url
        result.update(self.extra)
        return result


@dataclass
class SystemConfig:
    """系统配置"""
    tools: List[ToolConfig] = field(default_factory=list)
    skills: List[SkillConfig] = field(default_factory=list)
    llm: Optional[LLMConfig] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemConfig":
        """从字典创建"""
        tools = [ToolConfig(**t) for t in data.get("tools", [])]
        skills = [SkillConfig(**s) for s in data.get("skills", [])]

        llm_data = data.get("llm")
        llm = LLMConfig(**llm_data) if llm_data else None

        return cls(
            tools=tools,
            skills=skills,
            llm=llm,
            metadata=data.get("metadata", {})
        )

    @classmethod
    def from_file(cls, path: str) -> "SystemConfig":
        """从 JSON 文件加载"""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "tools": [
                {"name": t.name, "enabled": t.enabled, "implementation": t.implementation, "params": t.params}
                for t in self.tools
            ],
            "skills": [
                {"name": s.name, "enabled": s.enabled, "implementation": s.implementation, "params": s.params}
                for s in self.skills
            ],
            "llm": self.llm.to_dict() if self.llm else None,
            "metadata": self.metadata
        }

    def to_json(self, path: str):
        """保存为 JSON 文件"""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def get_tool_config(self, name: str) -> Optional[ToolConfig]:
        """获取工具配置"""
        return next((t for t in self.tools if t.name == name), None)

    def get_skill_config(self, name: str) -> Optional[SkillConfig]:
        """获取技能配置"""
        return next((s for s in self.skills if s.name == name), None)

    def get_enabled_tools(self) -> List[ToolConfig]:
        """获取启用的工具"""
        return [t for t in self.tools if t.enabled]

    def get_enabled_skills(self) -> List[SkillConfig]:
        """获取启用的技能"""
        return [s for s in self.skills if s.enabled]


class ConfigLoader:
    """配置加载器"""

    @staticmethod
    def load_default() -> SystemConfig:
        """加载默认配置"""
        # 查找默认配置文件
        possible_paths = [
            Path(__file__).parent.parent.parent / "configs" / "default_config.json",
            Path.cwd() / "config" / "default_config.json",
            Path.home() / ".stability_analyzer_agent" / "config.json",
        ]

        for path in possible_paths:
            if path.exists():
                logger.info(f"Loading config from: {path}")
                return SystemConfig.from_file(str(path))

        # 没有找到配置文件，返回空配置
        logger.warning("No config file found, using empty config")
        return SystemConfig()

    @staticmethod
    def load_from_env() -> SystemConfig:
        """从环境变量加载配置"""
        config_data = {}

        # LLM 配置
        if api_key := __import__("os").getenv("OPENAI_API_KEY"):
            config_data["llm"] = {
                "engine": "direct",
                "provider": "openai",
                "model": "gpt-4",
                "api_key": api_key,
            }
        elif api_key := __import__("os").getenv("DEEPSEEK_API_KEY"):
            config_data["llm"] = {
                "engine": "direct",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": api_key,
            }
        elif api_key := __import__("os").getenv("WENXIN_API_KEY"):
            config_data["llm"] = {
                "engine": "direct",
                "provider": "wenxin",
                "model": "ernie-4.0-8k",
                "api_key": api_key,
            }

        return SystemConfig.from_dict(config_data)


# 默认配置生成器
def create_default_config() -> SystemConfig:
    """创建默认配置"""
    return SystemConfig(
        tools=[
            ToolConfig(name="crash_log_parser", enabled=True),
            ToolConfig(name="add2line_resolver", enabled=True),
            ToolConfig(name="code_content_provider", enabled=True),
            ToolConfig(name="code_context_provider", enabled=True),  # 别名
        ],
        skills=[
            SkillConfig(name="ios_crash_analyze", enabled=True),
            SkillConfig(name="android_crash_analyze", enabled=True),
            SkillConfig(name="crash_analysis", enabled=True),
        ],
        llm=LLMConfig(
            engine="direct",
            provider="openai",
            model="glm-4"
        )
    )