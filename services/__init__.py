"""Analysis acceleration services (code file index, ctags symbol index, code locator, code fixer)."""

from services.code_index_service import CodeIndexMultiRoot, get_code_index_for_roots
from services.ctags_function_index import CtagsFunctionIndex, get_ctags_index_for_roots, warm_ctags_index_for_roots
from services.code_locator import (
    CodeLocatorService,
    LocatorConfig,
    LocatorContext,
    FileLocator,
    SymbolLocator,
    CallerLocator,
    VariableLocator,
    CallChainFunction,
    VariableFunction,
    FindSourceFileTimeout,
    CodeContextPhaseTimeout,
)
from services.code_fixer import (
    CodeFixer,
    FixResult,
    extract_candidate_nodes,
    graph_auto_fix_allowed,
    signatures_match,
    is_forbidden_patch,
    evaluate_fix_apply_success,
    parse_json_payload,
)
from services.verification import (
    VerificationRequest,
    VerificationCandidate,
    VerificationCapabilities,
    VerificationResult,
    VerificationProvider,
    NoopVerificationProvider,
    CommandVerificationProvider,
    create_verification_provider,
    discover_verification_candidates,
    make_approval,
    approval_is_valid,
)
from services.runtime_actions import (
    ACTION_NAMES,
    ApprovalBinding,
    RuntimeAction,
    RuntimeActionExecutor,
    VERIFICATION_ACTION_TOOLS,
    pending_tool_action_name,
)
from services.policy import PolicyDecision, PolicyEngine
from services.evidence_store import EvidenceItem, EvidenceStore, EvidenceContextManager
from services.evidence_ingest import (
    ingest_parse,
    ingest_symbolize,
    ingest_diagnosis,
    ingest_code_context,
    ingest_memory_context,
    ingest_pipeline_stages,
    normalize_diagnosis_for_evaluation,
)
from services.crash_repo_map import CrashRepoMap, RepoMapEntry, RepoMapSnapshot, render_repo_map
from services.decide_scorer import score_repair_decision
from services.evaluation import EvaluationResult, evaluate_case, evaluate_report_dir
from services.diff_review import DiffReview, review_changed_files
from services.verification import verification_observation
from services.verification_profile import VerificationProfile, VerificationCheck, normalize_verification_config
from services.verification_baseline import compare_verification_runs
from services.verification_plan import (
    VerificationClaim, VerificationPlan, VerificationCapability, ReproductionPlan,
    build_verification_plan, build_reproduction_plan, capabilities_from_profile,
)
from services.context_compactor import ContextCompactor, CompactedContext
from services.file_context_tracker import FileContextTracker, FileContextRecord, content_fingerprint
from services.action_failures import FAILURE_CLASSES, normalize_action_result
from services.code_evidence_index import CodeEvidenceIndex, IndexSnapshot, IndexCandidate
from services.crash_evidence_retriever import CrashEvidenceRetriever
from services.investigation_controller import InvestigationAction, InvestigationController, InvestigationState
from services.repair_context_bundle import RepairContextBundle, build_repair_context_bundle
from services.evidence_graph import EvidenceGraph
from services.external_agent_evaluation import build_external_agent_comparison, build_external_agent_evaluation
from services.context_parts import ContextPart, parts_from_evidence
from services.action_security import ActionSecurityAnalyzer, SecurityDecision
from services.workspace_revision import workspace_revisions
from services.repository_evidence import RepositoryEvidenceService
from services.agent_schema import AgentDecision, ContextRequest, RepairPlan, VerificationDecision
from services.repair_pipeline import (
    RepairPipelineResult,
    run_repair_pipeline,
    should_run_repair_pipeline,
    resume_verification_from_report,
    resume_tool_approval_from_report,
    unisolated_workspace_fingerprint,
)
from services.post_fix_diagnosis import run_post_fix_diagnosis, run_post_fix_diagnosis_from_request
from services.git_worktree_manager import (
    IsolatedCodeWorkspace,
    WorktreeIsolationError,
    prepare_isolated_workspace,
    write_workspace_artifacts,
    map_original_path,
    map_result_paths,
    sync_verified_files_back,
    cleanup_isolated_workspace,
    scan_worktree_runs,
    isolated_workspace_from_dict,
)

__all__ = [
    "CodeIndexMultiRoot",
    "get_code_index_for_roots",
    "CrashRepoMap",
    "RepoMapEntry",
    "RepoMapSnapshot",
    "render_repo_map",
    "CtagsFunctionIndex",
    "get_ctags_index_for_roots",
    "warm_ctags_index_for_roots",
    "CodeLocatorService",
    "LocatorConfig",
    "LocatorContext",
    "FileLocator",
    "SymbolLocator",
    "CallerLocator",
    "VariableLocator",
    "CallChainFunction",
    "VariableFunction",
    "FindSourceFileTimeout",
    "CodeContextPhaseTimeout",
    "CodeFixer",
    "FixResult",
    "extract_candidate_nodes",
    "graph_auto_fix_allowed",
    "signatures_match",
    "is_forbidden_patch",
    "evaluate_fix_apply_success",
    "parse_json_payload",
    "VerificationRequest",
    "VerificationCandidate",
    "VerificationCapabilities",
    "VerificationResult",
    "VerificationProvider",
    "NoopVerificationProvider",
    "CommandVerificationProvider",
    "create_verification_provider",
    "discover_verification_candidates",
    "make_approval",
    "approval_is_valid",
    "ACTION_NAMES", "RuntimeAction", "RuntimeActionExecutor", "ApprovalBinding",
    "PolicyDecision",
    "PolicyEngine",
    "EvidenceItem",
    "EvidenceStore",
    "EvidenceContextManager",
    "EvaluationResult",
    "evaluate_case", "evaluate_report_dir",
    "DiffReview",
    "review_changed_files",
    "RepositoryEvidenceService",
    "verification_observation",
    "VerificationProfile", "VerificationCheck", "normalize_verification_config",
    "compare_verification_runs",
    "VerificationClaim", "VerificationPlan", "VerificationCapability", "ReproductionPlan",
    "build_verification_plan", "build_reproduction_plan", "capabilities_from_profile",
    "ContextCompactor", "CompactedContext", "ActionSecurityAnalyzer", "SecurityDecision",
    "FileContextTracker", "FileContextRecord", "content_fingerprint",
    "FAILURE_CLASSES", "normalize_action_result",
    "CodeEvidenceIndex", "IndexSnapshot", "IndexCandidate", "CrashEvidenceRetriever",
    "InvestigationAction", "InvestigationController", "InvestigationState",
    "RepairContextBundle", "build_repair_context_bundle",
    "EvidenceGraph",
    "build_external_agent_evaluation",
    "build_external_agent_comparison",
    "ContextPart", "parts_from_evidence",
    "workspace_revisions",
    "AgentDecision",
    "ContextRequest",
    "RepairPlan",
    "VerificationDecision",
    "RepairPipelineResult",
    "run_repair_pipeline",
    "should_run_repair_pipeline",
    "resume_verification_from_report",
    "resume_tool_approval_from_report",
    "unisolated_workspace_fingerprint",
    "run_post_fix_diagnosis",
    "run_post_fix_diagnosis_from_request",
    "IsolatedCodeWorkspace",
    "WorktreeIsolationError",
    "prepare_isolated_workspace",
    "write_workspace_artifacts",
    "map_original_path",
    "map_result_paths",
    "sync_verified_files_back",
    "cleanup_isolated_workspace",
    "scan_worktree_runs",
    "isolated_workspace_from_dict",
]
