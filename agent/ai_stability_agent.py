#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stability Analysis Agent - 完整 Agent 版本（基于 LangGraph）
支持AI分析，专门用于VSCode插件调用
使用 LangGraph 图结构执行引擎管理工具链
"""

import json
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, TypedDict, Annotated, List
from dataclasses import dataclass, replace

# requests 可能在受限沙箱环境下因证书读取权限失败；保持可选导入
REQUESTS_AVAILABLE = False
try:
    import requests  # type: ignore
    REQUESTS_AVAILABLE = True
except Exception as e:
    print(f"WARNING: requests导入失败: {e}", file=sys.stderr)
    requests = None  # type: ignore

# LangGraph 相关导入
LANGGRAPH_AVAILABLE = False
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError as e:
    print(f"WARNING: LangGraph导入失败: {e}", file=sys.stderr)
    print("INFO: 将使用传统顺序执行模式", file=sys.stderr)
    LANGGRAPH_AVAILABLE = False
    # 避免在类型注解中引用未定义名称导致 NameError（Python 会在定义时求值注解）
    StateGraph = Any  # type: ignore
    END = None  # type: ignore
    MemorySaver = None  # type: ignore
from tools import crash_log_parser, CrashParseOptions
from tools import add2line_resolver
from tools import CodeContentProvider
from rag.feature_extractor import extract_features, build_pattern_query

# 可选：代码索引服务（services/ 目录，不属于 OSS 核心包）
try:
    from services.code_index_service import CodeIndexMultiRoot
    _CODE_INDEX_AVAILABLE = True
except ImportError:
    _CODE_INDEX_AVAILABLE = False
    CodeIndexMultiRoot = None  # type: ignore

# 可选：报告生成服务（report/ 目录，不属于 OSS 核心包）
try:
    from report.code_modifier import CodeModifier
    from report.code_check import CodeChecker
    from report.report_generator import ReportGenerator
    _REPORT_AVAILABLE = True
except ImportError:
    _REPORT_AVAILABLE = False
    CodeModifier = None   # type: ignore
    CodeChecker = None    # type: ignore
    ReportGenerator = None  # type: ignore
# 向量数据库（可选）
try:
    from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB  # type: ignore
    VECTOR_DB_AVAILABLE = True
except Exception as e:
    VECTOR_DB_AVAILABLE = False
    AIStabilityAnalyzerWithVectorDB = None  # type: ignore
    print(f"WARNING: 向量数据库模块导入失败，将禁用RAG: {e}", file=sys.stderr)


class CrashAnalysisState(TypedDict):
    """崩溃分析状态（LangGraph State）"""
    # 输入参数
    crash_log_content: str
    code_roots: List[str]
    code_root: str  # 首个根目录（兼容旧字段）
    library_dir: str
    model: Optional[str]  # 可选：指定AI模型
    
    # 中间结果（按执行顺序）
    parsed_crash_info: Optional[str]  # JSON 字符串
    parsed_data: Optional[Dict[str, Any]]  # 解析后的数据
    resolved_stack_trace: Optional[str]  # JSON 字符串
    resolved_data: Optional[Dict[str, Any]]  # 解析后的数据
    code_context_prompt: Optional[str]  # JSON 字符串
    prompt_data: Optional[Dict[str, Any]]  # 解析后的数据
    
    # 规则/向量/元数据检索结果（可选）
    rule_hits: Optional[list]
    pattern_hits: Optional[list]
    evidence_map: Optional[Dict[str, Any]]
    strategy_hits: Optional[list]
    decision_trace: Optional[list]
    vector_used: Optional[bool]
    memory_context: Optional[str]
    
    # 最终结果
    ai_analysis: Optional[str]
    ai_result: Optional[Dict[str, Any]]
    
    # 多轮调用控制（状态机模式）
    need_more_context: bool  # 是否需要更多上下文
    context_enhancement_request: Optional[str]  # AI请求的具体上下文需求（JSON字符串）
    iteration_count: int  # 当前迭代次数
    max_iterations: int  # 最大迭代次数（防止无限循环）
    tools_called_in_enhancement: list  # 在增强上下文中已调用的工具列表
    enhanced_contexts: list  # 增强的上下文列表
    
    # 元数据
    config: Optional[Dict[str, Any]]
    execution_log: list  # 执行日志列表
    error_message: Optional[str]
    start_time: Optional[str]
    total_time: Optional[float]


@dataclass
class AnalysisStep:
    """分析步骤信息（CLI 输出兼容）。"""
    step_name: str
    input_data: Any
    output_data: Any
    execution_time: float
    status: str
    error_message: Optional[str] = None


@dataclass
class CrashAnalysisResult:
    """分析结果（兼容 CLI 旧结构）。"""
    crash_log_content: str
    parsed_crash_info: Dict[str, Any]
    resolved_stack_trace: Dict[str, Any]
    code_context_prompt: Dict[str, Any]
    ai_analysis: str
    analysis_steps: List[AnalysisStep]
    total_execution_time: float
    timestamp: str
    status: str
    rule_hits: Optional[List[Dict[str, Any]]] = None
    pattern_hits: Optional[List[Dict[str, Any]]] = None
    evidence_map: Optional[Dict[str, Any]] = None
    strategy_hits: Optional[List[Dict[str, Any]]] = None
    decision_trace: Optional[List[Dict[str, Any]]] = None
    vector_used: Optional[bool] = None
    memory_context: Optional[str] = None


class FullStabilityAnalyzer:
    """完整的崩溃分析器，包含AI分析（基于 LangGraph）"""
    
    def __init__(
        self,
        config_dict=None,
        initialize_ai_runtime: bool = True,
        initialize_vector_db: bool = True,
        code_parser_backend: Optional[str] = None,
        max_static_call_chain_depth: Optional[int] = None,
        max_direct_callers: Optional[int] = None,
        max_shared_var_related_functions: Optional[int] = None,
        max_symbol_only_rescues: Optional[int] = None,
        find_source_timeout_sec: Optional[float] = None,
        code_context_timeout_sec: Optional[float] = None,
        **_kwargs,
    ):
        self.config = config_dict or {}
        self.code_parser_backend = code_parser_backend
        self.max_static_call_chain_depth = max_static_call_chain_depth
        self.max_direct_callers = max_direct_callers
        self.max_shared_var_related_functions = max_shared_var_related_functions
        self.max_symbol_only_rescues = max_symbol_only_rescues
        self.find_source_timeout_sec = find_source_timeout_sec
        self.code_context_timeout_sec = code_context_timeout_sec
        self.llm_config = self.config.get('llm_config', {})
        self.active_provider = self.llm_config.get('active_provider', 'baidu_qianfan')
        self.default_model = self.llm_config.get('default_model', 'ernie-4.5-turbo-128k')
        workflow_config = self.config.get("workflow_config", {}) if isinstance(self.config, dict) else {}
        self.disable_vector_db_save = bool(workflow_config.get("disable_vector_db_save", False))
        
        # 初始化新工具（可选，仅在 report/ 模块存在时实例化）
        self.code_modifier = CodeModifier() if _REPORT_AVAILABLE else None
        self.code_checker = CodeChecker() if _REPORT_AVAILABLE else None
        self.report_generator = ReportGenerator() if _REPORT_AVAILABLE else None

        # 初始化代码索引服务（延迟初始化，需要 code_roots；仅在 services/ 模块存在时使用）
        self.code_index_service: Optional[CodeIndexMultiRoot] = None  # type: ignore
        
        # 初始化向量数据库（可选）
        self.vector_db_analyzer = None
        workflow_config = self.config.get('workflow_config', {})
        disable_vector_db = workflow_config.get('disable_vector_db', False)
        
        if disable_vector_db or not initialize_vector_db:
            print("INFO: 向量数据库已禁用（通过 --disable-vector-db 参数）", file=sys.stderr)
        elif VECTOR_DB_AVAILABLE and AIStabilityAnalyzerWithVectorDB:
            try:
                self.vector_db_analyzer = AIStabilityAnalyzerWithVectorDB()
                print("INFO: 向量数据库初始化成功", file=sys.stderr)
            except Exception as e:
                print(f"WARNING: 向量数据库初始化失败: {e}", file=sys.stderr)
        
        # 初始化 LangGraph（如果可用）
        self.graph = None
        if LANGGRAPH_AVAILABLE:
            try:
                self.graph = self._build_graph()
                print("INFO: LangGraph 图结构初始化成功", file=sys.stderr)
            except Exception as e:
                print(f"WARNING: LangGraph 初始化失败，将使用传统模式: {e}", file=sys.stderr)
                self.graph = None
        
        # 打印配置信息
        print(f"INFO: FullStabilityAnalyzer初始化完成", file=sys.stderr)
        print(f"INFO: 配置字典: {bool(self.config)}", file=sys.stderr)
        print(f"INFO: 默认提供商: {self.active_provider}", file=sys.stderr)
        print(f"INFO: 默认模型: {self.default_model}", file=sys.stderr)
        print(f"INFO: LangGraph模式: {'启用' if self.graph else '禁用（使用传统模式）'}", file=sys.stderr)
        print(f"INFO: 向量数据库: {'启用' if self.vector_db_analyzer else '禁用'}", file=sys.stderr)

    def _merge_crash_parse_options(self, crash_parse_options: Optional[CrashParseOptions], library_dir: Optional[str]) -> CrashParseOptions:
        opts = crash_parse_options or CrashParseOptions()
        if library_dir and not opts.library_dir and os.path.exists(library_dir):
            opts = replace(opts, library_dir=os.path.abspath(library_dir))
        return opts

    def _build_graph(self) -> Optional[StateGraph]:
        """构建 LangGraph 执行图（支持多轮工具调用，状态机模式）"""
        if not LANGGRAPH_AVAILABLE:
            return None
        
        try:
            # 创建状态图
            workflow = StateGraph(CrashAnalysisState)
            
            # 添加节点
            workflow.add_node("crash_log_parser", self._node_crash_log_parser)
            workflow.add_node("add2line_resolver", self._node_add2line_resolver)
            workflow.add_node("code_content_provider", self._node_code_content_provider)
            workflow.add_node("vector_db_search", self._node_vector_db_search)
            workflow.add_node("ai_analysis", self._node_ai_analysis)
            workflow.add_node("enhance_context", self._node_enhance_context)  # 新增：上下文增强节点
            
            # 定义边的连接关系（初始流程）
            workflow.set_entry_point("crash_log_parser")
            workflow.add_edge("crash_log_parser", "add2line_resolver")
            workflow.add_edge("add2line_resolver", "code_content_provider")
            workflow.add_edge("code_content_provider", "vector_db_search")
            workflow.add_edge("vector_db_search", "ai_analysis")
            
            # AI分析节点后添加条件路由：判断是否需要增强上下文
            workflow.add_conditional_edges(
                "ai_analysis",
                self._should_enhance_context,  # 条件判断函数
                {
                    "enhance": "enhance_context",  # 如果需要增强上下文，跳转到增强节点
                    "end": END  # 如果不需要，结束
                }
            )
            
            # enhance_context 节点后，根据AI需求路由到不同的工具节点
            workflow.add_conditional_edges(
                "enhance_context",
                self._route_to_tool,  # 根据AI需求路由到不同工具
                {
                    "crash_log_parser": "crash_log_parser",
                    "add2line_resolver": "add2line_resolver",
                    "code_content_provider": "code_content_provider",
                    "ai_analysis": "ai_analysis"  # 如果所有工具都调用完了，回到AI分析
                }
            )
            
            # 工具节点执行完后，都回到 enhance_context 检查是否还需要调用其他工具
            # 注意：这些边只在增强上下文的迭代中生效，初始流程不受影响
            # 通过状态中的 tools_called_in_enhancement 来区分是初始调用还是增强调用
            workflow.add_conditional_edges(
                "crash_log_parser",
                self._check_enhancement_flow,  # 检查是否在增强流程中
                {
                    "enhance_context": "enhance_context",  # 如果在增强流程中，回到 enhance_context
                    "add2line_resolver": "add2line_resolver"  # 如果是初始流程，继续正常流程
                }
            )
            
            workflow.add_conditional_edges(
                "add2line_resolver",
                self._check_enhancement_flow,
                {
                    "enhance_context": "enhance_context",
                    "code_content_provider": "code_content_provider"
                }
            )
            
            workflow.add_conditional_edges(
                "code_content_provider",
                self._check_enhancement_flow,
                {
                    "enhance_context": "enhance_context",
                    "vector_db_search": "vector_db_search"
                }
            )
            
            # 编译图
            memory = MemorySaver()
            app = workflow.compile(checkpointer=memory)
            
            return app
        except Exception as e:
            print(f"ERROR: 构建图结构失败: {e}", file=sys.stderr)
            return None
    
    # ========== LangGraph 节点函数 ==========
    
    def _node_crash_log_parser(self, state: CrashAnalysisState) -> CrashAnalysisState:
        """节点1: 解析崩溃日志（支持增强流程）"""
        try:
            step_start = datetime.now()
            
            # 检查是否在增强流程中
            need_more_context = state.get("need_more_context", False)
            iteration_count = state.get("iteration_count", 0)
            is_enhancement = need_more_context and iteration_count > 0
            
            if is_enhancement:
                print(f"INFO: [增强流程] 正在重新解析崩溃日志...", file=sys.stderr)
                # 标记此工具已在增强流程中调用
                tools_called = state.get("tools_called_in_enhancement", [])
                if "crash_log_parser" not in tools_called:
                    tools_called.append("crash_log_parser")
                    state["tools_called_in_enhancement"] = tools_called
            else:
                print("INFO: 步骤1: 正在解析崩溃日志...", file=sys.stderr)
            
            crash_log_content = state["crash_log_content"]
            _popts = CrashParseOptions()
            _ld = state.get("library_dir")
            if _ld and os.path.exists(_ld):
                _popts = replace(_popts, library_dir=os.path.abspath(_ld))
            parsed_crash_info = crash_log_parser(crash_log_content, options=_popts)
            parsed_data = json.loads(parsed_crash_info)
            
            step_time = (datetime.now() - step_start).total_seconds()
            print(f"INFO: 崩溃日志提取完成，耗时: {step_time:.2f}秒", file=sys.stderr)
            
            # 添加执行时间信息
            parsed_data['start_time'] = step_start.strftime('%H:%M:%S.%f')[:-3]
            parsed_data['end_time'] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            parsed_data['duration'] = f"{step_time:.2f}"
            
            # 更新状态
            state["parsed_crash_info"] = parsed_crash_info
            state["parsed_data"] = parsed_data
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "crash_log_parser",
                "status": "success",
                "duration": step_time,
                "is_enhancement": is_enhancement,
                "iteration": iteration_count if is_enhancement else 0,
                "timestamp": datetime.now().isoformat()
            }]
            
            # 输出解析结果到stdout，供VSCode插件解析
            compressed_json = json.dumps(parsed_data, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:crash_log_parser:{compressed_json}")
            
            return state
        except Exception as e:
            print(f"ERROR: 崩溃日志提取失败: {e}", file=sys.stderr)
            state["error_message"] = f"crash_log_parser 失败: {str(e)}"
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "crash_log_parser",
                "status": "failed",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }]
            raise
    
    def _node_add2line_resolver(self, state: CrashAnalysisState) -> CrashAnalysisState:
        """节点2: 解析堆栈地址（支持增强流程）"""
        try:
            step_start = datetime.now()
            
            # 检查是否在增强流程中
            need_more_context = state.get("need_more_context", False)
            iteration_count = state.get("iteration_count", 0)
            is_enhancement = need_more_context and iteration_count > 0
            
            if is_enhancement:
                print(f"INFO: [增强流程] 正在重新解析堆栈地址...", file=sys.stderr)
                # 标记此工具已在增强流程中调用
                tools_called = state.get("tools_called_in_enhancement", [])
                if "add2line_resolver" not in tools_called:
                    tools_called.append("add2line_resolver")
                    state["tools_called_in_enhancement"] = tools_called
            else:
                print("INFO: 步骤2: 正在解析堆栈地址...", file=sys.stderr)
            
            parsed_crash_info = state.get("parsed_crash_info")
            if not parsed_crash_info:
                raise ValueError("缺少 parsed_crash_info，请先执行 crash_log_parser")
            
            library_dir = state.get("library_dir") or ""
            resolved_stack_trace = add2line_resolver(parsed_crash_info, library_dir)
            resolved_data = json.loads(resolved_stack_trace)
            
            step_time = (datetime.now() - step_start).total_seconds()
            print(f"INFO: 堆栈地址解析完成，耗时: {step_time:.2f}秒", file=sys.stderr)
            
            # 添加执行时间信息
            resolved_data['start_time'] = step_start.strftime('%H:%M:%S.%f')[:-3]
            resolved_data['end_time'] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            resolved_data['duration'] = f"{step_time:.2f}"
            
            # 更新状态
            state["resolved_stack_trace"] = resolved_stack_trace
            state["resolved_data"] = resolved_data
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "add2line_resolver",
                "status": "success",
                "duration": step_time,
                "is_enhancement": is_enhancement,
                "iteration": iteration_count if is_enhancement else 0,
                "timestamp": datetime.now().isoformat()
            }]
            
            # 输出解析结果到stdout
            compressed_json = json.dumps(resolved_data, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:add2line_resolver:{compressed_json}")
            
            return state
        except Exception as e:
            print(f"ERROR: 堆栈地址解析失败: {e}", file=sys.stderr)
            state["error_message"] = f"add2line_resolver 失败: {str(e)}"
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "add2line_resolver",
                "status": "failed",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }]
            raise
    
    def _node_code_content_provider(self, state: CrashAnalysisState) -> CrashAnalysisState:
        """节点3: 生成代码内容提示词（支持增强流程）"""
        try:
            step_start = datetime.now()
            
            # 检查是否在增强流程中
            need_more_context = state.get("need_more_context", False)
            iteration_count = state.get("iteration_count", 0)
            is_enhancement = need_more_context and iteration_count > 0
            
            if is_enhancement:
                print(f"INFO: [增强流程] 正在重新生成代码内容提示词...", file=sys.stderr)
                # 标记此工具已在增强流程中调用
                tools_called = state.get("tools_called_in_enhancement", [])
                if "code_content_provider" not in tools_called:
                    tools_called.append("code_content_provider")
                    state["tools_called_in_enhancement"] = tools_called
            else:
                print("INFO: 步骤3: 正在生成代码内容提示词...", file=sys.stderr)
            
            resolved_stack_trace = state.get("resolved_stack_trace")
            if not resolved_stack_trace:
                raise ValueError("缺少 resolved_stack_trace，请先执行 add2line_resolver")
            
            code_roots = state.get("code_roots") or ([state["code_root"]] if state.get("code_root") else [])
            # 使用代码索引服务（如果可用）
            _backend = os.environ.get("MAP_SDK_CRASH_CODE_PARSER_BACKEND", "tree-sitter")
            provider = CodeContentProvider(
                code_parser_backend=_backend,
                code_index_service=self.code_index_service,
            )
            code_context_prompt = provider.code_content_provider(resolved_stack_trace, code_roots)
            prompt_data = json.loads(code_context_prompt)
            
            step_time = (datetime.now() - step_start).total_seconds()
            print(f"INFO: 代码内容提示词生成完成，耗时: {step_time:.2f}秒", file=sys.stderr)
            
            # 添加执行时间信息
            prompt_data['start_time'] = step_start.strftime('%H:%M:%S.%f')[:-3]
            prompt_data['end_time'] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            prompt_data['duration'] = f"{step_time:.2f}"
            
            # 更新状态
            state["code_context_prompt"] = code_context_prompt
            state["prompt_data"] = prompt_data
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "code_content_provider",
                "status": "success",
                "duration": step_time,
                "is_enhancement": is_enhancement,
                "iteration": iteration_count if is_enhancement else 0,
                "timestamp": datetime.now().isoformat()
            }]
            
            # 输出解析结果到stdout
            compressed_json = json.dumps(prompt_data, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:code_content_provider:{compressed_json}")
            
            return state
        except Exception as e:
            print(f"ERROR: 代码内容提示词生成失败: {e}", file=sys.stderr)
            state["error_message"] = f"code_content_provider 失败: {str(e)}"
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "code_content_provider",
                "status": "failed",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }]
            raise
    
    def _node_vector_db_search(self, state: CrashAnalysisState) -> CrashAnalysisState:
        """节点4: 规则优先 + 向量检索（可选）"""
        try:
            if not self.vector_db_analyzer:
                print("INFO: 向量数据库未配置，跳过检索", file=sys.stderr)
                state["rule_hits"] = []
                state["pattern_hits"] = []
                state["evidence_map"] = {}
                state["strategy_hits"] = []
                state["decision_trace"] = []
                state["vector_used"] = False
                state["memory_context"] = ""
                return state
            
            print("INFO: 步骤4: 规则优先检索与向量召回...", file=sys.stderr)

            parsed_data = state.get("parsed_data") or {}
            resolved_data = state.get("resolved_data") or {}
            prompt_data = state.get("prompt_data") or {}
            features = extract_features(parsed_data, resolved_data, prompt_data)

            workflow_config = self.config.get("workflow_config", {}) if isinstance(self.config, dict) else {}
            rule_threshold = float(workflow_config.get("rule_confidence_threshold", 0.85))
            max_results = int(self.config.get("vector_db_config", {}).get("max_results", 3))

            rule_hits = self.vector_db_analyzer.match_rules(features, min_confidence=rule_threshold)
            high_conf_hits = [h for h in rule_hits if h.get("is_high_confidence")]

            decision_trace = []
            pattern_hits: List[Dict[str, Any]] = []
            evidence_map: Dict[str, Any] = {}
            strategy_hits: List[Dict[str, Any]] = []
            vector_used = False

            if high_conf_hits:
                decision_trace.append({
                    "stage": "rule",
                    "result": "hit",
                    "rule_ids": [h.get("rule_id") for h in high_conf_hits],
                })
            else:
                query_text, _signature = build_pattern_query(parsed_data, resolved_data, prompt_data)
                pattern_hits = self.vector_db_analyzer.retrieve_patterns(query_text, n_results=max_results)
                vector_used = bool(pattern_hits)
                pattern_ids = [p.get("pattern_id") for p in pattern_hits if p.get("pattern_id")]
                for pid in pattern_ids:
                    evidence_map[pid] = self.vector_db_analyzer.get_evidence(pid)
                strategy_hits = self.vector_db_analyzer.get_fix_strategies(pattern_ids)
                decision_trace.append({
                    "stage": "vector",
                    "result": "hit" if pattern_hits else "miss",
                    "pattern_ids": pattern_ids,
                })

            memory_context = self._render_memory_context(rule_hits, pattern_hits, evidence_map, strategy_hits)

            # 更新状态
            state["rule_hits"] = rule_hits
            state["pattern_hits"] = pattern_hits
            state["evidence_map"] = evidence_map
            state["strategy_hits"] = strategy_hits
            state["decision_trace"] = decision_trace
            state["vector_used"] = vector_used
            state["memory_context"] = memory_context
            
            return state
        except Exception as e:
            print(f"WARNING: 向量数据库搜索失败: {e}", file=sys.stderr)
            state["rule_hits"] = []
            state["pattern_hits"] = []
            state["evidence_map"] = {}
            state["strategy_hits"] = []
            state["decision_trace"] = []
            state["vector_used"] = False
            state["memory_context"] = ""
            return state  # 不抛出异常，继续执行
    
    def _node_ai_analysis(self, state: CrashAnalysisState) -> CrashAnalysisState:
        """节点5: AI分析（支持多轮调用）"""
        try:
            step_start = datetime.now()
            iteration_count = state.get("iteration_count", 0)
            
            if iteration_count > 0:
                print(f"INFO: [增强流程] 正在执行AI分析（迭代 {iteration_count}）...", file=sys.stderr)
            else:
                print("INFO: 步骤5: 正在执行AI分析...", file=sys.stderr)
            
            prompt_data = state.get("prompt_data")
            if not prompt_data:
                raise ValueError("缺少 prompt_data，请先执行 code_content_provider")
            
            crash_contexts = prompt_data.get('crash_contexts', [])
            code_contexts = prompt_data.get('code_contexts', [])
            memory_context = state.get("memory_context", "")
            rule_ids = [h.get("rule_id") for h in (state.get("rule_hits") or []) if h.get("rule_id")]
            pattern_ids = [p.get("pattern_id") for p in (state.get("pattern_hits") or []) if p.get("pattern_id")]
            guidance_text = self._get_guidance_for_prompt(rule_ids, pattern_ids, prompt_data)
            enhanced_contexts = state.get("enhanced_contexts", [])
            if enhanced_contexts:
                guidance_text += "\n\n## 增强的上下文信息\n"
                for i, ctx in enumerate(enhanced_contexts):
                    guidance_text += f"\n增强上下文 {i+1}:\n{ctx}\n"
            full_prompt = self._build_full_prompt(crash_contexts, code_contexts, guidance_text, memory_context)
            
            # 执行AI分析
            model = state.get("model")
            ai_response = self._perform_ai_analysis(full_prompt, model)
            
            # 检查AI响应是否需要更多上下文
            need_more_context, context_request = self._check_if_need_more_context(ai_response)
            
            step_time = (datetime.now() - step_start).total_seconds()
            print(f"INFO: AI分析完成，耗时: {step_time:.2f}秒", file=sys.stderr)
            
            if need_more_context:
                print(f"INFO: AI请求更多上下文: {context_request}", file=sys.stderr)
            
            ai_result = {
                "prompt_content": {
                    "analysis_guidance": guidance_text,
                    "crash_contexts": crash_contexts,
                    "code_contexts": code_contexts
                },
                "prompt_text": full_prompt,
                "analysis_result": ai_response,
                "model_used": model or self.default_model,
                "need_more_context": need_more_context,
                "context_request": context_request if need_more_context else None,
                "iteration": iteration_count,
                "start_time": step_start.strftime('%H:%M:%S.%f')[:-3],
                "end_time": datetime.now().strftime('%H:%M:%S.%f')[:-3],
                "duration": f"{step_time:.2f}"
            }
            
            # 更新状态
            state["ai_analysis"] = ai_response
            state["ai_result"] = ai_result
            state["need_more_context"] = need_more_context
            state["context_enhancement_request"] = context_request if need_more_context else None
            if need_more_context:
                state["iteration_count"] = iteration_count + 1
                # 重置工具调用列表，准备新一轮的工具调用
                state["tools_called_in_enhancement"] = []
            
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "ai_analysis",
                "status": "success",
                "duration": step_time,
                "need_more_context": need_more_context,
                "iteration": iteration_count,
                "timestamp": datetime.now().isoformat()
            }]
            
            # 输出AI分析结果到stdout
            compressed_json = json.dumps(ai_result, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:ai_analysis:{compressed_json}")
            
            return state
        except Exception as e:
            print(f"ERROR: AI分析失败: {e}", file=sys.stderr)
            # 即使AI分析失败，也要输出一个结果
            prompt_data = state.get("prompt_data", {})
            rule_ids = [h.get("rule_id") for h in (state.get("rule_hits") or []) if h.get("rule_id")]
            pattern_ids = [p.get("pattern_id") for p in (state.get("pattern_hits") or []) if p.get("pattern_id")]
            fallback_guidance = self._get_guidance_for_prompt(rule_ids, pattern_ids, prompt_data)
            ai_result = {
                "prompt_content": {
                    "analysis_guidance": fallback_guidance,
                    "crash_contexts": prompt_data.get('crash_contexts', []),
                    "code_contexts": prompt_data.get('code_contexts', [])
                },
                "prompt_text": full_prompt if 'full_prompt' in locals() else "",
                "analysis_result": f"AI分析失败: {e}",
                "model_used": state.get("model") or self.default_model,
                "start_time": datetime.now().strftime('%H:%M:%S.%f')[:-3],
                "end_time": datetime.now().strftime('%H:%M:%S.%f')[:-3],
                "duration": "0.00",
                "error": str(e)
            }
            state["ai_analysis"] = f"AI分析失败: {e}"
            state["ai_result"] = ai_result
            state["error_message"] = f"ai_analysis 失败: {str(e)}"
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "ai_analysis",
                "status": "failed",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }]
            
            compressed_json = json.dumps(ai_result, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:ai_analysis:{compressed_json}")
            
            return state  # 不抛出异常，允许流程继续
    
    def _node_enhance_context(self, state: CrashAnalysisState) -> CrashAnalysisState:
        """节点：上下文增强协调器（状态机模式）"""
        try:
            step_start = datetime.now()
            print("INFO: 正在协调上下文增强流程...", file=sys.stderr)
            
            # 这个节点主要负责协调，实际的工具调用由路由函数决定
            # 这里主要做状态管理和日志记录
            iteration_count = state.get("iteration_count", 0)
            max_iterations = state.get("max_iterations", 3)
            tools_called = state.get("tools_called_in_enhancement", [])
            
            print(f"INFO: 上下文增强迭代 {iteration_count}/{max_iterations}，已调用工具: {tools_called}", file=sys.stderr)
            
            state["execution_log"] = state.get("execution_log", []) + [{
                "step": "enhance_context",
                "status": "coordinating",
                "iteration": iteration_count,
                "tools_called": tools_called,
                "timestamp": datetime.now().isoformat()
            }]
            
            return state
        except Exception as e:
            print(f"ERROR: 上下文增强协调失败: {e}", file=sys.stderr)
            state["need_more_context"] = False
            return state
    
    # ========== 路由函数（状态机模式）==========
    
    def _should_enhance_context(self, state: CrashAnalysisState) -> str:
        """
        判断是否需要增强上下文
        
        Returns:
            "enhance" 或 "end"
        """
        need_more_context = state.get("need_more_context", False)
        iteration_count = state.get("iteration_count", 0)
        max_iterations = state.get("max_iterations", 3)
        
        if need_more_context and iteration_count < max_iterations:
            print(f"INFO: 需要增强上下文（迭代 {iteration_count}/{max_iterations}）", file=sys.stderr)
            return "enhance"
        else:
            if iteration_count >= max_iterations:
                print(f"INFO: 已达到最大迭代次数，结束分析", file=sys.stderr)
            else:
                print(f"INFO: AI分析完成，无需更多上下文", file=sys.stderr)
            return "end"
    
    def _route_to_tool(self, state: CrashAnalysisState) -> str:
        """
        根据AI需求路由到不同的工具节点（状态机模式）
        
        Returns:
            工具节点名称或 "ai_analysis"
        """
        context_request_str = state.get("context_enhancement_request", "")
        if not context_request_str:
            print("INFO: 没有上下文请求，直接回到AI分析", file=sys.stderr)
            return "ai_analysis"
        
        try:
            context_request = json.loads(context_request_str)
            missing = context_request.get("missing", [])
            suggested_tools = context_request.get("suggested_tools", [])
            tools_called = state.get("tools_called_in_enhancement", [])
            
            print(f"INFO: AI请求的缺失信息: {missing}", file=sys.stderr)
            print(f"INFO: AI建议的工具: {suggested_tools}", file=sys.stderr)
            print(f"INFO: 已调用的工具: {tools_called}", file=sys.stderr)
            
            # 按优先级决定调用哪个工具
            # 优先级：crash_log_parser -> add2line_resolver -> code_content_provider
            
            # 检查是否需要调用 crash_log_parser
            if (("crash_log_details" in missing or 
                 "crash_log_parser" in suggested_tools or
                 "log_filter" in suggested_tools) and
                "crash_log_parser" not in tools_called):
                print("INFO: 路由到工具: crash_log_parser", file=sys.stderr)
                return "crash_log_parser"
            
            # 检查是否需要调用 add2line_resolver
            if (("resolved_stack_trace" in missing or 
                 "add2line_resolver" in suggested_tools or
                 "addr2line" in suggested_tools) and
                "add2line_resolver" not in tools_called):
                print("INFO: 路由到工具: add2line_resolver", file=sys.stderr)
                return "add2line_resolver"
            
            # 检查是否需要调用 code_content_provider
            if (("function_source_code" in missing or 
                 "surrounding_code_context" in missing or
                 "code_content_provider" in suggested_tools) and
                "code_content_provider" not in tools_called):
                print("INFO: 路由到工具: code_content_provider", file=sys.stderr)
                return "code_content_provider"
            
            # 如果所有工具都调用完了，回到AI分析
            print("INFO: 所有请求的工具都已调用，回到AI分析", file=sys.stderr)
            return "ai_analysis"
        except json.JSONDecodeError as e:
            print(f"WARNING: 解析上下文请求失败: {e}，直接回到AI分析", file=sys.stderr)
            return "ai_analysis"
        except Exception as e:
            print(f"WARNING: 路由决策失败: {e}，直接回到AI分析", file=sys.stderr)
            return "ai_analysis"
    
    def _check_enhancement_flow(self, state: CrashAnalysisState) -> str:
        """
        检查当前是否在增强上下文的流程中
        
        Returns:
            "enhance_context" 如果在增强流程中，否则返回下一个正常流程节点名称
        """
        # 检查是否在增强流程中：通过检查 tools_called_in_enhancement 是否不为空
        # 或者通过检查 execution_log 中最近的步骤是否标记为 is_enhancement
        tools_called = state.get("tools_called_in_enhancement", [])
        execution_log = state.get("execution_log", [])
        
        # 如果 tools_called_in_enhancement 不为空，说明在增强流程中
        if tools_called:
            print(f"INFO: 检测到增强流程，工具调用后回到 enhance_context", file=sys.stderr)
            return "enhance_context"
        
        # 检查最近的执行日志，看是否标记为增强流程
        if execution_log:
            last_log = execution_log[-1]
            if last_log.get("is_enhancement", False):
                print(f"INFO: 检测到增强流程（通过日志标记），工具调用后回到 enhance_context", file=sys.stderr)
                return "enhance_context"
        
        # 正常流程，根据当前步骤返回下一个节点
        # 注意：这里返回 None 会让 LangGraph 使用默认边（在 _build_graph 中定义的边）
        # 但为了明确，我们返回下一个节点名称
        current_step = execution_log[-1].get("step", "") if execution_log else ""
        if current_step == "crash_log_parser":
            return "add2line_resolver"
        elif current_step == "add2line_resolver":
            return "code_content_provider"
        elif current_step == "code_content_provider":
            return "vector_db_search"
        else:
            # 默认继续下一个节点
            return "add2line_resolver"  # 安全默认值
    
    def _analyze_crash_traditional(self, crash_log_content: str, code_roots: List[str], library_dir: str, model: str = None):
        """传统顺序执行方法（LangGraph 不可用时的回退）"""
        start_time = datetime.now()
        
        try:
            print("INFO: 开始执行完整的崩溃分析流程（传统模式）...", file=sys.stderr)
            
            # 步骤1: 解析崩溃日志
            step1_start = datetime.now()
            print("INFO: 步骤1: 正在解析崩溃日志...", file=sys.stderr)
            
            _popts = CrashParseOptions()
            if library_dir and os.path.exists(library_dir):
                _popts = replace(_popts, library_dir=os.path.abspath(library_dir))
            parsed_crash_info = crash_log_parser(crash_log_content, options=_popts)
            parsed_data = json.loads(parsed_crash_info)
            
            step1_time = (datetime.now() - step1_start).total_seconds()
            print(f"INFO: 崩溃日志提取完成，耗时: {step1_time:.2f}秒", file=sys.stderr)
            
            parsed_data['start_time'] = step1_start.strftime('%H:%M:%S.%f')[:-3]
            parsed_data['end_time'] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            parsed_data['duration'] = f"{step1_time:.2f}"
            
            compressed_json = json.dumps(parsed_data, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:crash_log_parser:{compressed_json}")
            
            # 步骤2: 解析堆栈地址
            step2_start = datetime.now()
            print("INFO: 步骤2: 正在解析堆栈地址...", file=sys.stderr)
            
            resolved_stack_trace = add2line_resolver(parsed_crash_info, library_dir or "")
            resolved_data = json.loads(resolved_stack_trace)
            
            step2_time = (datetime.now() - step2_start).total_seconds()
            print(f"INFO: 堆栈地址解析完成，耗时: {step2_time:.2f}秒", file=sys.stderr)
            
            resolved_data['start_time'] = step2_start.strftime('%H:%M:%S.%f')[:-3]
            resolved_data['end_time'] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            resolved_data['duration'] = f"{step2_time:.2f}"
            
            compressed_json = json.dumps(resolved_data, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:add2line_resolver:{compressed_json}")
            
            # 步骤3: 生成代码内容提示词
            step3_start = datetime.now()
            print("INFO: 步骤3: 正在生成代码内容提示词...", file=sys.stderr)
            
            # 初始化代码索引服务（如果尚未初始化，且 services/ 模块可用）
            norm_roots = tuple(os.path.abspath(r) for r in (code_roots or []) if r)
            cur = tuple(getattr(self.code_index_service, "code_roots", []) or []) if self.code_index_service else ()
            if _CODE_INDEX_AVAILABLE and (self.code_index_service is None or cur != norm_roots):
                try:
                    self.code_index_service = CodeIndexMultiRoot(list(code_roots or []))
                except Exception as e:
                    print(f"WARNING: 代码索引服务初始化失败: {e}，将不使用索引", file=sys.stderr)
                    self.code_index_service = None
            
            _backend = os.environ.get("MAP_SDK_CRASH_CODE_PARSER_BACKEND", "tree-sitter")
            provider = CodeContentProvider(
                code_parser_backend=_backend,
                code_index_service=self.code_index_service,
            )
            code_context_prompt = provider.code_content_provider(resolved_stack_trace, code_roots)
            prompt_data = json.loads(code_context_prompt)
            
            step3_time = (datetime.now() - step3_start).total_seconds()
            print(f"INFO: 代码内容提示词生成完成，耗时: {step3_time:.2f}秒", file=sys.stderr)
            
            prompt_data['start_time'] = step3_start.strftime('%H:%M:%S.%f')[:-3]
            prompt_data['end_time'] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            prompt_data['duration'] = f"{step3_time:.2f}"
            
            compressed_json = json.dumps(prompt_data, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:code_content_provider:{compressed_json}")
            
            # 步骤4: 规则优先 + 向量检索（可选）
            memory_context = ""
            rule_hits = []
            pattern_hits = []
            evidence_map = {}
            strategy_hits = []
            decision_trace = []
            if self.vector_db_analyzer:
                try:
                    print("INFO: 步骤4: 规则优先检索与向量召回...", file=sys.stderr)
                    features = extract_features(parsed_data, resolved_data, prompt_data)
                    workflow_config = self.config.get("workflow_config", {}) if isinstance(self.config, dict) else {}
                    rule_threshold = float(workflow_config.get("rule_confidence_threshold", 0.85))
                    max_results = int(self.config.get("vector_db_config", {}).get("max_results", 3))

                    rule_hits = self.vector_db_analyzer.match_rules(features, min_confidence=rule_threshold)
                    high_conf_hits = [h for h in rule_hits if h.get("is_high_confidence")]

                    if high_conf_hits:
                        decision_trace.append({
                            "stage": "rule",
                            "result": "hit",
                            "rule_ids": [h.get("rule_id") for h in high_conf_hits],
                        })
                    else:
                        query_text, _signature = build_pattern_query(parsed_data, resolved_data, prompt_data)
                        pattern_hits = self.vector_db_analyzer.retrieve_patterns(query_text, n_results=max_results)
                        pattern_ids = [p.get("pattern_id") for p in pattern_hits if p.get("pattern_id")]
                        for pid in pattern_ids:
                            evidence_map[pid] = self.vector_db_analyzer.get_evidence(pid)
                        strategy_hits = self.vector_db_analyzer.get_fix_strategies(pattern_ids)
                        decision_trace.append({
                            "stage": "vector",
                            "result": "hit" if pattern_hits else "miss",
                            "pattern_ids": pattern_ids,
                        })

                    memory_context = self._render_memory_context(rule_hits, pattern_hits, evidence_map, strategy_hits)
                except Exception as e:
                    print(f"WARNING: 向量数据库搜索失败: {e}", file=sys.stderr)
            
            # 步骤5: AI分析
            step5_start = datetime.now()
            print("INFO: 步骤5: 正在执行AI分析...", file=sys.stderr)
            
            crash_contexts = prompt_data.get('crash_contexts', [])
            code_contexts = prompt_data.get('code_contexts', [])
            rule_ids = [h.get("rule_id") for h in (rule_hits or []) if h.get("rule_id")]
            pattern_ids = [p.get("pattern_id") for p in (pattern_hits or []) if p.get("pattern_id")]
            guidance_text = self._get_guidance_for_prompt(rule_ids, pattern_ids, prompt_data)
            full_prompt = self._build_full_prompt(crash_contexts, code_contexts, guidance_text, memory_context)
            ai_response = self._perform_ai_analysis(full_prompt, model)
            
            step5_time = (datetime.now() - step5_start).total_seconds()
            print(f"INFO: AI分析完成，耗时: {step5_time:.2f}秒", file=sys.stderr)
            
            ai_result = {
                "prompt_content": {
                    "analysis_guidance": guidance_text,
                    "crash_contexts": crash_contexts,
                    "code_contexts": code_contexts
                },
                "analysis_result": ai_response,
                "model_used": model or self.default_model,
                "start_time": step5_start.strftime('%H:%M:%S.%f')[:-3],
                "end_time": datetime.now().strftime('%H:%M:%S.%f')[:-3],
                "duration": f"{step5_time:.2f}"
            }
            
            compressed_json = json.dumps(ai_result, separators=(',', ':'), ensure_ascii=False)
            print(f"TOOL_OUTPUT:ai_analysis:{compressed_json}")
            
            total_time = (datetime.now() - start_time).total_seconds()
            print(f"INFO: 完整分析流程完成，总耗时: {total_time:.2f}秒", file=sys.stderr)

            # 构建最终状态（供 CLI/daemon 输出）
            final_state = {
                "crash_log_content": crash_log_content,
                "code_roots": list(code_roots or []),
                "code_root": (code_roots or [""])[0],
                "library_dir": library_dir,
                "model": model,
                "prompt_data": prompt_data,
                "rule_hits": rule_hits,
                "pattern_hits": pattern_hits,
                "evidence_map": evidence_map,
                "strategy_hits": strategy_hits,
                "decision_trace": decision_trace,
                "vector_used": bool(pattern_hits),
                "memory_context": memory_context,
                "ai_analysis": ai_response,
                "ai_result": ai_result,
                "total_time": total_time,
                "parsed_data": parsed_data,
                "resolved_data": resolved_data,
            }
            
            # 自动保存分析结果到向量数据库（仅当有有效的AI分析结果时）
            if self.vector_db_analyzer and not self.disable_vector_db_save:
                try:
                    print("INFO: 正在保存分析结果到向量数据库...", file=sys.stderr)
                    save_success = self._save_state_to_vector_db(final_state)
                    if save_success:
                        print("INFO: 分析结果已保存到向量数据库", file=sys.stderr)
                    else:
                        print("INFO: 跳过保存（AI分析结果无效或为空）", file=sys.stderr)
                except Exception as e:
                    print(f"WARNING: 保存到向量数据库时出错: {e}", file=sys.stderr)
            
            return final_state
            
        except Exception as e:
            print(f"ERROR: 完整分析流程失败: {e}", file=sys.stderr)
            raise
    
    # ========== 公共方法 ==========
    
    def analyze_crash_with_ai(self, crash_log_content: str, code_roots: List[str], library_dir: str, model: str = None):
        """
        执行完整的崩溃分析流程（包含AI分析）
        优先使用 LangGraph，如果不可用则回退到传统模式
        """
        start_time = datetime.now()
        roots = [os.path.abspath(r) for r in (code_roots or []) if r and str(r).strip()]
        
        # 初始化或更新代码索引服务（如果 code_roots 改变，且 services/ 模块可用）
        cur = tuple(getattr(self.code_index_service, "code_roots", []) or []) if self.code_index_service else ()
        if _CODE_INDEX_AVAILABLE and (self.code_index_service is None or cur != tuple(roots)):
            try:
                print(f"INFO: 初始化代码索引服务: {roots}", file=sys.stderr)
                self.code_index_service = CodeIndexMultiRoot(roots)
                print(f"INFO: 代码索引服务初始化完成", file=sys.stderr)
            except Exception as e:
                print(f"WARNING: 代码索引服务初始化失败: {e}，将不使用索引", file=sys.stderr)
                self.code_index_service = None
        
        # 如果 LangGraph 可用且未被配置禁用，使用图结构执行
        workflow_config_check = self.config.get('workflow_config', {}) if isinstance(self.config, dict) else {}
        _disable_langgraph = bool(workflow_config_check.get("disable_langgraph", False))
        if self.graph and not _disable_langgraph:
            try:
                print("INFO: 开始执行完整的崩溃分析流程（LangGraph模式）...", file=sys.stderr)
                
                # 初始化状态（包含多轮调用控制字段）
                workflow_config = self.config.get('workflow_config', {})
                max_iterations = workflow_config.get('max_iterations', 3)
                
                initial_state: CrashAnalysisState = {
                    "crash_log_content": crash_log_content,
                    "code_roots": roots,
                    "code_root": roots[0] if roots else "",
                    "library_dir": library_dir,
                    "model": model,
                    "parsed_crash_info": None,
                    "parsed_data": None,
                    "resolved_stack_trace": None,
                    "resolved_data": None,
                    "code_context_prompt": None,
                    "prompt_data": None,
                    "rule_hits": None,
                    "pattern_hits": None,
                    "evidence_map": None,
                    "strategy_hits": None,
                    "decision_trace": None,
                    "vector_used": None,
                    "memory_context": None,
                    "ai_analysis": None,
                    "ai_result": None,
                    "config": self.config,
                    "execution_log": [],
                    "error_message": None,
                    "start_time": start_time.isoformat(),
                    "total_time": None,
                    # 多轮调用控制字段
                    "need_more_context": False,
                    "context_enhancement_request": None,
                    "iteration_count": 0,
                    "max_iterations": max_iterations,
                    "tools_called_in_enhancement": [],
                    "enhanced_contexts": []
                }
                
                # 执行图
                config = {"configurable": {"thread_id": "1"}}
                final_state = self.graph.invoke(initial_state, config)
                
                # 计算总耗时
                total_time = (datetime.now() - start_time).total_seconds()
                final_state["total_time"] = total_time
                
                # 自动保存分析结果到向量数据库（仅当有有效的AI分析结果时）
                if self.vector_db_analyzer and not self.disable_vector_db_save:
                    try:
                        print("INFO: 正在保存分析结果到向量数据库...", file=sys.stderr)
                        save_success = self._save_state_to_vector_db(final_state)
                        if save_success:
                            print("INFO: 分析结果已保存到向量数据库", file=sys.stderr)
                        else:
                            print("INFO: 跳过保存（AI分析结果无效或为空）", file=sys.stderr)
                    except Exception as e:
                        print(f"WARNING: 保存到向量数据库时出错: {e}", file=sys.stderr)
                
                print(f"INFO: 完整分析流程完成（LangGraph模式），总耗时: {total_time:.2f}秒", file=sys.stderr)
                
                return final_state
                
            except Exception as e:
                print(f"WARNING: LangGraph执行失败，回退到传统模式: {e}", file=sys.stderr)
                # 回退到传统模式
                return self._analyze_crash_traditional(crash_log_content, roots, library_dir, model)
        else:
            # 使用传统模式
            return self._analyze_crash_traditional(crash_log_content, roots, library_dir, model)

    def analyze_crash(self, crash_log_content: str, code_roots: List[str], library_dir: str,
                      max_stack_frames: Optional[int] = None,
                      crash_parse_options: Optional[CrashParseOptions] = None) -> CrashAnalysisResult:
        """兼容旧 CLI：完整分析入口。"""
        final_state = self.analyze_crash_with_ai(crash_log_content, code_roots, library_dir, model=None)
        return CrashAnalysisResult(
            crash_log_content=crash_log_content,
            parsed_crash_info=final_state.get("parsed_data") or {},
            resolved_stack_trace=final_state.get("resolved_data") or {},
            code_context_prompt=final_state.get("prompt_data") or {},
            ai_analysis=str(final_state.get("ai_analysis") or ""),
            analysis_steps=[],
            total_execution_time=float(final_state.get("total_time") or 0.0),
            timestamp=datetime.now().isoformat(),
            status="success" if final_state.get("ai_analysis") else "success_without_ai",
            rule_hits=final_state.get("rule_hits"),
            pattern_hits=final_state.get("pattern_hits"),
            evidence_map=final_state.get("evidence_map"),
            strategy_hits=final_state.get("strategy_hits"),
            decision_trace=final_state.get("decision_trace"),
            vector_used=final_state.get("vector_used"),
            memory_context=final_state.get("memory_context"),
        )

    def analyze_crash_optimized(self, crash_log_content: str, code_roots: List[str], library_dir: str,
                                exclude_dirs: Optional[List[str]] = None,
                                include_subdirs: Optional[List[str]] = None,
                                crash_parse_options: Optional[CrashParseOptions] = None) -> CrashAnalysisResult:
        """兼容旧 CLI：optimized 目前复用完整分析。"""
        return self.analyze_crash(crash_log_content, code_roots, library_dir, crash_parse_options=crash_parse_options)

    def analyze_crash_parse_log_only(self, crash_log_content: str,
                                     crash_parse_options: Optional[CrashParseOptions] = None) -> CrashAnalysisResult:
        start_time = datetime.now()
        analysis_steps: List[AnalysisStep] = []
        try:
            step_start = datetime.now()
            parsed_crash_info = crash_log_parser(crash_log_content, options=crash_parse_options or CrashParseOptions())
            parsed_data = json.loads(parsed_crash_info)
            elapsed = (datetime.now() - step_start).total_seconds()
            parsed_data["start_time"] = step_start.strftime('%H:%M:%S.%f')[:-3]
            parsed_data["end_time"] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            parsed_data["duration"] = f"{elapsed:.2f}"
            print(f"TOOL_OUTPUT:crash_log_parser:{json.dumps(parsed_data, separators=(',', ':'), ensure_ascii=False)}")
            analysis_steps.append(AnalysisStep("crash_log_parser", crash_log_content[:200] + "...", parsed_data, elapsed, "success"))
            total = (datetime.now() - start_time).total_seconds()
            return CrashAnalysisResult(
                crash_log_content=crash_log_content,
                parsed_crash_info=parsed_data,
                resolved_stack_trace={},
                code_context_prompt={},
                ai_analysis="仅执行了崩溃日志提取",
                analysis_steps=analysis_steps,
                total_execution_time=total,
                timestamp=datetime.now().isoformat(),
                status="success_parse_log_only",
            )
        except Exception as e:
            total = (datetime.now() - start_time).total_seconds()
            return CrashAnalysisResult(crash_log_content, {}, {}, {}, "解析失败", analysis_steps, total, datetime.now().isoformat(), "failed")

    def analyze_crash_parse_only(self, crash_log_content: str, library_dir: str, max_stack_frames: Optional[int] = None,
                                 crash_parse_options: Optional[CrashParseOptions] = None) -> CrashAnalysisResult:
        start_time = datetime.now()
        analysis_steps: List[AnalysisStep] = []
        try:
            opts = self._merge_crash_parse_options(crash_parse_options, library_dir)
            step1 = datetime.now()
            parsed_crash_info = crash_log_parser(crash_log_content, options=opts)
            parsed_data = json.loads(parsed_crash_info)
            t1 = (datetime.now() - step1).total_seconds()
            parsed_data["start_time"] = step1.strftime('%H:%M:%S.%f')[:-3]
            parsed_data["end_time"] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            parsed_data["duration"] = f"{t1:.2f}"
            print(f"TOOL_OUTPUT:crash_log_parser:{json.dumps(parsed_data, separators=(',', ':'), ensure_ascii=False)}")
            analysis_steps.append(AnalysisStep("crash_log_parser", crash_log_content[:200] + "...", parsed_data, t1, "success"))

            step2 = datetime.now()
            resolved_stack_trace = add2line_resolver(parsed_crash_info, library_dir, max_frames=max_stack_frames, quick_mode=True)
            resolved_data = json.loads(resolved_stack_trace)
            t2 = (datetime.now() - step2).total_seconds()
            resolved_data["start_time"] = step2.strftime('%H:%M:%S.%f')[:-3]
            resolved_data["end_time"] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            resolved_data["duration"] = f"{t2:.2f}"
            print(f"TOOL_OUTPUT:add2line_resolver:{json.dumps(resolved_data, separators=(',', ':'), ensure_ascii=False)}")
            analysis_steps.append(AnalysisStep("add2line_resolver", parsed_data, resolved_data, t2, "success"))
            total = (datetime.now() - start_time).total_seconds()
            return CrashAnalysisResult(
                crash_log_content=crash_log_content,
                parsed_crash_info=parsed_data,
                resolved_stack_trace=resolved_data,
                code_context_prompt={},
                ai_analysis="只执行了崩溃日志提取和地址解析",
                analysis_steps=analysis_steps,
                total_execution_time=total,
                timestamp=datetime.now().isoformat(),
                status="success_parse_only",
            )
        except Exception:
            total = (datetime.now() - start_time).total_seconds()
            return CrashAnalysisResult(crash_log_content, {}, {}, {}, "解析失败", analysis_steps, total, datetime.now().isoformat(), "failed")

    def analyze_crash_without_ai(self, crash_log_content: str, code_roots: List[str], library_dir: str,
                                 exclude_dirs: Optional[List[str]] = None,
                                 include_subdirs: Optional[List[str]] = None,
                                 max_stack_frames: Optional[int] = None,
                                 crash_parse_options: Optional[CrashParseOptions] = None) -> CrashAnalysisResult:
        start_time = datetime.now()
        analysis_steps: List[AnalysisStep] = []
        try:
            parse_res = self.analyze_crash_parse_only(
                crash_log_content=crash_log_content,
                library_dir=library_dir,
                max_stack_frames=max_stack_frames,
                crash_parse_options=crash_parse_options,
            )
            parsed_data = parse_res.parsed_crash_info
            resolved_data = parse_res.resolved_stack_trace
            analysis_steps.extend(parse_res.analysis_steps)
            resolved_stack_trace = json.dumps(resolved_data, ensure_ascii=False)

            step3 = datetime.now()
            provider = CodeContentProvider(
                exclude_dirs=exclude_dirs,
                include_subdirs=include_subdirs,
                code_parser_backend=self.code_parser_backend,
                max_static_call_chain_depth=self.max_static_call_chain_depth,
                max_direct_callers=self.max_direct_callers,
                max_shared_var_related_functions=self.max_shared_var_related_functions,
                max_symbol_only_rescues=self.max_symbol_only_rescues,
                find_source_timeout_sec=self.find_source_timeout_sec,
                code_context_timeout_sec=self.code_context_timeout_sec,
                code_index_service=self.code_index_service,
            )
            code_context_prompt = provider.code_content_provider(
                resolved_stack_trace,
                [os.path.abspath(r) for r in (code_roots or []) if r],
                exclude_dirs=exclude_dirs,
                include_subdirs=include_subdirs,
            )
            prompt_data = json.loads(code_context_prompt)
            t3 = (datetime.now() - step3).total_seconds()
            prompt_data["start_time"] = step3.strftime('%H:%M:%S.%f')[:-3]
            prompt_data["end_time"] = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            prompt_data["duration"] = f"{t3:.2f}"
            print(f"TOOL_OUTPUT:code_content_provider:{json.dumps(prompt_data, separators=(',', ':'), ensure_ascii=False)}")
            analysis_steps.append(AnalysisStep("code_content_provider", resolved_data, prompt_data, t3, "success"))

            # 与 run bundle 对齐：输出 vector_memory + ai_prompt（即使在 prompt_only 模式下）
            rule_hits = pattern_hits = strategy_hits = []
            evidence_map: Dict[str, Any] = {}
            decision_trace: List[Dict[str, Any]] = []
            vector_used = False
            memory_context = ""
            if self.vector_db_analyzer:
                try:
                    features = extract_features(parsed_data, resolved_data, prompt_data)
                    workflow_config = self.config.get("workflow_config", {}) if isinstance(self.config, dict) else {}
                    rule_threshold = float(workflow_config.get("rule_confidence_threshold", 0.85))
                    max_results = int(self.config.get("vector_db_config", {}).get("max_results", 3))
                    rule_hits = self.vector_db_analyzer.match_rules(features, min_confidence=rule_threshold)
                    high_conf_hits = [h for h in rule_hits if h.get("is_high_confidence")]
                    if high_conf_hits:
                        decision_trace.append({"stage": "rule", "result": "hit", "rule_ids": [h.get("rule_id") for h in high_conf_hits]})
                    else:
                        query_text, _signature = build_pattern_query(parsed_data, resolved_data, prompt_data)
                        pattern_hits = self.vector_db_analyzer.retrieve_patterns(query_text, n_results=max_results)
                        vector_used = bool(pattern_hits)
                        pattern_ids = [p.get("pattern_id") for p in pattern_hits if p.get("pattern_id")]
                        for pid in pattern_ids:
                            evidence_map[pid] = self.vector_db_analyzer.get_evidence(pid)
                        strategy_hits = self.vector_db_analyzer.get_fix_strategies(pattern_ids)
                        decision_trace.append({"stage": "vector", "result": "hit" if pattern_hits else "miss", "pattern_ids": pattern_ids})
                    memory_context = self._render_memory_context(rule_hits, pattern_hits, evidence_map, strategy_hits)
                except Exception:
                    pass
            print(f"TOOL_OUTPUT:vector_memory:{json.dumps({'rule_hits': rule_hits, 'pattern_hits': pattern_hits, 'evidence_map': evidence_map, 'strategy_hits': strategy_hits, 'decision_trace': decision_trace, 'vector_used': vector_used, 'memory_context': memory_context}, ensure_ascii=False)}")
            guidance_text = self._get_guidance_for_prompt(
                [h.get("rule_id") for h in rule_hits if h.get("rule_id")],
                [p.get("pattern_id") for p in pattern_hits if p.get("pattern_id")],
                prompt_data,
            )
            ai_prompt = self._build_full_prompt(prompt_data.get("crash_contexts", []), prompt_data.get("code_contexts", []), guidance_text, memory_context)
            print(f"TOOL_OUTPUT:ai_prompt:{json.dumps(ai_prompt, ensure_ascii=False)}")

            total = (datetime.now() - start_time).total_seconds()
            return CrashAnalysisResult(
                crash_log_content=crash_log_content,
                parsed_crash_info=parsed_data,
                resolved_stack_trace=resolved_data,
                code_context_prompt=prompt_data,
                ai_analysis="跳过AI分析，只运行了前三个工具",
                analysis_steps=analysis_steps,
                total_execution_time=total,
                timestamp=datetime.now().isoformat(),
                status="success_without_ai",
                rule_hits=rule_hits,
                pattern_hits=pattern_hits,
                evidence_map=evidence_map,
                strategy_hits=strategy_hits,
                decision_trace=decision_trace,
                vector_used=vector_used,
                memory_context=memory_context,
            )
        except Exception:
            total = (datetime.now() - start_time).total_seconds()
            return CrashAnalysisResult(crash_log_content, {}, {}, {}, "分析失败", analysis_steps, total, datetime.now().isoformat(), "failed")

    def get_analysis_summary(self, result: CrashAnalysisResult) -> str:
        crash_summary = ""
        try:
            crash_summary = str((result.code_context_prompt or {}).get("crash_summary") or "")
        except Exception:
            crash_summary = ""
        lines = ["# 🔍 崩溃分析摘要"]
        if crash_summary:
            lines.append(f"\n## 📋 崩溃摘要\n{crash_summary}")
        lines.append(f"\n## 🤖 分析结果\n{result.ai_analysis or '（无）'}")
        lines.append(f"\n## 📊 状态\n- status: {result.status}\n- total_time: {result.total_execution_time:.2f}s")
        return "\n".join(lines).rstrip() + "\n"

    def perform_consultation(self, prompt: str) -> str:
        return self._perform_ai_analysis(prompt, model=None)

    def get_vector_db_statistics(self) -> Dict[str, Any]:
        if not self.vector_db_analyzer:
            return {"enabled": False, "message": "向量数据库未初始化"}
        try:
            if hasattr(self.vector_db_analyzer, "get_statistics"):
                return self.vector_db_analyzer.get_statistics()
        except Exception as e:
            return {"enabled": True, "error": str(e)}
        return {"enabled": True}
    
    def build_vector_db_record(self, final_state: CrashAnalysisState) -> Optional[Dict[str, Any]]:
        """构建向量数据库记录（仅当有有效的AI分析结果时）"""
        try:
            # 检查是否有有效的AI分析结果
            ai_analysis = final_state.get("ai_analysis") or ""
            if isinstance(ai_analysis, str):
                ai_analysis = ai_analysis.strip()
            else:
                ai_analysis = ""
            
            # 无效的AI分析内容（占位文本或错误信息），这类结果不应该写入 Crash Memory
            invalid_patterns = [
                "AI分析跳过",
                "跳过AI分析",
                "AI分析失败",
                "分析失败",
                "未配置密钥",
                "只运行了前三个工具"
            ]
            
            # 如果AI分析为空或包含无效模式，则不保存
            if not ai_analysis or any(pattern in ai_analysis for pattern in invalid_patterns):
                return None
            
            # 提取崩溃信息
            parsed_data = final_state.get("parsed_data", {})
            resolved_data = final_state.get("resolved_data", {})
            prompt_data = final_state.get("prompt_data", {}) or {}

            crash_info = parsed_data.get("crash_info", {}) if isinstance(parsed_data, dict) else {}
            meta_info = parsed_data.get("meta_info", {}) if isinstance(parsed_data, dict) else {}
            crash_reason = crash_info.get("crash_reason", "unknown") if isinstance(crash_info, dict) else "unknown"
            signal = crash_info.get("signal", "") if isinstance(crash_info, dict) else ""
            os_type = meta_info.get("os_type", "unknown") if isinstance(meta_info, dict) else "unknown"
            platform = meta_info.get("platform") if isinstance(meta_info, dict) else None

            query_text, signature = build_pattern_query(parsed_data, resolved_data, prompt_data)
            pattern_summary = prompt_data.get("crash_summary") or ai_analysis.splitlines()[0]
            pattern_summary = str(pattern_summary).strip()

            crash_category = "unknown"
            if "SIGSEGV" in str(signal) or "segmentation" in str(crash_reason).lower():
                crash_category = "memory"
            elif "deadlock" in str(crash_reason).lower() or "lock" in str(crash_reason).lower():
                crash_category = "concurrency"

            evidence_requirements = []
            if resolved_data.get("resolved_frames"):
                evidence_requirements.append("stack_trace")
            if prompt_data.get("code_contexts"):
                evidence_requirements.append("code_snippet")
            if parsed_data.get("raw_content"):
                evidence_requirements.append("log_fragment")

            pattern_id = f"pattern_{hashlib.md5((pattern_summary + signature + datetime.now().isoformat()).encode()).hexdigest()}"
            pattern = {
                "pattern_id": pattern_id,
                "pattern_summary": pattern_summary or query_text[:200],
                "crash_signature": signature,
                "platform_scope": {
                    "os": os_type,
                    "platform": platform,
                },
                "crash_category": crash_category,
                "evidence_requirements": evidence_requirements,
                "confidence_score": 0.6,
                "validation_state": "draft",
                "source_type": "internal_case",
                "created_at": datetime.now().isoformat(),
            }

            evidence_list = []
            for frame in resolved_data.get("resolved_frames", [])[:5]:
                evidence_list.append({
                    "evidence_id": f"evidence_{hashlib.md5((pattern_id + str(frame)).encode()).hexdigest()}",
                    "pattern_id": pattern_id,
                    "evidence_type": "stack_trace",
                    "raw_content": json.dumps(frame, ensure_ascii=False),
                    "normalized_features": {
                        "function": frame.get("function"),
                        "module": frame.get("module"),
                    },
                    "reliability_score": 0.7,
                    "created_at": datetime.now().isoformat(),
                })

            return {
                "pattern": pattern,
                "evidence": evidence_list,
            }
            
        except Exception as e:
            print(f"WARNING: 构建向量数据库记录失败: {e}", file=sys.stderr)
            return None
    
    def _save_state_to_vector_db(self, final_state: CrashAnalysisState) -> bool:
        """将分析结果保存到向量数据库（仅当有有效的AI分析结果时）"""
        try:
            if not self.vector_db_analyzer:
                return False
            
            payload = self.build_vector_db_record(final_state)
            if not payload:
                return False

            pattern = payload.get("pattern")
            evidence_list = payload.get("evidence") or []
            if not pattern:
                return False
            success = self.vector_db_analyzer.add_pattern(pattern)
            if success:
                for ev in evidence_list:
                    self.vector_db_analyzer.add_evidence(ev)
            return success
            
        except Exception as e:
            print(f"WARNING: 保存分析结果到向量数据库失败: {e}", file=sys.stderr)
            return False
    
    def _perform_ai_analysis(self, prompt_content: str, model: str = None) -> str:
        """执行AI分析（支持流式响应版本）"""
        try:
            # 使用传入的模型参数，如果没有则使用默认模型
            model_to_use = model or self.default_model
            print(f"INFO: 使用AI模型: {model_to_use}", file=sys.stderr)
            
            # provider 选择（默认走 config.llm_config.active_provider）
            provider = self.active_provider
            llm_cfg = self.config.get("llm_config", {}) if isinstance(self.config, dict) else {}
            providers = llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
            provider_defaults = llm_cfg.get("provider_defaults", {}) if isinstance(llm_cfg, dict) else {}
            if not isinstance(provider_defaults, dict):
                provider_defaults = {}
            provider_cfg = (
                {**provider_defaults, **(providers.get(provider, {}) or {})}
                if isinstance(providers, dict) else {}
            )
            cfg_models_any = provider_cfg.get("models", {}) if isinstance(provider_cfg, dict) else {}

            # 处理模型名称格式：provider:model -> model
            if ':' in model_to_use:
                actual_model = model_to_use.split(':', 1)[1]
            else:
                actual_model = model_to_use
            print(f"INFO: 实际模型名称: {actual_model}", file=sys.stderr)

            # 直接使用配置中的 base_url，不自动拼接接口路径
            base_url = provider_cfg.get("base_url") or "https://api.openai.com/v1/chat/completions"
            if str(base_url).endswith("/"):
                base_url = str(base_url)[:-1]

            # 鉴权优先级：环境变量 > 配置文件（provider 配置驱动，兼容历史逻辑）
            auth_type = str(provider_cfg.get("auth_type") or "").strip().lower()
            if not auth_type:
                auth_type = "authorization" if (provider == "baidu_qianfan" or provider_cfg.get("authorization")) else "api_key"
            auth_header = str(provider_cfg.get("auth_header") or "Authorization").strip() or "Authorization"
            auth_prefix = provider_cfg.get("auth_prefix")
            if auth_prefix is None:
                auth_prefix = "Bearer "
            auth_prefix = str(auth_prefix)

            env_key_candidates = []
            custom_env = provider_cfg.get("api_key_env")
            if isinstance(custom_env, list):
                env_key_candidates.extend([str(x).strip() for x in custom_env if str(x).strip()])
            elif isinstance(custom_env, str) and custom_env.strip():
                env_key_candidates.extend([x.strip() for x in custom_env.split(",") if x.strip()])

            if provider == "zhipu_bigmodel":
                env_key_candidates.extend(["ZHIPU_API_KEY", "BIGMODEL_API_KEY"])
            elif provider == "baidu_qianfan":
                env_key_candidates.append("BAIDU_QIANFAN_AUTHORIZATION")
            elif provider == "openai":
                env_key_candidates.append("OPENAI_API_KEY")
            else:
                env_key_candidates.append(f"{provider.upper()}_API_KEY")

            env_secret = next((os.getenv(k) for k in env_key_candidates if os.getenv(k)), None)
            if auth_type == "authorization":
                config_secret = provider_cfg.get("authorization")
            elif auth_type == "none":
                config_secret = ""
            else:
                config_secret = provider_cfg.get("api_key")
            authorization = env_secret or config_secret

            if auth_type != "none":
                if not authorization:
                    raise RuntimeError(
                        f"缺少鉴权：请设置环境变量（建议 {', '.join(env_key_candidates[:3])}）"
                        f"或在 tools/configs/agent_config.local.json 中配置 llm_config.providers.{provider}"
                    )
                if auth_prefix and not str(authorization).startswith(auth_prefix):
                    authorization = f"{auth_prefix}{authorization}"

            # 准备请求头
            headers = {"Content-Type": "application/json"}
            if auth_type != "none":
                headers[auth_header] = authorization
            
            # 动态调整参数以提高性能
            prompt_length = len(prompt_content)
            
            # 根据提示词长度调整max_tokens
            if prompt_length > 8000:
                max_tokens = 4000
                temperature = 0.05
            elif prompt_length > 4000:
                max_tokens = 6000
                temperature = 0.08
            else:
                max_tokens = 8000
                temperature = 0.1

            # 以配置/环境变量为上限，避免过大的 max_tokens 触发 TPM 限流
            cfg_models = cfg_models_any if isinstance(cfg_models_any, dict) else {}
            cfg_model = cfg_models.get(actual_model) or cfg_models.get(model_to_use) or {}
            cfg_max_tokens = cfg_model.get("max_tokens")
            env_max_tokens = os.getenv("AI_STABILITY_QIANFAN_MAX_TOKENS")
            try:
                env_max_tokens_int = int(env_max_tokens) if env_max_tokens else None
            except Exception:
                env_max_tokens_int = None

            upper_bound = None
            if isinstance(cfg_max_tokens, int) and cfg_max_tokens > 0:
                upper_bound = cfg_max_tokens
            if isinstance(env_max_tokens_int, int) and env_max_tokens_int > 0:
                upper_bound = env_max_tokens_int if upper_bound is None else min(upper_bound, env_max_tokens_int)
            if upper_bound is not None:
                max_tokens = min(max_tokens, upper_bound)
            
            # 检查是否启用流式响应
            use_streaming = self.config.get("workflow_config", {}).get("streaming_response", True)
            
            # provider/model 级别参数（智谱/千帆）
            cfg_model_any = cfg_models_any.get(actual_model) or cfg_models_any.get(model_to_use) or {}
            if not isinstance(cfg_model_any, dict):
                cfg_model_any = {}

            # 思考模式（智谱：thinking 对象；千帆：enable_thinking bool）
            enable_thinking = True
            if cfg_model_any.get("enable_thinking") is not None:
                enable_thinking = bool(cfg_model_any.get("enable_thinking"))
            thinking_obj = cfg_model_any.get("thinking")
            if thinking_obj is not None and not isinstance(thinking_obj, dict):
                thinking_obj = None

            # 采样参数（智谱文档：do_sample/top_p/temperature；千帆也兼容 temperature/top_p）
            do_sample = cfg_model_any.get("do_sample")
            top_p = cfg_model_any.get("top_p")
            
            request_format = str(provider_cfg.get("request_format") or "openai_chat_completions_compatible").strip().lower()
            system_prompt = """你是崩溃修复专家。任务：基于实际源代码分析崩溃并提供可直接应用的修复代码。

