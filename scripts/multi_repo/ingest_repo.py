"""
scripts/multi_repo/ingest_repo.py — Config-driven multi-repo IC ingestion pipeline.

Clones and/or updates each registered repo, runs the temporal ETL, deep RTL extraction,
and local GraphRAG pipeline, and loads results into ArangoDB.

Usage:
    # Ingest all repos (in priority order)
    python scripts/multi_repo/ingest_repo.py

    # Ingest a single repo by name
    python scripts/multi_repo/ingest_repo.py --repo mor1kx

    # Skip specific steps
    python scripts/multi_repo/ingest_repo.py --repo ibex --no-clone --no-temporal --no-rtl

    # Dry run (no DB writes)
    python scripts/multi_repo/ingest_repo.py --repo mor1kx --dry-run
"""

import os
import sys
import json
import argparse
from datetime import datetime

# Add src/ to path
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, os.path.dirname(__file__))

from config_temporal import (
    REPO_REGISTRY, INGESTION_LOG_FILE, TEMPORAL_DATA_DIR,
    get_local_path, load_repo_registry,
)

from clone_manager import ensure_clone


def log_ingestion(repo_name: str, result: dict) -> None:
    """Append an ingestion record to the log file."""
    os.makedirs(os.path.dirname(INGESTION_LOG_FILE), exist_ok=True)
    entry = {
        "repo":       repo_name,
        "ts":         int(datetime.utcnow().timestamp()),
        "ts_human":   datetime.utcnow().isoformat(),
        **result,
    }
    with open(INGESTION_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def ingest_one_repo(
    repo_config:       dict,
    do_clone:          bool = True,
    do_temporal:       bool = True,
    do_rtl:            bool = True,
    do_graphrag:       bool = True,
    dry_run:           bool = False,
    commit_limit:      int  = None,
    embedding_backend: str  = "sentence_transformers",
    doc_version:       str  = None,
) -> dict:
    """
    Full ingestion pipeline for a single repo.

    Steps:
        1.   Clone / pull
        2.   Temporal ETL (commit replay + epoch detection + ArangoDB load)
        2.5  Deep RTL extraction (ports, signals, logic chunks, edges)
        3.   Local GraphRAG (doc chunking + entity extraction + community + load)

    Returns summary dict.
    """
    name       = repo_config["name"]
    prefix     = repo_config["prefix"]
    rtl_subdir = repo_config.get("rtl_path", "rtl/verilog")
    doc_subdir = repo_config.get("doc_path", "doc")
    branch     = repo_config.get("branch", "master")
    github_url = repo_config.get("github_url", "")

    print(f"\n{'='*60}")
    print(f" Ingesting repo: {name}  (prefix={prefix})")
    print(f"{'='*60}")

    summary = {"nodes": 0, "edges": 0, "entities": 0, "chunks": 0, "communities": 0,
               "rtl_modules": 0, "rtl_ports": 0, "rtl_signals": 0}

    # ---- Step 1: Clone / pull -----------------------------------------------
    if do_clone:
        try:
            local_path = ensure_clone(repo_config, verbose=True)
        except RuntimeError as e:
            print(f"[ingestor] FATAL: Could not clone {name}: {e}")
            return {"error": str(e)}
    else:
        local_path = get_local_path(repo_config)
        if not os.path.isdir(local_path):
            print(f"[ingestor] ERROR: local path not found: {local_path} (use --no-clone only if already cloned)")
            return {"error": "local path missing"}

    repo_git_name = github_url.rstrip("/").split("github.com/")[-1] if "github.com" in github_url else name

    # ---- Step 2: Temporal ETL -----------------------------------------------
    if do_temporal:
        print(f"\n[ingestor] Running temporal ETL for {name} …")
        try:
            # Import here to avoid circular issues at module level
            from etl_temporal_git import replay_git_history, save_jsonl
            from scripts.temporal.load_temporal_data import main as load_temporal_main
        except ImportError:
            # Adjust path for running from scripts/multi_repo/
            sys.path.insert(0, os.path.join(SRC_DIR, "..", "scripts", "temporal"))
            from etl_temporal_git import replay_git_history, save_jsonl

        nodes_out = os.path.join(TEMPORAL_DATA_DIR, f"{name}_temporal_nodes.jsonl")
        edges_out = os.path.join(TEMPORAL_DATA_DIR, f"{name}_temporal_edges.jsonl")

        nodes, edges = replay_git_history(
            repo_path=local_path,
            repo_name=repo_git_name,
            rtl_subdir=rtl_subdir,
            commit_limit=commit_limit,
            original_branch=branch,
        )

        if not dry_run:
            save_jsonl(nodes, nodes_out)
            save_jsonl(edges, edges_out)

            # Load into ArangoDB
            import subprocess as _sp
            load_script = os.path.join(
                os.path.dirname(__file__), "..", "temporal", "load_temporal_data.py"
            )
            _sp.run([
                sys.executable, load_script,
                "--nodes-file", nodes_out,
                "--edges-file", edges_out,
            ], check=False)

        summary["nodes"] = len(nodes)
        summary["edges"] = len(edges)
        print(f"[ingestor] Temporal ETL done: {len(nodes)} nodes, {len(edges)} edges")

    # ---- Step 2.5: Deep RTL extraction ------------------------------------
    if do_rtl:
        rtl_dir = os.path.join(local_path, rtl_subdir)
        if not os.path.isdir(rtl_dir):
            print(f"[ingestor] No RTL dir at {rtl_dir} — skipping deep RTL for {name}")
        else:
            print(f"\n[ingestor] Running deep RTL extraction on {rtl_dir} …")
            try:
                from etl_rtl import parse_verilog_files
                from dotenv import load_dotenv
                load_dotenv()
                import os as _os
                from arango import ArangoClient
                _client = ArangoClient(hosts=_os.environ["ARANGO_ENDPOINT"])
                db = _client.db(
                    _os.environ["ARANGO_DATABASE"],
                    username=_os.environ["ARANGO_USERNAME"],
                    password=_os.environ["ARANGO_PASSWORD"],
                )
                rtl_extensions = repo_config.get("rtl_extensions", [".v"])
                rtl_summary = parse_verilog_files(
                    rtl_dir=rtl_dir,
                    prefix=prefix,
                    db=None if dry_run else db,
                    dry_run=dry_run,
                    rtl_extensions=rtl_extensions,
                )
                summary["rtl_modules"] = rtl_summary.get("modules", 0)
                summary["rtl_ports"]   = rtl_summary.get("ports", 0)
                summary["rtl_signals"] = rtl_summary.get("signals", 0)
            except Exception as e:
                print(f"[ingestor] WARNING: RTL extraction failed for {name}: {e}")
                import traceback; traceback.print_exc()

    # ---- Step 3: Local GraphRAG on docs ------------------------------------
    if do_graphrag:
        doc_dir = os.path.join(local_path, doc_subdir)
        if not os.path.isdir(doc_dir):
            print(f"[ingestor] No doc dir found at {doc_dir} — skipping GraphRAG for {name}")
        else:
            print(f"\n[ingestor] Running local GraphRAG on {doc_dir} …")
            try:
                from local_graphrag.pipeline import LocalGraphRAGPipeline
                pipeline = LocalGraphRAGPipeline(
                    prefix=prefix,
                    embedding_backend=embedding_backend,
                    chunk_size=repo_config.get("chunk_size"),
                    overlap=repo_config.get("chunk_overlap"),
                    entity_types=repo_config.get("entity_types"),
                    relation_types=repo_config.get("relation_types"),
                    doc_version=doc_version,
                )
                graphrag_summary = pipeline.run(doc_dir=doc_dir, dry_run=dry_run)
                summary["entities"]    = graphrag_summary.get("entities", 0)
                summary["chunks"]      = graphrag_summary.get("chunks", 0)
                summary["communities"] = graphrag_summary.get("communities", 0)
            except Exception as e:
                print(f"[ingestor] WARNING: GraphRAG failed for {name}: {e}")

    log_ingestion(name, summary)
    return summary


def ingest_all(
    repos:             list[dict] = None,
    do_clone:          bool = True,
    do_temporal:       bool = True,
    do_rtl:            bool = True,
    do_graphrag:       bool = True,
    dry_run:           bool = False,
    commit_limit:      int  = None,
    embedding_backend: str  = "sentence_transformers",
    doc_version:       str  = None,
) -> dict[str, dict]:
    """Ingest all repos in priority order."""
    repos = repos or load_repo_registry()
    results = {}
    for repo in repos:
        results[repo["name"]] = ingest_one_repo(
            repo,
            do_clone=do_clone,
            do_temporal=do_temporal,
            do_rtl=do_rtl,
            do_graphrag=do_graphrag,
            dry_run=dry_run,
            commit_limit=commit_limit,
            embedding_backend=embedding_backend,
            doc_version=doc_version,
        )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IC multi-repo ingestor")
    parser.add_argument("--repo",          default=None,
                        help="Name of a single repo to ingest (default: all)")
    parser.add_argument("--no-clone",      action="store_true",
                        help="Skip git clone/pull (use existing local copy)")
    parser.add_argument("--no-temporal",   action="store_true",
                        help="Skip temporal ETL")
    parser.add_argument("--no-rtl",        action="store_true",
                        help="Skip deep RTL extraction (ports, signals, logic chunks)")
    parser.add_argument("--no-graphrag",   action="store_true",
                        help="Skip local GraphRAG doc processing")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--commit-limit",  type=int, default=None,
                        help="Limit commits processed (for testing)")
    parser.add_argument("--embedding-backend", default="sentence_transformers",
                        choices=["sentence_transformers", "openai"],
                        help="Embedding backend for entity vectors (default: sentence_transformers)")
    parser.add_argument("--doc-version",       default=None,
                        help="Tag injected into doc_version / first_seen_version on every "
                             "golden entity (e.g. 'v2.0', '2026-03-16'). Enables temporal "
                             "tracking of documentation re-ingestions.")
    args = parser.parse_args()

    registry = load_repo_registry()

    if args.repo:
        repo_cfg = next((r for r in registry if r["name"] == args.repo), None)
        if not repo_cfg:
            print(f"ERROR: repo '{args.repo}' not found in registry.")
            sys.exit(1)
        repos_to_run = [repo_cfg]
    else:
        repos_to_run = registry

    results = ingest_all(
        repos=repos_to_run,
        do_clone=not args.no_clone,
        do_temporal=not args.no_temporal,
        do_rtl=not args.no_rtl,
        do_graphrag=not args.no_graphrag,
        dry_run=args.dry_run,
        commit_limit=args.commit_limit,
        embedding_backend=args.embedding_backend,
        doc_version=args.doc_version,
    )

    print(f"\n{'='*60}")
    print(" Ingestion Summary")
    print(f"{'='*60}")
    for repo_name, summary in results.items():
        print(f"  {repo_name:20s}: {summary}")

    print(f"\n{'='*60}")
    print(" Post-Ingestion Steps (run separately or via rebuild_database.sh):")
    print(f"{'='*60}")
    print("  2. PYTHONPATH=src python3 src/etl_authors.py")
    print("  3. PYTHONPATH=src python3 src/situation_detector.py --all")
    print("  4. PYTHONPATH=src python3 src/rtl_semantic_bridge.py --all")
    print("  5. PYTHONPATH=src python3 src/cross_repo_bridge.py --all")
    print("  6. PYTHONPATH=src python3 scripts/setup/create_snapshot_of_edges.py")
    print("  7. PYTHONPATH=src python3 scripts/temporal/create_temporal_graph.py")
    print("  8. PYTHONPATH=src python3 scripts/setup/install_ic_theme.py")
    print("  9. PYTHONPATH=src python3 scripts/setup/install_demo_setup.py")
    print("")
    print("  Or run: ./scripts/rebuild_database.sh --skip-ingestion")
