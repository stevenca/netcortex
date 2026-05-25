"""Content hashing for incremental Neo4j writes.

Each node/edge gets a sha1 of its canonical payload.  At ingest time we look
up the existing `_content_hash` on the node/edge in Neo4j and skip the
SET if it matches — turning a no-op cycle into a cheap MATCH instead of
a write.

Only a stable subset of the payload is hashed (id, type, properties).
`source_adapter`, `dimensions`, and `netbox_id`/`netbox_type` are included
because they materially affect rendering and queries.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from netcortex.graph.models import GraphEdge, GraphNode


def _canon(obj: Any) -> str:
    """Deterministic JSON serialization for hashing."""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def node_hash(node: GraphNode) -> str:
    """Stable content hash for a node."""
    payload = {
        "id": node.id,
        "type": node.type.value,
        "props": node.properties or {},
        "dims": sorted(d.value for d in node.dimensions or []),
        "src": node.source_adapter or "",
        "nb_id": node.netbox_id,
        "nb_type": node.netbox_type,
    }
    return hashlib.sha1(_canon(payload).encode("utf-8")).hexdigest()


def edge_hash(edge: GraphEdge) -> str:
    """Stable content hash for an edge."""
    payload = {
        "src": edge.source_id,
        "dst": edge.target_id,
        "type": edge.type.value,
        "props": edge.properties or {},
        "dim": edge.dimension.value if edge.dimension else None,
        "src_adp": edge.source_adapter or "",
    }
    return hashlib.sha1(_canon(payload).encode("utf-8")).hexdigest()