关键要求：
- 基于提供的实际源代码进行分析
- 禁止使用'未知'、'假设'、'示例'等词汇
- 重点修复崩溃点所在的函数，不要修复其他无关函数
- 提供完整的可编译修复代码
- 修复代码可直接替换原代码
- 保持原有函数签名
- 提供完整的函数实现，不要只提供部分代码
- 修复代码应该可以直接复制粘贴使用
- 必须基于实际代码进行修复，不要创造不存在的函数
- 必须直接修复导致崩溃的代码行

**重要：信息不足时的处理规则**
- 如果提供的上下文信息不足以进行准确分析，**绝对不要强行给出结论或猜测性修复方案**
- 必须明确指出缺少哪些信息，并以结构化JSON格式返回
- 只有在信息充足的情况下，才输出完整的修复方案

可用工具：
- crash_log_parser: 解析崩溃日志，提取堆栈帧和崩溃信息
- add2line_resolver: 解析堆栈地址，将PC地址转换为文件:行号
- code_content_provider: 提取相关源代码上下文
 - log_filter: 过滤和提取特定日志信息"""

            # 常见协议支持：
            # 1) openai_chat_completions_compatible（默认）
            # 2) anthropic_messages_compatible
            # 3) openai_responses_compatible（OpenAI Responses API）
            # 4) minimax_text_chatcompletion_v2_compatible（MiniMax 标准文本接口，结构与 chat-completions 接近）
            if request_format == "anthropic_messages_compatible":
                # Anthropic Messages 不复用 OpenAI 流式解析器，先固定非流式
                use_streaming = False
                headers.setdefault("anthropic-version", str(provider_cfg.get("anthropic_version") or "2023-06-01"))
                data = {
                    "model": actual_model,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": prompt_content}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if top_p is not None:
                    data["top_p"] = top_p
            elif request_format == "openai_responses_compatible":
                # Responses API 的 stream 事件与 chat-completions 不同，先固定非流式
                use_streaming = False
                data = {
                    "model": actual_model,
                    "input": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt_content},
                    ],
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                }
            else:
                data = {
                    "model": actual_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {
                            "role": "user",
                            "content": prompt_content
                        }
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": use_streaming,
                }

                # 不同 provider 的参数兼容：千帆支持 enable_thinking；智谱 BigModel 端不保证该字段可用
                if provider == "baidu_qianfan":
                    data["enable_thinking"] = enable_thinking
                    if top_p is not None:
                        data["top_p"] = top_p
                if provider == "zhipu_bigmodel":
                    # 按智谱文档透传参数
                    if do_sample is not None:
                        data["do_sample"] = bool(do_sample)
                    if top_p is not None:
                        data["top_p"] = top_p
                    if thinking_obj is not None:
                        data["thinking"] = thinking_obj
                    elif enable_thinking is not None:
                        # 兼容旧字段 enable_thinking
                        data["thinking"] = {"type": "enabled" if enable_thinking else "disabled"}
            
            print(f"INFO: 正在调用AI模型 {data['model']} 进行分析...", file=sys.stderr)
            print(f"INFO: 提示词长度: {len(prompt_content)} 字符", file=sys.stderr)
            print(f"INFO: 最大输出tokens: {max_tokens}", file=sys.stderr)
            print(f"INFO: 流式响应: {'启用' if use_streaming else '禁用'}", file=sys.stderr)
            print(f"INFO: 思考模式: {'启用' if enable_thinking else '禁用'}", file=sys.stderr)
            
            # 动态设置超时时间（并允许通过配置覆盖）
            default_timeout = min(120, max(30, prompt_length // 100))
            cfg_timeout = None
            try:
                cfg_timeout = int(cfg_model.get("request_timeout")) if isinstance(cfg_model, dict) and cfg_model.get("request_timeout") is not None else None
            except Exception:
                cfg_timeout = None
            timeout = cfg_timeout if (isinstance(cfg_timeout, int) and cfg_timeout > 0) else default_timeout
            print(f"INFO: 请求超时时间: {timeout}秒", file=sys.stderr)
            
            # 发送请求
            if use_streaming:
                return self._handle_streaming_response(base_url, headers, data, timeout)
            else:
                # 429 限流：短暂退避重试
                response = None
                retry_sleep_s = 2
                for attempt in range(1, 4):
                    response = requests.post(base_url, headers=headers, json=data, timeout=timeout)
                    if response.status_code != 429:
                        break
                    print(f"WARNING: 命中 429 限流，{retry_sleep_s}s 后重试（{attempt}/3）", file=sys.stderr)
                    import time as _time
                    _time.sleep(retry_sleep_s)
                    retry_sleep_s = min(retry_sleep_s * 2, 20)
                if response is None:
                    raise Exception("AI请求失败：未获得响应")
            
            if response.status_code == 200:
                result = response.json()
                content = ""
                if request_format == "anthropic_messages_compatible":
                    blocks = result.get("content") if isinstance(result, dict) else None
                    if isinstance(blocks, list):
                        text_parts = []
                        for blk in blocks:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                text_parts.append(str(blk.get("text") or ""))
                        content = "".join(text_parts).strip()
                elif request_format == "openai_responses_compatible":
                    # 优先使用 output_text；兼容 output[].content[].text
                    output_text = result.get("output_text") if isinstance(result, dict) else None
                    if isinstance(output_text, str) and output_text.strip():
                        content = output_text.strip()
                    elif isinstance(result, dict):
                        output = result.get("output")
                        if isinstance(output, list):
                            text_parts = []
                            for item in output:
                                if not isinstance(item, dict):
                                    continue
                                content_blocks = item.get("content")
                                if not isinstance(content_blocks, list):
                                    continue
                                for blk in content_blocks:
                                    if isinstance(blk, dict) and blk.get("type") in ("output_text", "text"):
                                        text_parts.append(str(blk.get("text") or ""))
                            content = "".join(text_parts).strip()
                else:
                    if "choices" in result and len(result["choices"]) > 0:
                        msg = result["choices"][0].get("message", {}) or {}
                        if isinstance(msg, dict):
                            final_content = (msg.get("content") or "").strip()
                            reasoning_content = (msg.get("reasoning_content") or "").strip()
                            content = final_content or reasoning_content

                if not content:
                    raise Exception(f"AI响应格式异常: {result}")

                print(f"INFO: AI分析完成", file=sys.stderr)
                if isinstance(result, dict) and "usage" in result:
                    usage = result["usage"]
                    print(f"INFO: 使用统计 - 输入tokens: {usage.get('prompt_tokens', 'N/A')}, 输出tokens: {usage.get('completion_tokens', 'N/A')}, 总tokens: {usage.get('total_tokens', 'N/A')}", file=sys.stderr)
                return content
            else:
                raise Exception(f"AI请求失败，状态码: {response.status_code}, 错误: {response.text}")
                
        except Exception as e:
            print(f"ERROR: AI分析执行失败: {e}", file=sys.stderr)
            raise
    
    def _handle_streaming_response(self, base_url: str, headers: dict, data: dict, timeout: int) -> str:
        """处理流式响应"""
        try:
            print("INFO: 开始接收流式响应...", file=sys.stderr)
            
            # 发送流式响应开始信号到VSCode插件
            start_data = {
                "type": "start",
                "model": data.get("model", "unknown"),
                "prompt_length": len(data.get("messages", [{}])[-1].get("content", ""))
            }
            print(f"AI_STREAM_DATA:{json.dumps(start_data, ensure_ascii=False)}", file=sys.stdout)
            sys.stdout.flush()
            
            # 发送流式请求
            # 429 限流：短暂退避重试
            response = None
            retry_sleep_s = 2
            for attempt in range(1, 4):
                response = requests.post(base_url, headers=headers, json=data, timeout=timeout, stream=True)
                if response.status_code != 429:
                    break
                print(f"WARNING: 流式请求命中 429 限流，{retry_sleep_s}s 后重试（{attempt}/3）", file=sys.stderr)
                import time as _time
                _time.sleep(retry_sleep_s)
                retry_sleep_s = min(retry_sleep_s * 2, 20)
            if response is None:
                raise Exception("流式请求失败：未获得响应")
            
            if response.status_code != 200:
                raise Exception(f"流式请求失败，状态码: {response.status_code}, 错误: {response.text}")
            
            # 收集完整的响应内容
            full_content = ""
            chunk_count = 0
            
            # 处理流式响应
            for line in response.iter_lines():
                if line:
                    line_str = line.decode('utf-8')
                    
                    # 跳过空行和注释行
                    if not line_str.strip() or line_str.startswith(':'):
                        continue
                    
                    # 处理SSE格式的数据（兼容 data: 前缀 或 直接 JSON 行）
                    data_content = None
                    if line_str.startswith('data: '):
                        data_content = line_str[6:]
                    else:
                        # 有些实现可能直接输出 JSON 行
                        if line_str.strip().startswith('{') and line_str.strip().endswith('}'):
                            data_content = line_str.strip()
                    if data_content is None:
                        continue

                    # 检查是否是结束标记
                    if data_content.strip() == '[DONE]':
                        print(f"INFO: 流式响应结束，共接收 {chunk_count} 个数据块", file=sys.stderr)
                        break

                    try:
                        # 解析JSON数据
                        chunk_data = json.loads(data_content)

                        # 提取内容
                        if "choices" in chunk_data and len(chunk_data["choices"]) > 0:
                            choice = chunk_data["choices"][0]
                            if "delta" in choice and isinstance(choice["delta"], dict):
                                delta = choice["delta"]
                                # 兼容智谱 BigModel：可能输出 reasoning_content
                                content_delta = delta.get("content") or delta.get("reasoning_content") or ""
                                if content_delta:
                                    full_content += content_delta
                                    chunk_count += 1

                                # 发送流式数据块到VSCode插件
                                stream_chunk = {
                                    "type": "chunk",
                                    "content": content_delta,
                                    "accumulated_length": len(full_content),
                                    "chunk_count": chunk_count,
                                    "is_complete": False
                                }
                                print(f"AI_STREAM_DATA:{json.dumps(stream_chunk, ensure_ascii=False)}", file=sys.stdout)
                                sys.stdout.flush()

                                # 实时输出内容片段（可选）
                                if chunk_count % 10 == 0:
                                    print(f"INFO: 已接收 {chunk_count} 个数据块，当前内容长度: {len(full_content)} 字符", file=sys.stderr)

                        # 显示使用统计
                        if "usage" in chunk_data and chunk_count == 1:
                            usage = chunk_data["usage"]
                            print(f"INFO: 使用统计 - 输入tokens: {usage.get('prompt_tokens', 'N/A')}, 输出tokens: {usage.get('completion_tokens', 'N/A')}, 总tokens: {usage.get('total_tokens', 'N/A')}", file=sys.stderr)

                    except json.JSONDecodeError as e:
                        print(f"WARNING: 解析流式数据块失败: {e}, 数据: {data_content}", file=sys.stderr)
                        continue
            
            # 发送流式响应结束信号到VSCode插件
            end_data = {
                "type": "end",
                "final_content": full_content,
                "total_length": len(full_content),
                "total_chunks": chunk_count
            }
            print(f"AI_STREAM_DATA:{json.dumps(end_data, ensure_ascii=False)}", file=sys.stdout)
            sys.stdout.flush()
            
            print(f"INFO: 流式响应完成，总内容长度: {len(full_content)} 字符", file=sys.stderr)
            return full_content.strip()
            
        except Exception as e:
            print(f"ERROR: 流式响应处理失败: {e}", file=sys.stderr)
            # 如果流式响应失败，回退到普通响应
            print("INFO: 回退到普通响应模式...", file=sys.stderr)
            data["stream"] = False
            # 429 限流：短暂退避重试
            response = None
            retry_sleep_s = 2
            for attempt in range(1, 4):
                response = requests.post(base_url, headers=headers, json=data, timeout=timeout)
                if response.status_code != 429:
                    break
                print(f"WARNING: 回退模式命中 429 限流，{retry_sleep_s}s 后重试（{attempt}/3）", file=sys.stderr)
                import time as _time
                _time.sleep(retry_sleep_s)
                retry_sleep_s = min(retry_sleep_s * 2, 20)
            if response is None:
                raise Exception("回退模式请求失败：未获得响应")
            
            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    print(f"INFO: 回退模式AI分析完成", file=sys.stderr)
                    return content
                else:
                    raise Exception(f"回退模式响应格式异常: {result}")
            else:
                raise Exception(f"回退模式请求失败，状态码: {response.status_code}, 错误: {response.text}")
    
    def _build_full_prompt(self, crash_contexts: list, code_contexts: list, guidance_text: str, memory_context: str = "") -> str:
        """构建完整的AI提示词（优化版本）。指导内容来自 guidance_text（向量库或默认 JSON）。"""
        max_crash_contexts = 3
        max_code_contexts = 2
        max_code_length = 3000
        limited_crash_contexts = crash_contexts[:max_crash_contexts]
        limited_code_contexts = code_contexts[:max_code_contexts]
        full_prompt = f"""
{guidance_text}

