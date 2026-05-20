#!/usr/bin/env python3
"""
Bootstrap ArangoDB Graph Visualizer system collections for a fresh database.

When a database is created programmatically (not by opening the Graph
Visualizer in the Web UI first), the underscore-prefixed collections that the
Visualizer uses for themes, canvas actions, viewpoints, and stored queries do
not yet exist. Any attempt to install themes / queries / actions will then
silently skip with ``[PREREQ]`` warnings.

This script idempotently creates those collections (with ``system=True``) and
inserts a ``Default`` viewpoint for the target graph so that downstream
installers — ``install_ic_theme.py`` and ``install_demo_setup.py`` — can run
without any manual Web UI step.

Usage:
    PYTHONPATH=src python3 scripts/setup/bootstrap_visualizer_collections.py
    PYTHONPATH=src python3 scripts/setup/bootstrap_visualizer_collections.py \
        --graph IC_Temporal_Knowledge_Graph
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from db_utils import get_db  # noqa: E402
from config_temporal import TEMPORAL_GRAPH_NAME  # noqa: E402


VISUALIZER_DOCUMENT_COLLECTIONS = [
    "_graphThemeStore",
    "_canvasActions",
    "_viewpoints",
    "_queries",
    "_editor_saved_queries",
]

VISUALIZER_EDGE_COLLECTIONS = [
    "_viewpointActions",
    "_viewpointQueries",
]


def ensure_collection(db, name: str, edge: bool = False) -> bool:
    if db.has_collection(name):
        return False
    db.create_collection(name, edge=edge, system=name.startswith("_"))
    return True


def ensure_default_viewpoint(db, graph_name: str) -> str:
    vp_col = db.collection("_viewpoints")
    for query in (
        {"graphId": graph_name, "name": "Default"},
        {"graphId": graph_name},
    ):
        existing = list(vp_col.find(query))
        if existing:
            return existing[0]["_id"]
    now = datetime.utcnow().isoformat() + "Z"
    res = vp_col.insert(
        {
            "graphId": graph_name,
            "name": "Default",
            "description": f"Default viewpoint for {graph_name}",
            "createdAt": now,
            "updatedAt": now,
        }
    )
    return res["_id"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--graph",
        default=TEMPORAL_GRAPH_NAME,
        help=f"Graph name to create Default viewpoint for (default: {TEMPORAL_GRAPH_NAME})",
    )
    args = p.parse_args()

    db = get_db()
    print(f"Connected: {db.name}")
    print()
    print("[1/2] Ensuring Visualizer system collections exist …")

    created = 0
    for name in VISUALIZER_DOCUMENT_COLLECTIONS:
        if ensure_collection(db, name, edge=False):
            print(f"  [create] document collection: {name}")
            created += 1
        else:
            print(f"  [exists] {name}")

    for name in VISUALIZER_EDGE_COLLECTIONS:
        if ensure_collection(db, name, edge=True):
            print(f"  [create] edge collection:     {name}")
            created += 1
        else:
            print(f"  [exists] {name}")

    print()
    print(f"[2/2] Ensuring Default viewpoint for graph '{args.graph}' …")
    vp_id = ensure_default_viewpoint(db, args.graph)
    print(f"  viewpoint: {vp_id}")

    print()
    print(
        f"[OK] Bootstrap complete — {created} collection(s) created; "
        "installers can now run without manual UI step."
    )


if __name__ == "__main__":
    main()
