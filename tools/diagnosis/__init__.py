"""Shared diagnosis infrastructure used by all stability analyzers."""

from .models import DiagnosisResult, EvidenceItem, KnowledgeEntry
from .knowledge import KnowledgeRegistry, default_registry, register_builtin_knowledge
from .repair_gate import RepairDecision, evaluate_repair_gate
from .report import build_report_manifest, write_report_manifest
from .evidence_tier import EvidenceTier, assign_tier, resolve_conflicts, annotate_evidence_chain
from .knowledge_loader import load_yaml_knowledge, merge_into_registry
from .feature_loader import detect_features, resolve_features_to_files, load_conditional_knowledge, FeatureReferenceMap

__all__ = [
    "DiagnosisResult",
    "EvidenceItem",
    "KnowledgeEntry",
    "KnowledgeRegistry",
    "default_registry",
    "register_builtin_knowledge",
    "RepairDecision",
    "evaluate_repair_gate",
    "build_report_manifest",
    "write_report_manifest",
    "EvidenceTier",
    "assign_tier",
    "resolve_conflicts",
    "annotate_evidence_chain",
    "load_yaml_knowledge",
    "merge_into_registry",
    "detect_features",
    "resolve_features_to_files",
    "load_conditional_knowledge",
    "FeatureReferenceMap",
]