## 崩溃上下文信息
"""
        
        # 添加崩溃上下文信息
        for i, ctx in enumerate(limited_crash_contexts):
            full_prompt += f"""
崩溃点 {i+1}:
- 地址: {ctx.get('address', 'N/A')}
- 函数: {ctx.get('resolved_function', 'N/A')}
- 文件: {ctx.get('resolved_file', 'N/A')}
- 行号: {ctx.get('resolved_line', 'N/A')}
- 崩溃原因: {ctx.get('crash_reason', 'N/A')}
- 线程类型: {ctx.get('thread_type', 'N/A')}
"""
        
        # 添加代码上下文信息
        full_prompt += f"""

## 相关代码上下文
"""
        
        for i, code_ctx in enumerate(limited_code_contexts):
            code_content = code_ctx.get('code_snippet', code_ctx.get('surrounding_code', 'N/A'))
            if len(code_content) > max_code_length:
                code_content = code_content[:max_code_length] + "\n... (代码已截断)"
            
            full_prompt += f"""
代码文件 {i+1}: {code_ctx.get('file_path', 'N/A')}:{code_ctx.get('line_number', 'N/A')}
函数: {code_ctx.get('function_name', 'N/A')}
代码内容:
{code_content}
"""
        
        # 添加历史相似案例信息
        if memory_context:
            full_prompt += memory_context
        
        # 添加输出格式要求（支持结构化输出）
        full_prompt += f"""

