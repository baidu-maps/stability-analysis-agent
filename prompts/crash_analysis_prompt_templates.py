#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
崩溃分析提示词模板
针对新的 JSON 格式生成大模型提示词
"""

def generate_crash_analysis_prompt(crash_data: dict) -> str:
    """
    生成崩溃分析提示词
    
    Args:
        crash_data: 包含崩溃分析数据的字典
        
    Returns:
        str: 格式化的提示词
    """
    prompt_parts = []
    
    # 1. 任务指令
    prompt_parts.append("# 崩溃修复任务")
    prompt_parts.append("基于提供的实际源代码，分析崩溃原因并提供可直接应用的修复代码。")
    prompt_parts.append("")
    
    # 2. 崩溃信息
    if "crash_summary" in crash_data:
        summary = crash_data["crash_summary"]
        prompt_parts.append("## 崩溃信息")
        prompt_parts.append(f"**文件**: {summary.get('file', 'unknown')}")
        prompt_parts.append(f"**函数**: {summary.get('function', 'unknown')}")
        prompt_parts.append(f"**行号**: {summary.get('line', 'unknown')}")
        prompt_parts.append(f"**地址**: {summary.get('stack_address', 'unknown')}")
        prompt_parts.append(f"**错误类型**: {summary.get('error_type', 'unknown')}")
        prompt_parts.append(f"**线程ID**: {summary.get('thread_id', 'unknown')}")
        prompt_parts.append("")
    
    # 3. 崩溃函数代码
    if "crash_func" in crash_data:
        crash_func = crash_data["crash_func"]
        prompt_parts.append("## 崩溃函数代码")
        prompt_parts.append(f"**函数名**: {crash_func.get('name', 'unknown')}")
        prompt_parts.append(f"**函数签名**: {crash_func.get('signature', 'unknown')}")
        prompt_parts.append("")
        prompt_parts.append("```cpp")
        for line in crash_func.get('snippet', []):
            prompt_parts.append(line)
        prompt_parts.append("```")
        prompt_parts.append("")
        prompt_parts.append(f"**崩溃行**: `{crash_func.get('crash_line', 'unknown')}`")
        prompt_parts.append("")
    
    # 4. 调用链函数
    if "call_chain_fun" in crash_data and crash_data["call_chain_fun"]:
        prompt_parts.append("## 调用链函数")
        prompt_parts.append("以下函数调用了崩溃函数，请检查它们的线程安全性：")
        prompt_parts.append("")
        
        for i, func in enumerate(crash_data["call_chain_fun"], 1):
            prompt_parts.append(f"### {i}. {func.get('name', 'unknown')}")
            prompt_parts.append(f"**文件**: {func.get('file', 'unknown')}")
            prompt_parts.append("")
            prompt_parts.append("```cpp")
            for line in func.get('snippet', []):
                prompt_parts.append(line)
            prompt_parts.append("```")
            prompt_parts.append("")
    
    # 5. 变量相关函数
    if "var_call_fun" in crash_data and crash_data["var_call_fun"]:
        prompt_parts.append("## 变量相关函数")
        prompt_parts.append("以下函数使用了崩溃行涉及的变量，请检查它们的线程安全性：")
        prompt_parts.append("")
        
        for i, func in enumerate(crash_data["var_call_fun"], 1):
            prompt_parts.append(f"### {i}. {func.get('name', 'unknown')}")
            prompt_parts.append(f"**变量**: {func.get('variable', 'unknown')}")
            prompt_parts.append(f"**关系**: {func.get('relation', 'unknown')}")
            prompt_parts.append(f"**文件**: {func.get('file', 'unknown')}")
            prompt_parts.append("")
            prompt_parts.append("```cpp")
            for line in func.get('snippet', []):
                prompt_parts.append(line)
            prompt_parts.append("```")
            prompt_parts.append("")
    
    # 6. 线程上下文
    if "thread_context" in crash_data and crash_data["thread_context"]:
        prompt_parts.append("## 线程上下文")
        prompt_parts.append("以下是与崩溃相关的线程信息：")
        prompt_parts.append("")
        
        for i, thread in enumerate(crash_data["thread_context"], 1):
            prompt_parts.append(f"### 线程 {i}")
            prompt_parts.append(f"**线程ID**: {thread.get('thread_id', 'unknown')}")
            prompt_parts.append(f"**调用链**: {' -> '.join(thread.get('call_chain', []))}")
            
            if thread.get('shared_vars'):
                prompt_parts.append(f"**共享变量**: {', '.join(thread['shared_vars'])}")
            
            if thread.get('sync_primitives'):
                prompt_parts.append(f"**同步原语**: {', '.join(thread['sync_primitives'])}")
            
            prompt_parts.append("")
    
    # 7. 分析指导
    prompt_parts.append("## 分析指导")
    prompt_parts.append("**重要**：请进行全面的崩溃原因分析，不要局限于表面现象！")
    prompt_parts.append("")
    prompt_parts.append("**分析步骤**：")
    prompt_parts.append("1. 分析崩溃点的直接原因（空指针、越界等）")
    prompt_parts.append("2. 分析为什么会出现这种直接原因")
    prompt_parts.append("3. 检查是否有其他线程或函数可能影响崩溃点的数据")
    prompt_parts.append("4. 分析所有相关函数的线程安全性")
    prompt_parts.append("5. 识别多线程竞争、内存管理、资源竞争等潜在问题")
    prompt_parts.append("6. 提出针对根本原因的修复方案")
    prompt_parts.append("")
    
    # 8. 多线程问题检查要点
    prompt_parts.append("**多线程问题检查要点**：")
    prompt_parts.append("- 检查所有访问共享数据的函数是否有锁保护")
    prompt_parts.append("- 检查函数调用链路中是否有线程安全问题")
    prompt_parts.append("- 检查是否有数据竞争、内存竞争等问题")
    prompt_parts.append("- 检查是否有释放后使用、双重释放等问题")
    prompt_parts.append("")
    
    # 9. 输出要求
    prompt_parts.append("## 输出要求")
    prompt_parts.append("**必须提供**：")
    prompt_parts.append("1. 崩溃原因分析（直接原因和根本原因）")
    prompt_parts.append("2. 需要修改的函数列表（包括崩溃函数和相关函数）")
    prompt_parts.append("3. 每个函数的完整修复代码")
    prompt_parts.append("4. 修改说明和验证方法")
    prompt_parts.append("")
    
    # 10. 输出格式
    prompt_parts.append("## 输出格式")
    prompt_parts.append("```")
    prompt_parts.append("### 结论（崩溃定位与根因）")
    prompt_parts.append("- 直接原因：[具体原因]")
    prompt_parts.append("- 根本原因：[根本原因]")
    prompt_parts.append("- 位置：[文件:行号]")
    prompt_parts.append("")
    prompt_parts.append("#### 关键证据（引用堆栈/代码）")
    prompt_parts.append("- 证据1：[栈帧/文件:行号/代码语句]")
    prompt_parts.append("- 证据2：[栈帧/文件:行号/代码语句]")
    prompt_parts.append("")
    prompt_parts.append("### 修复方案")
    prompt_parts.append("#### 需要修改的函数")
    prompt_parts.append("- [函数名1] - [修改原因]")
    prompt_parts.append("- [函数名2] - [修改原因]")
    prompt_parts.append("")
    prompt_parts.append("#### 修复代码")
    prompt_parts.append("```cpp")
    prompt_parts.append("// [函数名1] - 完整修复代码")
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append("```cpp")
    prompt_parts.append("// [函数名2] - 完整修复代码")
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append("#### 说明与验证")
    prompt_parts.append("- 说明：[为什么这样修改]")
    prompt_parts.append("- 验证：[如何测试]")
    prompt_parts.append("```")
    prompt_parts.append("")
    
    # 11. 关键约束
    prompt_parts.append("## 关键约束")
    prompt_parts.append("- 必须基于实际源代码进行修复")
    prompt_parts.append("- 必须分析所有相关函数的线程安全性")
    prompt_parts.append("- 修复代码必须完整且可编译")
    prompt_parts.append("- 重点检查线程函数的锁保护情况")
    prompt_parts.append("- 禁止使用'未知'、'假设'、'示例'等词汇")
    prompt_parts.append("")
    
    return '\n'.join(prompt_parts)

def generate_crash_repair_prompt(crash_data: dict) -> str:
    """
    生成崩溃修复提示词（简化版本）
    
    Args:
        crash_data: 包含崩溃分析数据的字典
        
    Returns:
        str: 格式化的修复提示词
    """
    prompt_parts = []
    
    # 1. 任务指令
    prompt_parts.append("# 崩溃修复任务")
    prompt_parts.append("基于提供的实际源代码，分析崩溃原因并提供可直接应用的修复代码。")
    prompt_parts.append("")
    
    # 2. 崩溃信息
    if "crash_summary" in crash_data:
        summary = crash_data["crash_summary"]
        prompt_parts.append("## 崩溃信息")
        prompt_parts.append(f"**函数**: {summary.get('function', 'unknown')}")
        prompt_parts.append(f"**位置**: {summary.get('file', 'unknown')}:{summary.get('line', 'unknown')}")
        prompt_parts.append("")
    
    # 3. 源代码
    if "crash_func" in crash_data:
        crash_func = crash_data["crash_func"]
        prompt_parts.append("## 源代码")
        prompt_parts.append("```cpp")
        for line in crash_func.get('snippet', []):
            prompt_parts.append(line)
        prompt_parts.append("```")
        prompt_parts.append("")
        prompt_parts.append(f"**崩溃行**: `{crash_func.get('crash_line', 'unknown')}`")
        prompt_parts.append("")
    
    # 4. 相关函数
    if "call_chain_fun" in crash_data and crash_data["call_chain_fun"]:
        prompt_parts.append("## 相关函数")
        for func in crash_data["call_chain_fun"]:
            prompt_parts.append(f"### {func.get('name', 'unknown')}")
            prompt_parts.append("```cpp")
            for line in func.get('snippet', []):
                prompt_parts.append(line)
            prompt_parts.append("```")
            prompt_parts.append("")
    
    # 5. 分析要求
    prompt_parts.append("## 分析要求")
    prompt_parts.append("**必须检查**：")
    prompt_parts.append("1. 崩溃函数是否有锁保护")
    prompt_parts.append("2. 其他访问相同数据的函数是否有锁保护")
    prompt_parts.append("3. 线程函数（如*_thread、*_handler）的锁使用情况")
    prompt_parts.append("4. 是否存在多线程竞争导致的数据不一致")
    prompt_parts.append("")
    
    # 6. 输出格式
    prompt_parts.append("## 输出格式")
    prompt_parts.append("```cpp")
    prompt_parts.append("// 修复后的完整代码")
    prompt_parts.append("[基于实际代码的修复实现]")
    prompt_parts.append("```")
    prompt_parts.append("")
    
    # 7. 关键约束
    prompt_parts.append("## 关键约束")
    prompt_parts.append("- 必须基于实际源代码进行修复")
    prompt_parts.append("- 修复代码必须完整且可编译")
    prompt_parts.append("- 保持原有函数签名和接口")
    prompt_parts.append("- 重点检查线程函数的锁保护情况")
    prompt_parts.append("")
    
    return '\n'.join(prompt_parts)

def generate_thread_safety_prompt(crash_data: dict) -> str:
    """
    生成线程安全分析提示词
    
    Args:
        crash_data: 包含崩溃分析数据的字典
        
    Returns:
        str: 格式化的线程安全分析提示词
    """
    prompt_parts = []
    
    # 1. 任务指令
    prompt_parts.append("# 线程安全分析任务")
    prompt_parts.append("分析多线程环境下的崩溃问题，重点检查线程安全性和数据竞争。")
    prompt_parts.append("")
    
    # 2. 崩溃信息
    if "crash_summary" in crash_data:
        summary = crash_data["crash_summary"]
        prompt_parts.append("## 崩溃信息")
        prompt_parts.append(f"**函数**: {summary.get('function', 'unknown')}")
        prompt_parts.append(f"**位置**: {summary.get('file', 'unknown')}:{summary.get('line', 'unknown')}")
        prompt_parts.append(f"**线程ID**: {summary.get('thread_id', 'unknown')}")
        prompt_parts.append("")
    
    # 3. 线程上下文
    if "thread_context" in crash_data and crash_data["thread_context"]:
        prompt_parts.append("## 线程上下文")
        for i, thread in enumerate(crash_data["thread_context"], 1):
            prompt_parts.append(f"### 线程 {i}")
            prompt_parts.append(f"**线程ID**: {thread.get('thread_id', 'unknown')}")
            prompt_parts.append(f"**调用链**: {' -> '.join(thread.get('call_chain', []))}")
            
            if thread.get('shared_vars'):
                prompt_parts.append(f"**共享变量**: {', '.join(thread['shared_vars'])}")
            
            if thread.get('sync_primitives'):
                prompt_parts.append(f"**同步原语**: {', '.join(thread['sync_primitives'])}")
            
            prompt_parts.append("")
    
    # 4. 源代码分析
    if "crash_func" in crash_data:
        crash_func = crash_data["crash_func"]
        prompt_parts.append("## 崩溃函数代码")
        prompt_parts.append("```cpp")
        for line in crash_func.get('snippet', []):
            prompt_parts.append(line)
        prompt_parts.append("```")
        prompt_parts.append("")
    
    # 5. 相关函数分析
    if "call_chain_fun" in crash_data and crash_data["call_chain_fun"]:
        prompt_parts.append("## 调用链函数")
        for func in crash_data["call_chain_fun"]:
            prompt_parts.append(f"### {func.get('name', 'unknown')}")
            prompt_parts.append("```cpp")
            for line in func.get('snippet', []):
                prompt_parts.append(line)
            prompt_parts.append("```")
            prompt_parts.append("")
    
    # 6. 变量相关函数
    if "var_call_fun" in crash_data and crash_data["var_call_fun"]:
        prompt_parts.append("## 变量相关函数")
        for func in crash_data["var_call_fun"]:
            prompt_parts.append(f"### {func.get('name', 'unknown')}")
            prompt_parts.append(f"**变量**: {func.get('variable', 'unknown')} ({func.get('relation', 'unknown')})")
            prompt_parts.append("```cpp")
            for line in func.get('snippet', []):
                prompt_parts.append(line)
            prompt_parts.append("```")
            prompt_parts.append("")
    
    # 7. 线程安全分析要点
    prompt_parts.append("## 线程安全分析要点")
    prompt_parts.append("**必须检查**：")
    prompt_parts.append("1. 所有访问共享数据的函数是否有锁保护")
    prompt_parts.append("2. 锁的使用是否正确（加锁和解锁配对）")
    prompt_parts.append("3. 是否存在死锁风险")
    prompt_parts.append("4. 是否存在数据竞争")
    prompt_parts.append("5. 内存管理是否安全（避免释放后使用）")
    prompt_parts.append("6. 原子操作的使用是否恰当")
    prompt_parts.append("")
    
    # 8. 输出要求
    prompt_parts.append("## 输出要求")
    prompt_parts.append("**必须提供**：")
    prompt_parts.append("1. 线程安全问题分析")
    prompt_parts.append("2. 数据竞争分析")
    prompt_parts.append("3. 修复方案（包括锁保护）")
    prompt_parts.append("4. 完整的修复代码")
    prompt_parts.append("")
    
    return '\n'.join(prompt_parts)

# 测试代码
if __name__ == "__main__":
    # 示例崩溃数据
    sample_crash_data = {
        "crash_summary": {
            "file": "src/data/complex_data.cpp",
            "function": "ComplexDataStructure::add_node",
            "line": 128,
            "stack_address": "0x102d126fc",
            "error_type": "SIGSEGV",
            "thread_id": "0x700005f94000"
        },
        "crash_func": {
            "name": "ComplexDataStructure::add_node",
            "signature": "void add_node(int id, size_t value)",
            "snippet": [
                "void ComplexDataStructure::add_node(int id, size_t value) {",
                "    Node* n = new Node(id, value);",
                "    nodes.push_back(n);   // <-- crash line",
                "}"
            ],
            "crash_line": "nodes.push_back(n);"
        },
        "call_chain_fun": [
            {
                "name": "worker_thread",
                "file": "src/thread/worker.cpp",
                "snippet": [
                    "void worker_thread(int tid) {",
                    "    ComplexDataStructure cds;",
                    "    cds.add_node(tid, tid * 100);",
                    "}"
                ]
            }
        ],
        "var_call_fun": [
            {
                "variable": "nodes",
                "relation": "write",
                "name": "ComplexDataStructure::remove_node",
                "file": "src/data/complex_data.cpp",
                "snippet": [
                    "void ComplexDataStructure::remove_node(int id) {",
                    "    auto it = std::find_if(...);",
                    "    if (it != nodes.end()) nodes.erase(it);",
                    "}"
                ]
            }
        ],
        "thread_context": [
            {
                "thread_id": "0x700005f94000",
                "call_chain_from_add2line": [
                    "ComplexDataStructure::add_node",
                    "worker_thread",
                    "std::__thread_execute"
                ]
            }
        ]
    }
    
    # 生成不同类型的提示词
    print("=== 完整崩溃分析提示词 ===")
    print(generate_crash_analysis_prompt(sample_crash_data))
    print("\n" + "="*50 + "\n")
    
    print("=== 简化修复提示词 ===")
    print(generate_crash_repair_prompt(sample_crash_data))
    print("\n" + "="*50 + "\n")
    
    print("=== 线程安全分析提示词 ===")
    print(generate_thread_safety_prompt(sample_crash_data))
