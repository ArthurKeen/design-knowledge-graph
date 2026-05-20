#!/usr/bin/env python3
"""
Demo Setup Script: Install Saved Queries and Canvas Actions
============================================================

This script installs the demonstration queries and canvas actions into
the ArangoDB instance for the IC Temporal Knowledge Graph visualizer.

NOTE: This script does NOT install the theme. Run install_theme.py separately.

Requirements:
- python-arango library
- Access to ic-knowledge-graph-temporal database
- .env file with database credentials

Usage:
    python install_demo_setup.py
    python install_demo_setup.py --db ic-knowledge-graph-temporal
    python install_demo_setup.py --graph IC_Temporal_Knowledge_Graph

The script will:
1. Load queries and actions from DEMO_SETUP_QUERIES.json
2. Insert/update them into _editor_saved_queries and _canvasActions
3. Create necessary _viewpointActions edges
4. Verify installation

For theme installation, run: python install_theme.py

"""

import os
import sys
import json
import argparse
from datetime import datetime

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from config_temporal import TEMPORAL_GRAPH_NAME  # noqa: E402
DEFAULT_GRAPH_NAME = TEMPORAL_GRAPH_NAME


def _rewrite_graph_refs(text: str, graph_name: str) -> str:
    """Rewrite any hard-coded GRAPH \"...\" reference to the target graph."""
    if not isinstance(text, str):
        return text
    for legacy in ('OR1200_Knowledge_Graph', 'IC_Knowledge_Graph', 'IC_Temporal_Knowledge_Graph'):
        text = text.replace(f"GRAPH '{legacy}'", f"GRAPH '{graph_name}'")
        text = text.replace(f'GRAPH "{legacy}"', f'GRAPH "{graph_name}"')
    return text


def _normalize_assets(data: list, database_name: str, graph_name: str) -> list:
    """Ensure assets are scoped to the target DB and graph."""
    for q in data[0].get("queries", []):
        q["databaseName"] = database_name
        if "content" in q:
            q["content"] = _rewrite_graph_refs(q["content"], graph_name)
            q["value"] = q["content"]

    for a in data[1].get("actions", []):
        if "queryText" in a:
            a["queryText"] = _rewrite_graph_refs(a["queryText"], graph_name)
        a["graphId"] = graph_name

    return data


def install_saved_queries(db, queries_data):
    """Install saved queries into _editor_saved_queries collection."""
    print("\n[1/3] Installing Saved Queries...")

    if not db.has_collection("_editor_saved_queries"):
        try:
            db.create_collection("_editor_saved_queries", system=True)
            print("  Created system collection: _editor_saved_queries")
        except Exception as e:
            print(f"  Warning: Could not create _editor_saved_queries: {e}")
            print("  Trying non-system fallback...")
            try:
                db.create_collection("editor_saved_queries")
                print("  Created collection: editor_saved_queries (without underscore)")
            except Exception as e2:
                print(f"  Error: {e2}")
                return 0

    col_name = "_editor_saved_queries" if db.has_collection("_editor_saved_queries") else "editor_saved_queries"
    query_col = db.collection(col_name)
    installed = 0

    for query in queries_data[0]["queries"]:
        if "content" in query:
            query["value"] = query["content"]

        existing = list(query_col.find({"title": query["title"]}))

        if existing:
            doc_key = existing[0]["_key"]
            query["_key"] = doc_key
            query["createdAt"] = existing[0].get("createdAt", query.get("createdAt"))
            query["updatedAt"] = datetime.utcnow().isoformat() + "Z"
            query_col.replace(query, check_rev=False)
            print(f"  Updated: {query['title']}")
        else:
            query_col.insert(query)
            print(f"  Installed: {query['title']}")

        installed += 1

    print(f"\n  Total queries processed: {installed}")
    return installed


def install_canvas_actions(db, actions_data):
    """Install canvas actions into _canvasActions collection.

    The ArangoDB Graph Visualizer requires canvas actions to use ``queryText``
    (not ``query``) and the bind variable ``@nodes`` (array).  We use
    ``replace`` (not merge-update) so that any stale fields left by previous
    install runs are cleaned out.
    """
    print("\n[2/3] Installing Canvas Actions...")

    if not db.has_collection("_canvasActions"):
        print("  [PREREQ] Collection _canvasActions not found in this database.")
        print("  Action: Open Graph Visualizer for this DB once, then rerun this script.")
        return 0

    action_col = db.collection("_canvasActions")
    installed = 0

    for action in actions_data[1]["actions"]:
        key = action["_key"]

        if 'title' in action and 'name' not in action:
            action['name'] = action['title']
        if 'name' in action and 'title' not in action:
            action['title'] = action['name']

        if 'query' in action and 'queryText' not in action:
            action['queryText'] = action.pop('query')
        elif 'query' in action:
            del action['query']

        if 'bindVariables' not in action:
            action['bindVariables'] = {"nodes": []}

        action['updatedAt'] = datetime.utcnow().isoformat() + "Z"

        if action_col.has(key):
            action['_key'] = key
            action_col.replace(action)
            print(f"  Replaced: {action.get('name', key)}")
        else:
            action_col.insert(action)
            print(f"  Installed: {action.get('name', key)}")

        installed += 1

    print(f"\n  Total actions processed: {installed}")
    return installed