## 输出格式要求

### 情况1：信息充足，可以进行分析
如果提供的上下文信息充足，请按照以下格式输出崩溃修复方案：

### 崩溃分析
- 崩溃原因：[具体原因]
- 崩溃位置：[文件:行号]
- 影响范围：[影响的功能模块]

### 问题分析
- 根本原因：[详细分析]
- 触发条件：[什么情况下会触发]
- 风险等级：[高/中/低]

### 具体修复方案
**必须提供以下内容：**

#### 1. 需要修改的文件和函数
- 文件1：[具体文件路径]
  - 函数1：[具体函数名] - [修改原因]
  - 函数2：[具体函数名] - [修改原因]

#### 2. 具体的代码修改
**对于每个需要修改的函数，提供完整的修改后代码：**

```cpp
// 文件：[具体文件路径]
// 函数：[具体函数名]
// 修改说明：[为什么这样修改]

[完整的修改后函数代码，可以直接替换原代码]
```

#### 3. 修改说明
- 修改原理：[为什么这样修改]
- 关键改动：[具体改动了哪些地方]
- 注意事项：[需要注意的问题]

### 验证方法
- 编译测试：[如何验证代码能正常编译]
- 功能测试：[如何验证修复效果]
- 压力测试：[如何验证多线程安全性]

