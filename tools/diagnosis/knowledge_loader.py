#!/usr/bin/env python3
"""Load fault mode knowledge from external YAML files in the knowledge/ directory.

This enables stability experts to maintain knowledge bases without modifying code.
Falls back silently to Python definitions if YAML files are missing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import KnowledgeEntry

logger = logging.getLogger(__name__)

# Resolve knowledge/ directory relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_DIR = _PROJECT_ROOT / "knowledge"


def _safe_load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    """Load a YAML file safely; return None if pyyaml is missing or file doesn't exist."""
    if not path.is_file():
        return None
    try:
        import yaml
    except ImportError:
        logger.debug("pyyaml not installed; skipping YAML knowledge loading")
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Failed to load YAML knowledge %s: %s", path, exc)
        return None


def load_yaml_knowledge(domain: str, filename: str = "fault_modes.yaml",
                        knowledge_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load fault mode entries from a domain-specific YAML file.

    Args:
        domain: The domain subdirectory name (e.g., 'cpp_crash', 'appfreeze')
        filename: The YAML filename within the domain directory
        knowledge_dir: Override the default knowledge directory

    Returns:
        List of fault mode dicts, or empty list if file doesn't exist or is invalid.
    """
    base = knowledge_dir or KNOWLEDGE_DIR
    path = base / domain / filename
    data = _safe_load_yaml(path)
    if not data or not isinstance(data.get("fault_modes"), list):
        return []
    return data["fault_modes"]


def load_yaml_as_entries(domain: str, filename: str = "fault_modes.yaml",
                         knowledge_dir: Optional[Path] = None) -> List[KnowledgeEntry]:
    """Load YAML fault modes and convert to KnowledgeEntry objects.

    Maps YAML fields to KnowledgeEntry fields:
    - id -> id
    - name -> root_cause
    - domain (from file header or function arg) -> domain
    - level_2 or owner -> module
    - evidence_patterns -> evidence_patterns
    - guidance -> guidance
    """
    modes = load_yaml_knowledge(domain, filename, knowledge_dir)
    entries: List[KnowledgeEntry] = []
    for mode in modes:
        if not mode.get("id"):
            continue
        entries.append(KnowledgeEntry(
            id=mode["id"],
            domain=domain,
            module=str(mode.get("level_2") or mode.get("owner") or ""),
            root_cause=str(mode.get("name") or mode.get("id")),
            evidence_patterns=mode.get("evidence_patterns") or [],
            guidance=mode.get("guidance") or [],
            source="yaml",
        ))
    return entries


def load_all_domain_knowledge(knowledge_dir: Optional[Path] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Load fault modes from all domain subdirectories.

    Returns:
        Dict mapping domain name to list of fault mode dicts.
    """
    base = knowledge_dir or KNOWLEDGE_DIR
    if not base.is_dir():
        return {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    for subdir in sorted(base.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        modes = load_yaml_knowledge(subdir.name, knowledge_dir=base)
        if modes:
            result[subdir.name] = modes
    return result


def merge_into_registry(registry: Any, domain: Optional[str] = None,
                        knowledge_dir: Optional[Path] = None) -> int:
    """Load YAML entries and merge into a KnowledgeRegistry.

    YAML entries override existing entries with the same id.

    Args:
        registry: Target KnowledgeRegistry to merge into
        domain: If specified, only load this domain. Otherwise load all.
        knowledge_dir: Override the default knowledge directory

    Returns:
        Number of entries merged.
    """
    count = 0
    if domain:
        entries = load_yaml_as_entries(domain, knowledge_dir=knowledge_dir)
        for entry in entries:
            registry.register(entry)
            count += 1
    else:
        base = knowledge_dir or KNOWLEDGE_DIR
        all_domains = load_all_domain_knowledge(base)
        for dom, modes in all_domains.items():
            for mode in modes:
                if not mode.get("id"):
                    continue
                entry = KnowledgeEntry(
                    id=mode["id"],
                    domain=dom,
                    module=str(mode.get("level_2") or mode.get("owner") or ""),
                    root_cause=str(mode.get("name") or mode.get("id")),
                    evidence_patterns=mode.get("evidence_patterns") or [],
                    guidance=mode.get("guidance") or [],
                    source="yaml",
                )
                registry.register(entry)
                count += 1
    return count