def link_actions_to_graph(db, actions_data, graph_name: str):
    """Create edges linking viewpoint to canvas actions."""
    print("\n[3/3] Linking Canvas Actions to Viewpoint...")
    
    # Find the viewpoint for the graph
    if not db.has_collection("_viewpoints"):
        print("  [PREREQ] _viewpoints collection not found in this database.")
        print("  Canvas actions will not appear until the graph has been opened once in Graph Visualizer.")
        print("  Action: Open Graph Visualizer for this DB (Graphs → IC_Knowledge_Graph) once, then rerun this script.")
        return 0
    
    viewpoints_col = db.collection("_viewpoints")
    viewpoints = list(viewpoints_col.all())
    
    if not viewpoints:
        print("  ERROR: No viewpoints found!")
        print("  Please open IC_Knowledge_Graph in the visualizer, then run this script again.")
        return 0
    
    # Prefer viewpoint matching the target graph (fallback to action graphId, then first viewpoint)
    action_graph_id = actions_data[1]["actions"][0].get("graphId") or graph_name
    viewpoint = None
    if action_graph_id:
        for vp in viewpoints:
            if vp.get("graphId") == action_graph_id:
                viewpoint = vp
                break
    if viewpoint is None:
        viewpoint = viewpoints[0]
    viewpoint_id = viewpoint['_id']
    print(f"  Using viewpoint: {viewpoint_id}")
    
    # Ensure _viewpointActions edge collection exists
    if not db.has_collection("_viewpointActions"):
        db.create_collection("_viewpointActions", edge=True)
        print("  Created edge collection: _viewpointActions")
    
    actions_edge_col = db.collection("_viewpointActions")
    linked = 0
    
    for action in actions_data[1]["actions"]:
        if action_graph_id and action.get("graphId") and action["graphId"] != action_graph_id:
            print(f"  Warning: action {action.get('_key')} has graphId {action.get('graphId')} (expected {action_graph_id})")
        action_id = f"_canvasActions/{action['_key']}"
        
        # Check if edge already exists FROM viewpoint TO action
        existing = list(actions_edge_col.find({"_from": viewpoint_id, "_to": action_id}))
        
        if not existing:
            edge = {
                "_from": viewpoint_id,
                "_to": action_id,
                "createdAt": datetime.utcnow().isoformat() + "Z"
            }
            actions_edge_col.insert(edge)
            print(f"  Linked: {action['title']}")
            linked += 1
        else:
            print(f"  Already linked: {action['title']}")
    
    print(f"\n  Total actions linked: {linked}")
    return linked


def install_visualizer_queries(db, data, graph_name: str):
    """Install queries into the Graph Visualizer's own query system.

    The Graph Visualizer uses ``_queries`` (linked to a viewpoint via
    ``_viewpointQueries``), which is separate from the standalone Query
    Editor's ``_editor_saved_queries``.
    """
    vis_section = next((s for s in data if "visualizer_queries" in s), None)
    if not vis_section:
        return 0

    print("\n[4/4] Installing Visualizer Queries...")

    if not db.has_collection("_queries"):
        print("  [PREREQ] _queries collection not found.")
        print("  Open the Graph Visualizer once, then rerun this script.")
        return 0

    if not db.has_collection("_viewpoints"):
        print("  [PREREQ] _viewpoints collection not found.")
        return 0

    viewpoints = list(db.collection("_viewpoints").all())
    viewpoint = next(
        (vp for vp in viewpoints if vp.get("graphId") == graph_name),
        viewpoints[0] if viewpoints else None,
    )
    if viewpoint is None:
        print("  ERROR: No viewpoints found.")
        return 0

    viewpoint_id = viewpoint["_id"]
    print(f"  Using viewpoint: {viewpoint_id}")

    queries_col = db.collection("_queries")
    if not db.has_collection("_viewpointQueries"):
        db.create_collection("_viewpointQueries", edge=True)
    vq_col = db.collection("_viewpointQueries")

    installed = 0
    for q in vis_section["visualizer_queries"]:
        q["graphId"] = graph_name
        q.setdefault("bindVariables", {})
        if "queryText" in q:
            q["queryText"] = _rewrite_graph_refs(q["queryText"], graph_name)

        existing = list(queries_col.find({"name": q["name"]}))
        if existing:
            q["_key"] = existing[0]["_key"]
            q["createdAt"] = existing[0].get("createdAt", q.get("createdAt"))
            q["updatedAt"] = datetime.utcnow().isoformat() + "Z"
            queries_col.replace(q, check_rev=False)
            query_id = existing[0]["_id"]
            if not list(vq_col.find({"_from": viewpoint_id, "_to": query_id})):
                vq_col.insert({
                    "_from": viewpoint_id,
                    "_to": query_id,
                    "createdAt": q["updatedAt"],
                    "updatedAt": q["updatedAt"],
                })
            print(f"  Replaced: {q['name']}")
        else:
            q["createdAt"] = datetime.utcnow().isoformat() + "Z"
            q["updatedAt"] = q["createdAt"]
            result = queries_col.insert(q)
            query_id = result["_id"]
            vq_col.insert({
                "_from": viewpoint_id,
                "_to": query_id,
                "createdAt": q["createdAt"],
                "updatedAt": q["updatedAt"],
            })
            print(f"  Installed + linked: {q['name']}")
        installed += 1

    print(f"\n  Total visualizer queries processed: {installed}")
    return installed


