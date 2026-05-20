"""
scripts/temporal/create_temporal_graph.py
Create (or recreate) the IC_Temporal_Knowledge_Graph named graph in ArangoDB.

Run from project root:
    PYTHONPATH=src python3 scripts/temporal/create_temporal_graph.py

Run anytime to rebuild the graph definition after schema changes.
Existing graph is deleted and recreated — edge/vertex data is untouched.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from dotenv import load_dotenv
load_dotenv()

from arango import ArangoClient
from config_temporal import TEMPORAL_GRAPH_NAME, ARANGO_DATABASE

def create_graph(db):
    cols = {c["name"] for c in db.collections() if not c["name"].startswith("_")}

    # Restrict GraphRAG collection discovery to our known repo prefixes to
    # avoid accidentally absorbing foreign *_Entities/*_Relations/etc.
    # collections that a managed cluster may pre-seed (observed on fresh
    # ArangoGraph Pilot databases, e.g. a stray RISC1200_* graph).
    try:
        import yaml  # noqa: WPS433
        registry_path = os.path.join(
            os.path.dirname(__file__), "..", "multi_repo", "repo_registry.yaml"
        )
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = yaml.safe_load(f) or {}
        prefixes = tuple(
            r["prefix"] for r in (registry.get("repos") or []) if r.get("prefix")
        )
    except Exception:
        prefixes = ("OR1200_", "MOR1KX_", "MAROCCHINO_", "IBEX_")

    def _ours(c: str) -> bool:
        return c.startswith(prefixes)

    golden_entity_cols = sorted(c for c in cols if _ours(c) and c.endswith("_Golden_Entities"))
    entity_cols        = sorted(c for c in cols if _ours(c) and c.endswith("_Entities"))
    community_cols     = sorted(c for c in cols if _ours(c) and c.endswith("_Communities"))
    chunk_cols         = sorted(c for c in cols if _ours(c) and c.endswith("_Chunks"))
    relation_cols      = sorted(c for c in cols if _ours(c) and c.endswith("_Relations") and not c.endswith("_Golden_Relations"))
    golden_rel_cols    = sorted(c for c in cols if _ours(c) and c.endswith("_Golden_Relations"))
    doc_cols           = sorted(c for c in cols if _ours(c) and c.endswith("_Documents"))

    rtl_vertex_cols = [c for c in ["RTL_Module", "RTL_Port", "RTL_Signal",
                                    "RTL_Parameter", "RTL_LogicChunk"] if c in cols]

    cross_vertex_cols = [c for c in golden_entity_cols + ["RTL_Module"] if c in cols]

    edge_definitions = [
        # ── Core temporal traversal ──────────────────────────────────────
        {
            "edge_collection": "MODIFIED",
            "from_vertex_collections": [c for c in ["GitCommit"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Module"] if c in cols],
        },
        {
            "edge_collection": "BELONGS_TO_EPOCH",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["DesignEpoch"] if c in cols],
        },
        {
            "edge_collection": "PRECEDES_EPOCH",
            "from_vertex_collections": [c for c in ["DesignEpoch"] if c in cols],
            "to_vertex_collections":   [c for c in ["DesignEpoch"] if c in cols],
        } if "PRECEDES_EPOCH" in cols else None,
        # ── RTL structural edges ─────────────────────────────────────────
        {
            "edge_collection": "HAS_PORT",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Port"] if c in cols],
        },
        {
            "edge_collection": "HAS_SIGNAL",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Signal"] if c in cols],
        },
        {
            "edge_collection": "HAS_PARAMETER",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Parameter"] if c in cols],
        },
        {
            "edge_collection": "OVERRIDES",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Parameter"] if c in cols],
        },
        {
            "edge_collection": "CONTAINS",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_LogicChunk"] if c in cols],
        },
        {
            "edge_collection": "DEPENDS_ON",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Module"] if c in cols],
        },
        # ── Temporal snapshot → deep-RTL HEAD link ─────────────────────
        {
            "edge_collection": "SNAPSHOT_OF",
            "from_vertex_collections": [c for c in ["RTL_Module"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Module"] if c in cols],
        },
        {
            "edge_collection": "WIRED_TO",
            "from_vertex_collections": [c for c in ["RTL_Port", "RTL_Signal"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Port", "RTL_Signal"] if c in cols],
        },
        # ── Semantic bridge: RTL → Knowledge ─────────────────────────────
        {
            "edge_collection": "RESOLVED_TO",
            "from_vertex_collections": [c for c in ["RTL_Port", "RTL_Signal"] if c in cols],
            "to_vertex_collections":   golden_entity_cols if golden_entity_cols else ["RTL_Module"],
        },
        # ── Cross-repo bridges ───────────────────────────────────────────
        {
            "edge_collection": "CROSS_REPO_SIMILAR_TO",
            "from_vertex_collections": cross_vertex_cols,
            "to_vertex_collections":   cross_vertex_cols,
        },
        {
            "edge_collection": "CROSS_REPO_EVOLVED_FROM",
            "from_vertex_collections": cross_vertex_cols,
            "to_vertex_collections":   cross_vertex_cols,
        },
        # ── Author expertise ─────────────────────────────────────────────
        {
            "edge_collection": "AUTHORED",
            "from_vertex_collections": [c for c in ["Author"] if c in cols],
            "to_vertex_collections":   [c for c in ["GitCommit"] if c in cols],
        },
        {
            "edge_collection": "MAINTAINS",
            "from_vertex_collections": [c for c in ["Author"] if c in cols],
            "to_vertex_collections":   [c for c in ["RTL_Module"] if c in cols],
        },
    ]

    # Drop edge definitions where from/to ended up empty (collection doesn't exist yet)
    edge_definitions = [
        ed for ed in edge_definitions
        if ed and ed["from_vertex_collections"] and ed["to_vertex_collections"]
    ]

    # Per-repo GraphRAG Golden relation edges
    for rel_col in golden_rel_cols:
        ge_col = rel_col.replace("_Golden_Relations", "_Golden_Entities")
        if ge_col in cols:
            edge_definitions.append({
                "edge_collection": rel_col,
                "from_vertex_collections": [ge_col],
                "to_vertex_collections":   [ge_col],
            })

    # Per-repo raw relation edges
    for rel_col in relation_cols:
        prefix  = rel_col.replace("_Relations", "_")
        e_col   = prefix + "Entities"
        ch_col  = prefix + "Chunks"
        comm_col= prefix + "Communities"
        froms   = [c for c in [e_col, ch_col, comm_col] if c in cols]
        if froms:
            edge_definitions.append({
                "edge_collection": rel_col,
                "from_vertex_collections": froms,
                "to_vertex_collections":   froms,
            })

    # Per-repo CONSOLIDATES edges: Golden_Entity → raw Entity
    consolidates_cols = sorted(c for c in cols if c.endswith("_Consolidates"))
    for con_col in consolidates_cols:
        prefix   = con_col.replace("Consolidates", "")  # e.g. "IBEX_"
        ge_col   = prefix + "Golden_Entities"
        ent_col  = prefix + "Entities"
        if ge_col in cols and ent_col in cols:
            edge_definitions.append({
                "edge_collection": con_col,
                "from_vertex_collections": [ge_col],
                "to_vertex_collections":   [ent_col],
            })

    # Per-repo MENTIONED_IN edges: raw Entity → Chunk
    mentioned_cols = sorted(c for c in cols if c.endswith("_MentionedIn"))
    for men_col in mentioned_cols:
        prefix  = men_col.replace("MentionedIn", "")   # e.g. "IBEX_"
        ent_col = prefix + "Entities"
        ch_col  = prefix + "Chunks"
        if ent_col in cols and ch_col in cols:
            edge_definitions.append({
                "edge_collection": men_col,
                "from_vertex_collections": [ent_col],
                "to_vertex_collections":   [ch_col],
            })

    # Orphan collections (vertex-only, no direct edge from this graph)
    all_vertex_cols = (
        ["GitCommit", "RTL_Module", "RTL_Port", "RTL_Signal",
         "RTL_Parameter", "RTL_LogicChunk",
         "DesignEpoch", "DesignSituation", "Author"]
        + golden_entity_cols + entity_cols + community_cols + chunk_cols + doc_cols
    )
    linked = {
        v
        for ed in edge_definitions
        for v in ed["from_vertex_collections"] + ed["to_vertex_collections"]
    }
    orphans = [c for c in all_vertex_cols if c in cols and c not in linked]

    # Delete and recreate
    if db.has_graph(TEMPORAL_GRAPH_NAME):
        db.delete_graph(TEMPORAL_GRAPH_NAME)
        print(f"[graph] Deleted existing graph: {TEMPORAL_GRAPH_NAME}")

    graph = db.create_graph(
        TEMPORAL_GRAPH_NAME,
        edge_definitions=edge_definitions,
        orphan_collections=orphans,
    )
    print(f"[graph] Created: {TEMPORAL_GRAPH_NAME}")
    print(f"  Edge definitions ({len(edge_definitions)}):")
    for ed in edge_definitions:
        froms = ", ".join(ed["from_vertex_collections"])
        tos   = ", ".join(ed["to_vertex_collections"])
        print(f"    {ed['edge_collection']:35s}  [{froms}] → [{tos}]")
    if orphans:
        print(f"  Orphan vertex collections: {orphans}")
    return graph


def ensure_all_indexes(db) -> None:
    """
    Ensure vertex-centric indexes exist on every edge collection in the DB.

    Creates two persistent indexes per edge collection (if not already present):
      - (_from, toNodeType)   — enables fast "all edges from X to nodes of type Y" queries
      - (_to,   fromNodeType) — enables fast "all edges into X from nodes of type Y" queries

    This is idempotent: safe to run multiple times.
    """
    INDEX_PAIRS = [
        ["_from", "toNodeType"],
        ["_to",   "fromNodeType"],
    ]

    edge_collections = [
        c["name"] for c in db.collections()
        if not c["name"].startswith("_") and c["type"] == "edge"
    ]

    print(f"[indexes] Checking vertex-centric indexes on {len(edge_collections)} edge collections …")
    created = 0
    skipped = 0

    for col_name in sorted(edge_collections):
        col = db.collection(col_name)
        existing_fields = {frozenset(idx["fields"]) for idx in col.indexes()}
        for fields in INDEX_PAIRS:
            if frozenset(fields) not in existing_fields:
                col.add_index({"type": "persistent", "fields": fields, "sparse": False})
                created += 1
            else:
                skipped += 1

    print(f"[indexes] Done — {created} indexes created, {skipped} already present")


if __name__ == "__main__":
    client = ArangoClient(hosts=os.environ["ARANGO_ENDPOINT"])
    db = client.db(
        ARANGO_DATABASE,
        username=os.environ["ARANGO_USERNAME"],
        password=os.environ["ARANGO_PASSWORD"],
    )
    print(f"[graph] Connected to: {ARANGO_DATABASE} @ {os.environ['ARANGO_ENDPOINT']}")
    create_graph(db)
    ensure_all_indexes(db)
