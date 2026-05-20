#!/usr/bin/env python3
"""
Create PRECEDES_EPOCH edges for DesignEpoch timelines.

The Graph Visualizer demo uses these edges for release timelines and
neighbor expansion actions. This script is idempotent: it upserts the managed
edges it owns and removes managed edges that are no longer valid after a data
refresh.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from config_temporal import TEMPORAL_GRAPH_NAME  # noqa: E402
from db_utils import get_db  # noqa: E402

EDGE_COLLECTION = "PRECEDES_EPOCH"
MANAGED_BY = "scripts/setup/create_precedes_epoch_edges.py"
MILESTONE_TYPES = {"initial_commit", "milestone_tag"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def edge_key(edge_type: str, from_id: str, to_id: str) -> str:
    raw = f"{edge_type}|{from_id}|{to_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def ensure_edge_collection(db):
    if not db.has_collection(EDGE_COLLECTION):
        db.create_collection(EDGE_COLLECTION, edge=True)
        return db.collection(EDGE_COLLECTION)

    collection_meta = next(
        (c for c in db.collections() if c["name"] == EDGE_COLLECTION),
        {},
    )
    col = db.collection(EDGE_COLLECTION)
    if collection_meta.get("type") != "edge":
        raise RuntimeError(f"{EDGE_COLLECTION} exists but is not an edge collection")
    return col


def ensure_graph_edge_definition(db, graph_name: str) -> None:
    if not db.has_graph(graph_name):
        print(f"[graph] Skipped: graph '{graph_name}' not found")
        return

    graph = db.graph(graph_name)
    existing_edges = {
        ed.get("edge_collection")
        for ed in graph.edge_definitions()
    }
    if EDGE_COLLECTION in existing_edges:
        print(f"[graph] Edge definition already exists: {EDGE_COLLECTION}")
        return

    graph.create_edge_definition(
        edge_collection=EDGE_COLLECTION,
        from_vertex_collections=["DesignEpoch"],
        to_vertex_collections=["DesignEpoch"],
    )
    print(f"[graph] Added edge definition: {EDGE_COLLECTION}")


def load_epochs_by_repo(db) -> dict[str, list[dict]]:
    epochs_by_repo: dict[str, list[dict]] = defaultdict(list)
    cursor = db.aql.execute(
        """
        FOR epoch IN DesignEpoch
          FILTER epoch.repo != null
          FILTER epoch.start_ts != null
          SORT epoch.repo ASC, epoch.start_ts ASC, epoch._key ASC
          RETURN KEEP(epoch, "_id", "_key", "repo", "label", "epoch_type", "start_ts", "end_ts", "git_tag")
        """
    )
    for epoch in cursor:
        epochs_by_repo[epoch["repo"]].append(epoch)
    return dict(epochs_by_repo)


def build_edge(edge_type: str, from_epoch: dict, to_epoch: dict, index: int, ts: str) -> dict:
    gap_days = None
    if from_epoch.get("start_ts") is not None and to_epoch.get("start_ts") is not None:
        gap_days = round((to_epoch["start_ts"] - from_epoch["start_ts"]) / 86400, 2)

    return {
        "_key": edge_key(edge_type, from_epoch["_id"], to_epoch["_id"]),
        "_from": from_epoch["_id"],
        "_to": to_epoch["_id"],
        "edge_type": edge_type,
        "repo": from_epoch.get("repo"),
        "from_label": from_epoch.get("label"),
        "to_label": to_epoch.get("label"),
        "from_start_ts": from_epoch.get("start_ts"),
        "to_start_ts": to_epoch.get("start_ts"),
        "gap_days": gap_days,
        "sequence_index": index,
        "managedBy": MANAGED_BY,
        "updatedAt": ts,
    }


def upsert_edges(col, edges: list[dict]) -> tuple[int, int, int]:
    expected_keys = {edge["_key"] for edge in edges}
    ts = now_iso()
    inserted = 0
    updated = 0

    for edge in edges:
        existing = col.get(edge["_key"]) if col.has(edge["_key"]) else None
        if existing:
            edge["createdAt"] = existing.get("createdAt", ts)
            col.replace(edge, check_rev=False)
            updated += 1
        else:
            edge["createdAt"] = ts
            col.insert(edge)
            inserted += 1

    deleted = 0
    for stale in col.find({"managedBy": MANAGED_BY}):
        if stale["_key"] not in expected_keys:
            col.delete(stale["_key"])
            deleted += 1

    return inserted, updated, deleted


def build_precedes_epoch_edges(db) -> list[dict]:
    edges: list[dict] = []
    ts = now_iso()
    epochs_by_repo = load_epochs_by_repo(db)

    for repo, epochs in sorted(epochs_by_repo.items()):
        for index, (current, nxt) in enumerate(zip(epochs, epochs[1:])):
            edges.append(build_edge("next_epoch", current, nxt, index, ts))

        milestones = [
            epoch for epoch in epochs
            if epoch.get("epoch_type") in MILESTONE_TYPES
        ]
        for index, (current, nxt) in enumerate(zip(milestones, milestones[1:])):
            edges.append(build_edge("milestone_shortcut", current, nxt, index, ts))

    return edges


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--graph",
        default=TEMPORAL_GRAPH_NAME,
        help=f"Named graph to update (default: {TEMPORAL_GRAPH_NAME})",
    )
    args = parser.parse_args()

    db = get_db()
    print(f"Connected: {db.name}")

    col = ensure_edge_collection(db)
    edges = build_precedes_epoch_edges(db)
    inserted, updated, deleted = upsert_edges(col, edges)
    print(
        f"[edges] {EDGE_COLLECTION}: {inserted} inserted, "
        f"{updated} updated, {deleted} stale managed edge(s) removed"
    )

    ensure_graph_edge_definition(db, args.graph)


if __name__ == "__main__":
    main()
