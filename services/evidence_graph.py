"""Small provenance graph for crash investigation evidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional


def _id(kind: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "%s_%s" % (kind, hashlib.sha256(raw).hexdigest()[:16])


@dataclass
class EvidenceGraph:
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edges: List[Dict[str, Any]] = field(default_factory=list)

    def add_node(self, kind: str, value: Any, **metadata: Any) -> str:
        node_id = _id(str(kind), value)
        node = {"id": node_id, "kind": str(kind), "value": value}
        node.update({key: value for key, value in metadata.items() if value is not None})
        self.nodes.setdefault(node_id, node)
        return node_id

    def add_edge(self, source: str, relation: str, target: str, **metadata: Any) -> None:
        if not source or not target or source not in self.nodes or target not in self.nodes:
            return
        edge = {"source": source, "relation": str(relation), "target": target}
        edge.update({key: value for key, value in metadata.items() if value is not None})
        if edge not in self.edges:
            self.edges.append(edge)

    def link(self, source_kind: str, source: Any, relation: str, target_kind: str,
             target: Any, **metadata: Any) -> None:
        source_id = self.add_node(source_kind, source)
        target_id = self.add_node(target_kind, target)
        self.add_edge(source_id, relation, target_id, **metadata)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, "nodes": list(self.nodes.values()), "edges": list(self.edges)}

    @classmethod
    def from_dict(cls, value: Any) -> "EvidenceGraph":
        payload = value if isinstance(value, Mapping) else {}
        graph = cls()
        for node in payload.get("nodes") or []:
            if isinstance(node, Mapping) and node.get("id"):
                graph.nodes[str(node["id"])] = dict(node)
        graph.edges = [dict(edge) for edge in payload.get("edges") or [] if isinstance(edge, Mapping)]
        return graph


def append_graph_observation(payload: Any, kind: str, value: Any, *, relation: str = "informs",
                             hypothesis_ids: Optional[List[str]] = None, **metadata: Any) -> Dict[str, Any]:
    """Append a result node to a serialized graph without requiring a live ContextEngine."""
    graph = EvidenceGraph.from_dict(payload)
    node_id = graph.add_node(kind, value, **metadata)
    wanted = set(str(item) for item in (hypothesis_ids or []))
    for node in list(graph.nodes.values()):
        if node.get("kind") != "hypothesis":
            continue
        node_value = node.get("value") if isinstance(node.get("value"), Mapping) else {}
        if wanted and str(node_value.get("id") or "") not in wanted:
            continue
        graph.add_edge(node_id, relation, str(node.get("id")), **metadata)
    return graph.to_dict()