---

### 情况2：信息不足，需要更多上下文
**如果提供的上下文信息不足以进行准确分析，请严格按照以下JSON格式输出，不要输出其他内容：**

```json
{{
  "status": "need_more_context",
  "missing": [
    "function_source_code",
    "resolved_stack_trace",
    "crash_log_details",
    "surrounding_code_context"
  ],
  "suggested_tools": [
    "add2line_resolver",
    "code_content_provider",
    "crash_log_parser"
  ],
  "reason": "Current stack trace contains unresolved PC addresses. Need resolved file:line information to analyze the crash location.",
  "details": {{
    "function_source_code": "需要崩溃函数的完整源代码",
    "resolved_stack_trace": "需要解析后的堆栈地址（文件:行号）",
    "crash_log_details": "需要更详细的崩溃日志信息"
  }}
}}
```

**missing 字段可选值：**
- "function_source_code": 缺少函数源代码
- "resolved_stack_trace": 缺少解析后的堆栈信息（文件:行号）
- "crash_log_details": 缺少崩溃日志详细信息
- "surrounding_code_context": 缺少周围代码上下文
- "variable_values": 缺少变量值信息
- "thread_context": 缺少线程上下文信息
- "call_stack": 缺少完整调用栈

**suggested_tools 字段可选值：**
- "crash_log_parser": 崩溃日志解析工具
- "add2line_resolver": 堆栈地址解析工具
- "code_content_provider": 代码上下文提取工具
- "log_filter": 日志过滤工具

