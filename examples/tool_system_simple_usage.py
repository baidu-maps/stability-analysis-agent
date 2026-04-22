#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tool System 使用示例
"""

import os
import json
from stability_analyzer_agent.tool_system import (
    ToolAndWorkflowRegistry,
    SystemConfig, LLMConfig, ToolConfig, WorkflowConfig,
    ConfigDrivenExecutor,
    DirectLLMAdapter,
    register_all_tools_and_workflows,
    Priority
)

# 示例崩溃日志
SAMPLE_CRASH_LOG = """
Exception Type:  SIGSEGV
Exception Codes: SEGV_MAPERR at 0x00000000
Crashed Thread:  0

Thread 0 Crashed:
0   libobjc.A.dylib  0x00007fff6e52e1c7 objc_msgSend + 23
1   MyApp  0x0000000102345abc -[ViewController viewDidLoad] + 88
2   MyApp  0x0000000102345678 main + 44
"""


def main():
    # 1. 创建注册表并注册内置工具/工作流
    print("=" * 50)
    print("Step 1: 创建注册表并注册内置")
    print("=" * 50)

    registry = ToolAndWorkflowRegistry()
    register_all_tools_and_workflows(registry)

    print(f"已注册工具: {registry.list_all_tools()}")
    print(f"已注册工作流: {registry.list_all_workflows()}")

    # 2. 创建配置
    print("\n" + "=" * 50)
    print("Step 2: 创建系统配置")
    print("=" * 50)

    # 从环境变量获取 API Key（或使用默认值）
    api_key = os.environ.get("WENXIN_API_KEY", "test-api-key")

    config = SystemConfig(
        tools=[
            ToolConfig(name="crash_log_parser", enabled=True),
            ToolConfig(name="add2line_resolver", enabled=True),
            ToolConfig(name="code_content_provider", enabled=True),
        ],
        workflows=[
            WorkflowConfig(name="ios_crash_analyze", enabled=True),
            WorkflowConfig(name="crash_analysis", enabled=True),
        ],
        llm=LLMConfig(
            engine="direct",
            provider="openai",  # 或 "glm"
            model="glm-4",
            api_key=api_key,
            base_url="https://open.bigmodel.cn/api/paas/v4"
        )
    )

    print(f"配置: engine={config.llm.engine}, model={config.llm.model}")

    # 3. 创建执行器（需要有效 API key 才能真正调用 LLM）
    print("\n" + "=" * 50)
    print("Step 3: 创建执行器")
    print("=" * 50)

    # 注意：这里使用模拟的 API key，实际使用需要真实 key
    llm_config = config.llm.to_dict()
    llm_adapter = None

    # 尝试创建 LLM 适配器（如果 API key 有效）
    if api_key != "test-api-key":
        from stability_analyzer_agent.tool_system import LLMAdapterFactory
        llm_adapter = LLMAdapterFactory.create(llm_config)
        print(f"LLM 适配器创建成功: {llm_adapter}")
    else:
        print("⚠️ 使用测试 API key，跳过 LLM 初始化")

    executor = ConfigDrivenExecutor(registry, config, llm_adapter)
    print(f"执行器创建成功")
    print(f"活跃工具: {executor.list_active()['tools']}")
    print(f"活跃工作流: {executor.list_active()['workflows']}")

    # 4. 执行工具示例
    print("\n" + "=" * 50)
    print("Step 4: 执行工具示例")
    print("=" * 50)

    try:
        # 执行 crash_log_parser 工具
        tool_result = executor.execute_tool("crash_log_parser", {
            "log_content": SAMPLE_CRASH_LOG
        })
        print(f"crash_log_parser 结果 (部分): {json.dumps(tool_result, ensure_ascii=False)[:500]}...")
    except Exception as e:
        print(f"crash_log_parser 执行失败: {e}")

    # 5. 展示如何扩展自定义工具/工作流
    print("\n" + "=" * 50)
    print("Step 5: 扩展示例")
    print("=" * 50)

    # 定义自定义工具
    from stability_analyzer_agent.tool_system import BaseTool, ToolDefinition

    class MyCustomTool(BaseTool):
        """自定义工具示例"""

        @property
        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                name="my_custom_tool",
                description="这是一个自定义工具示例",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                output_schema={"type": "object", "properties": {"output": {"type": "string"}}},
                category="custom"
            )

        def execute(self, input_data):
            return {"output": f"处理: {input_data.get('input', '')}"}

    # 注册自定义工具（优先级更高，会覆盖内置）
    registry.register(
        "my_custom_tool",
        MyCustomTool(),
        priority=Priority.CUSTOM,
        force_override=True,
        is_tool=True,
        module="custom"
    )

    print(f"注册自定义工具后: {registry.list_all_tools()}")
    print(f"覆盖历史: {registry.list_overrides()}")

    # 6. 展示配置文件驱动的使用
    print("\n" + "=" * 50)
    print("Step 6: 配置文件驱动示例")
    print("=" * 50)

    # 可以将配置保存为 JSON 文件
    config_json = {
        "tools": [
            {"name": "crash_log_parser", "enabled": True},
            {"name": "add2line_resolver", "enabled": True},
        ],
        "workflows": [
            {"name": "ios_crash_analyze", "enabled": True},
        ],
        "llm": {
            "engine": "direct",
            "provider": "openai",
            "model": "glm-4"
        }
    }

    print("配置 JSON:")
    print(json.dumps(config_json, indent=2, ensure_ascii=False))

    print("\n" + "=" * 50)
    print("完成！")
    print("=" * 50)


if __name__ == "__main__":
    main()