# Theme installation removed - use install_theme.py instead


def verify_installation(db):
    """Verify that all components were installed correctly."""
    print("\n" + "="*60)
    print("VERIFICATION")
    print("="*60)

    checks = []

    if db.has_collection("_editor_saved_queries"):
        query_count = db.collection("_editor_saved_queries").count()
        checks.append(("Saved Queries", query_count, query_count >= 20))
    else:
        checks.append(("Saved Queries", 0, False))

    if db.has_collection("_canvasActions"):
        action_count = db.collection("_canvasActions").count()
        checks.append(("Canvas Actions", action_count, action_count >= 12))
    else:
        checks.append(("Canvas Actions", 0, False))

    if db.has_collection("_viewpointActions"):
        link_count = db.collection("_viewpointActions").count()
        checks.append(("Action Links", link_count, link_count >= 12))
    else:
        checks.append(("Action Links", 0, False))

    all_passed = True
    for name, count, passed in checks:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {status} {name}: {count} documents")
        if not passed:
            all_passed = False

    print("\n" + "="*60)
    if all_passed:
        print("Installation successful! All components verified.")
        print("\nNext steps:")
        print("1. Make sure you've run: python scripts/setup/install_theme.py")
        print("2. Open ArangoDB web interface")
        print("3. Navigate to Graphs → IC_Temporal_Knowledge_Graph")
        print("4. Select 'Integrated Circuit' theme in Legend panel")
        print("5. Open Queries tab → select saved queries by [scene] prefix")
        print("6. Right-click nodes → Canvas Action to use actions")
    else:
        print("Installation incomplete. Please check errors above.")
    
    print("="*60 + "\n")
    
    return all_passed


def main():
    """Main installation routine."""
    parser = argparse.ArgumentParser(description="Install demo saved queries and canvas actions")
    parser.add_argument("--db", help="Target database name (e.g., ic-knowledge-graph-1)")
    parser.add_argument("--graph", default=DEFAULT_GRAPH_NAME, help="Target graph name (default: IC_Knowledge_Graph)")
    args = parser.parse_args()

    if args.db:
        # Ensure config resolves the correct DB regardless of LOCAL/REMOTE mode
        os.environ["ARANGO_DATABASE"] = args.db
        os.environ["LOCAL_ARANGO_DATABASE"] = args.db

    try:
        from db_utils import get_db
    except ImportError:
        print("Error: Could not import db_utils. Make sure you're in the project root.")
        sys.exit(1)

    print("="*60)
    print("IC Temporal Knowledge Graph - Demo Setup Installer")
    print("="*60)
    print("\nNote: This installs queries and actions only.")
    print("For theme installation, run: python scripts/setup/install_theme.py")
    
    # Load data from JSON file
    # Load queries and actions from JSON in docs/
    json_file = os.path.join(os.path.dirname(__file__), '..', '..', 'docs', 'DEMO_SETUP_QUERIES.json')
    
    if not os.path.exists(json_file):
        print(f"\nError: Could not find {json_file}")
        print("Make sure you're running this script from the docs/ directory.")
        sys.exit(1)
    
    print(f"\nLoading data from: {json_file}")
    with open(json_file, 'r') as f:
        data = json.load(f)

    # Scope assets to the target DB/graph
    if args.db:
        data = _normalize_assets(data, database_name=args.db, graph_name=args.graph)
    
    # Connect to database
    print("\nConnecting to database...")
    try:
        db = get_db()
        print(f"Connected to: {db.name}")
    except Exception as e:
        print(f"\nError connecting to database: {e}")
        print("Make sure your .env file is configured correctly.")
        sys.exit(1)
    
    # Verify graph exists
    if not db.has_graph(args.graph):
        print(f"\nError: Graph '{args.graph}' not found.")
        print("Please create the graph before running this script.")
        sys.exit(1)
    
    print(f"Graph '{args.graph}' found.")
    
    # Install components
    try:
        install_saved_queries(db, data)
        install_canvas_actions(db, data)
        link_actions_to_graph(db, data, graph_name=args.graph)
        install_visualizer_queries(db, data, graph_name=args.graph)
        
        # Verify installation
        success = verify_installation(db)
        
        sys.exit(0 if success else 1)
        
    except Exception as e:
        print(f"\n[ERROR] Installation failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