**重要规则：**
1. 如果信息不足，**必须**使用情况2的JSON格式输出
2. 不要强行给出猜测性的修复方案
3. 在 reason 字段中详细说明为什么需要这些信息
4. 在 details 字段中说明每个缺失信息的具体用途

## 重要要求
1. **必须提供完整的、可编译的代码**，不是建议或示例
2. **代码必须基于提供的实际源代码进行修改**
3. **修改后的代码应该可以直接复制粘贴使用**
4. **不要使用"建议"、"应该"等词汇，直接给出修改方案**
5. **如果没有发现明显的空指针问题，优先考虑多线程竞争问题**
6. **确保修改后的代码语法正确，可以直接编译运行**
7. **如果信息不足，优先使用结构化JSON格式请求更多信息，而不是强行分析**

请确保输出内容结构清晰，每个部分都有明确的标题和具体的代码修改。
"""
        
        return full_prompt
    
    def _render_memory_context(
        self,
        rule_hits: list,
        pattern_hits: list,
        evidence_map: Dict[str, Any],
        strategy_hits: list,
    ) -> str:
        if not rule_hits and not pattern_hits and not strategy_hits:
            return ""
        # 该段会直接拼入发给大模型的提示词：优先紧凑、可读、易理解，减少无用元信息。
        # 不在返回文本中添加前导空行，避免在提示词中形成过大的空白间隔。
        lines: list[str] = ["## 规则与经验模式参考"]
        if rule_hits:
            lines.append("")
            lines.append("### 规则命中（确定性）")
            for r in rule_hits[:5]:
                name = r.get("rule_name") or r.get("rule_id")
                payload = r.get("conclusion_payload")
                if name:
                    lines.append(f"- {name}")
                if payload:
                    lines.append(f"  - 结论要点: {json.dumps(payload, ensure_ascii=False)}")
        if pattern_hits:
            lines.append("")
            lines.append("### 经验模式召回（向量）")
            for p in pattern_hits[:5]:
                summary = p.get("pattern_summary") or p.get("pattern_id")
                signature = p.get("crash_signature")
                if summary:
                    lines.append(f"- {summary}")
                ev = (evidence_map or {}).get(p.get("pattern_id")) or []
                if ev:
                    lines.append(f"  - 证据条目: {len(ev)}")
                if signature:
                    lines.append(f"  - 语义签名: {signature}")
        if strategy_hits:
            lines.append("")
            lines.append("### 修复策略候选（非结论）")
            for s in strategy_hits[:5]:
                intent = s.get("fix_intent")
                if intent:
                    lines.append(f"- {intent}")
        lines.append("")
        lines.append("注意：规则与向量召回仅作为推理依据，不代表最终结论。")
        return "\n".join(lines)

    def _load_default_guidance_blocks(self) -> List[Dict[str, Any]]:
        """Load default guidance blocks from JSON (fallback when no vector DB or no hits)."""
        candidates = [
            Path(__file__).resolve().parents[3] / "tools" / "configs" / "default_guidance_blocks.json",
            Path.cwd() / "configs" / "default_guidance_blocks.json",
        ]
        for path in candidates:
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
        return []

    def _get_guidance_for_prompt(
        self, rule_ids: List[str], pattern_ids: List[str], prompt_data: Dict[str, Any]
    ) -> str:
        """Build guidance text from vector DB blocks or default JSON; substitute placeholders."""
        blocks: List[Dict[str, Any]] = []
        if self.vector_db_analyzer:
            try:
                blocks = self.vector_db_analyzer.get_guidance_blocks(rule_ids, pattern_ids)
            except Exception:
                pass
        if not blocks:
            blocks = self._load_default_guidance_blocks()
        if not blocks:
            return ""
        parts = [b.get("content") or "" for b in blocks]
        text = "\n\n".join(parts)
        crash_function_name = "崩溃函数"
        if isinstance(prompt_data.get("crash_func"), dict):
            crash_function_name = prompt_data["crash_func"].get("name", crash_function_name)
        elif isinstance(prompt_data.get("crash_summary"), dict):
            crash_function_name = prompt_data["crash_summary"].get("function", crash_function_name)
        related_fun = prompt_data.get("related_fun") or []
        if related_fun:
            names = [f.get("name", "") for f in related_fun[:3] if isinstance(f, dict)]
            related_funcs_desc = "、".join([f"`{n}`" for n in names if n])
            if len(related_fun) > 3:
                related_funcs_desc += " 等"
        else:
            related_funcs_desc = "同一类中的其他函数"
        text = text.replace("{{crash_function_name}}", crash_function_name)
        text = text.replace("{{related_funcs_desc}}", related_funcs_desc)
        return text

    def _check_if_need_more_context(self, ai_response: str) -> tuple[bool, str]:
        """
        检查AI响应是否需要更多上下文（支持结构化JSON格式）
        
        Returns:
            (need_more_context: bool, context_request: str) - context_request 是 JSON 字符串
        """
        import re
        
        # 首先尝试解析JSON格式的响应
        try:
            # 尝试提取JSON部分（可能在代码块中）
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', ai_response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 尝试直接解析整个响应是否为JSON
                json_str = ai_response.strip()
            
            parsed = json.loads(json_str)
            
            # 检查是否是 need_more_context 格式
            if isinstance(parsed, dict) and parsed.get("status") == "need_more_context":
                missing = parsed.get("missing", [])
                suggested_tools = parsed.get("suggested_tools", [])
                reason = parsed.get("reason", "需要更多上下文信息")
                details = parsed.get("details", {})
                
                # 构建结构化的上下文请求
                context_request = {
                    "status": "need_more_context",
                    "missing": missing,
                    "suggested_tools": suggested_tools,
                    "reason": reason,
                    "details": details
                }
                
                print(f"INFO: AI以结构化格式请求更多上下文: {json.dumps(context_request, ensure_ascii=False, indent=2)}", file=sys.stderr)
                return True, json.dumps(context_request, ensure_ascii=False)
        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            # 如果不是JSON格式，继续使用原有的关键词检测方法
            pass
        
        # 回退到原有的关键词检测方法
        need_context_keywords = [
            "缺乏", "缺少", "需要更多", "需要额外", "信息不足", "上下文不足",
            "无法确定", "无法分析", "需要查看", "需要了解", "需要获取",
            "insufficient", "lack", "need more", "need additional", "cannot determine"
        ]
        
        ai_response_lower = ai_response.lower()
        need_more = any(keyword in ai_response_lower for keyword in need_context_keywords)
        
        # 提取具体的上下文需求
        context_request = ""
        if need_more:
            # 尝试提取AI请求的具体内容
            patterns = [
                r"需要(?:更多|额外)?(?:的)?(?:上下文|信息|代码|日志|堆栈)?[：:](.*?)(?:\n|$)",
                r"缺乏(?:.*?)[：:](.*?)(?:\n|$)",
                r"需要查看(.*?)(?:\n|$)",
            ]
            for pattern in patterns:
                match = re.search(pattern, ai_response, re.IGNORECASE)
                if match:
                    context_request = match.group(1).strip()
                    break
            
            if not context_request:
                context_request = "AI请求更多上下文信息"
            
            # 转换为结构化格式
            context_request_dict = {
                "status": "need_more_context",
                "missing": ["unknown"],  # 无法从文本中提取具体缺失项
                "suggested_tools": [],  # 无法从文本中提取具体工具
                "reason": context_request,
                "details": {}
            }
            context_request = json.dumps(context_request_dict, ensure_ascii=False)
        
        return need_more, context_request


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Stability Analysis Agent - 完整版本（LangGraph）")
    parser.add_argument('--crash-log', type=str, required=True, help='崩溃日志内容或文件路径')
    parser.add_argument('--library-dir', type=str, required=True, help='库文件目录')
    parser.add_argument('--code-root', action='append', dest='code_roots', required=True, metavar='DIR',
                        help='代码根目录，可重复指定多个')
    parser.add_argument('--config', type=str, help='配置文件路径')
    parser.add_argument('--model', type=str, help='指定AI模型')
    
    args = parser.parse_args()
    
    # 打印调试信息
    print(f"INFO: 命令行参数解析完成", file=sys.stderr)
    print(f"INFO: 崩溃日志: {'stdin' if args.crash_log == '-' else args.crash_log}", file=sys.stderr)
    print(f"INFO: 库目录: {args.library_dir}", file=sys.stderr)
    print(f"INFO: 代码根目录: {args.code_roots}", file=sys.stderr)
    print(f"INFO: 配置文件: {args.config}", file=sys.stderr)
    print(f"INFO: 指定模型: {args.model}", file=sys.stderr)
    
    # 加载配置
    config_dict = None
    if args.config:
        try:
            with open(args.config, 'r', encoding='utf-8') as f:
                config_dict = json.load(f)
        except Exception as e:
            print(f"WARNING: 配置文件加载失败: {e}", file=sys.stderr)
    
    # 读取崩溃日志
    crash_log_content = args.crash_log
    if args.crash_log == '-':
        crash_log_content = sys.stdin.read()
    elif Path(args.crash_log).exists():
        with open(args.crash_log, 'r', encoding='utf-8') as f:
            crash_log_content = f.read()
    
    # 创建分析器并执行分析
    seen: set[str] = set()
    roots: List[str] = []
    for r in args.code_roots or []:
        if not r or not str(r).strip():
            continue
        a = os.path.abspath(os.path.expanduser(str(r).strip()))
        if a not in seen:
            seen.add(a)
            roots.append(a)
    analyzer = FullStabilityAnalyzer(config_dict)
    analyzer.analyze_crash_with_ai(crash_log_content, roots, args.library_dir, args.model)


def generate_code_modifications(ai_response: str, code_contexts: list) -> str:
    """生成代码修改建议"""
    if not _REPORT_AVAILABLE:
        return json.dumps({"success": False, "error": "report 模块不可用（report/ 目录未安装）", "modifications": []}, ensure_ascii=False, indent=2)
    try:
        print("INFO: 开始生成代码修改建议...", file=sys.stderr)
        code_modifier = CodeModifier()
        result = code_modifier.code_modifier(ai_response, code_contexts, auto_confirm=False)
        print("INFO: 代码修改建议生成完成", file=sys.stderr)
        return result
    except Exception as e:
        print(f"ERROR: 生成代码修改建议失败: {e}", file=sys.stderr)
        return json.dumps({
            "success": False,
            "error": str(e),
            "modifications": []
        }, ensure_ascii=False, indent=2)


def run_code_check(code_root: str, modified_files: list = None) -> str:
    """运行代码检查"""
    if not _REPORT_AVAILABLE:
        return json.dumps({"success": False, "error": "report 模块不可用（report/ 目录未安装）", "results": []}, ensure_ascii=False, indent=2)
    try:
        print("INFO: 开始运行代码检查...", file=sys.stderr)
        code_checker = CodeChecker()
        result = code_checker.code_check(code_root, modified_files)
        print("INFO: 代码检查完成", file=sys.stderr)
        return result
    except Exception as e:
        print(f"ERROR: 代码检查失败: {e}", file=sys.stderr)
        return json.dumps({
            "success": False,
            "error": str(e),
            "results": []
        }, ensure_ascii=False, indent=2)


def generate_report(crash_info: dict, analysis_result: dict,
                   modifications: list = None, check_results: list = None,
                   metadata: dict = None, filename: str = None) -> str:
    """生成分析报告"""
    if not _REPORT_AVAILABLE:
        return json.dumps({"success": False, "error": "report 模块不可用（report/ 目录未安装）", "report_path": None}, ensure_ascii=False, indent=2)
    try:
        print("INFO: 开始生成分析报告...", file=sys.stderr)
        report_generator = ReportGenerator()
        result = report_generator.report_generator(
            crash_info, analysis_result, modifications, check_results, metadata, filename
        )
        print("INFO: 分析报告生成完成", file=sys.stderr)
        return result
    except Exception as e:
        print(f"ERROR: 生成分析报告失败: {e}", file=sys.stderr)
        return json.dumps({
            "success": False,
            "error": str(e),
            "report_path": None
        }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
