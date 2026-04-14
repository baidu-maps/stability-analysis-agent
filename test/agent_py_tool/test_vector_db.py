#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试向量数据库功能
"""

import sys
import json
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

def test_vector_database():
    """测试向量数据库功能"""
    try:
        print("正在测试向量数据库功能...")
        
        # 导入向量数据库模块
        from stability_analyzer_agent.rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB
        
        # 创建分析器实例
        analyzer = AIStabilityAnalyzerWithVectorDB()
        print("✅ 向量数据库初始化成功")
        
        # 添加示例 pattern + evidence
        sample_pattern = {
            "pattern_id": "pattern_test_nullptr",
            "pattern_summary": "空指针访问导致 SIGSEGV，常见于未检查指针的访问路径",
            "crash_signature": "SIGSEGV + null pointer + deref",
            "platform_scope": {"os": "Android"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "log_fragment"],
            "confidence_score": 0.75,
            "validation_state": "verified",
            "source_type": "internal_case",
            "created_at": "2025-01-01T00:00:00",
        }
        sample_evidence = {
            "evidence_id": "evidence_test_001",
            "pattern_id": "pattern_test_nullptr",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps({"address": "0x1234", "function": "main"}, ensure_ascii=False),
            "normalized_features": {"function": "main"},
            "reliability_score": 0.7,
            "created_at": "2025-01-01T00:00:00",
        }
        
        success = analyzer.add_pattern(sample_pattern)
        
        if success:
            analyzer.add_evidence(sample_evidence)
            print("✅ 示例 pattern 添加成功")
        else:
            print("❌ 示例 pattern 添加失败")
        
        # 测试搜索功能
        print("\n正在测试搜索功能...")
        
        # 搜索相似模式
        pattern_hits = analyzer.retrieve_patterns("SIGSEGV null pointer", n_results=3)
        print(f"✅ 搜索到 {len(pattern_hits)} 个相似模式")
        
        # 获取数据库统计信息
        stats = analyzer.get_database_statistics()
        print(f"\n数据库统计信息:")
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        
        print("\n🎉 向量数据库功能测试完成！")
        return True
        
    except ImportError as e:
        print(f"❌ 导入失败: {e}")
        print("请安装向量数据库依赖: pip install -r requirements_vector_db.txt")
        return False
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False

def test_ai_agent_integration():
    """测试AI Agent与向量数据库的集成"""
    try:
        print("\n正在测试AI Agent与向量数据库的集成...")
        
        # 导入AI Agent
        from stability_analyzer_agent.agent.ai_stability_agent import FullStabilityAnalyzer
        
        # 创建AI Agent实例
        agent = FullStabilityAnalyzer()
        print("✅ AI Agent初始化成功")
        
        # 检查向量数据库是否可用
        if agent.vector_db_analyzer:
            print("✅ 向量数据库集成成功")
            
            # 获取统计信息
            stats = agent.get_vector_db_statistics()
            print(f"向量数据库统计: {stats}")
        else:
            print("⚠️ 向量数据库未初始化")
        
        # 测试咨询功能（包含向量数据库搜索）
        print("\n正在测试咨询功能...")
        response = agent.perform_consultation("如何避免空指针崩溃？")
        print(f"咨询回复: {response[:200]}...")
        
        print("✅ AI Agent集成测试完成！")
        return True
        
    except Exception as e:
        print(f"❌ AI Agent集成测试失败: {e}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("Stability Analysis Agent 向量数据库功能测试")
    print("=" * 60)
    
    # 测试向量数据库基本功能
    success1 = test_vector_database()
    
    # 测试AI Agent集成
    success2 = test_ai_agent_integration()
    
    print("\n" + "=" * 60)
    if success1 and success2:
        print("🎉 所有测试通过！向量数据库功能正常")
    else:
        print("❌ 部分测试失败，请检查错误信息")
    print("=" * 60)
