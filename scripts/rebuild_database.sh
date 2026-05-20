#!/usr/bin/env bash
#
# scripts/rebuild_database.sh
#
# Full database rebuild from scratch. Runs the complete pipeline:
#
#   Phase 0: Database Setup
#     0. Create database if it doesn't exist              (create_oneshard_database.py)
#
#   Phase 1: Data Ingestion
#     1. Multi-repo temporal ETL + deep RTL + GraphRAG    (ingest_repo.py)
#
#   Phase 2: Inference & Graph Definition
#     2. Author expertise mapping                         (etl_authors.py)
#     3. Situation detection                              (situation_detector.py)
#     4. RESOLVED_TO semantic bridges                     (rtl_semantic_bridge.py)
#     5. Cross-repo bridges                               (cross_repo_bridge.py)
#     6. SNAPSHOT_OF edges (temporal → HEAD)               (create_snapshot_of_edges.py)
#     7. Named graph definition (last — introspects all)  (create_temporal_graph.py)
#
#   Phase 3: Visualizer Setup
#     8a. Bootstrap Visualizer collections                 (bootstrap_visualizer_collections.py)
#     8b. Theme installation                               (install_ic_theme.py)
#     9.  Saved queries + canvas actions                    (install_demo_setup.py)
#
# Usage:
#   ./scripts/rebuild_database.sh                    # full rebuild
#   ./scripts/rebuild_database.sh --skip-ingestion   # skip Phase 1 (data already loaded)
#   ./scripts/rebuild_database.sh --skip-visualizer  # skip Phase 3 (theme/queries)
#
# Prerequisites:
#   - .env configured with ARANGO_* variables
#   - Python environment with python-arango, pyyaml, etc.
#   - Repos cloned (or let ingest_repo.py clone them)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_DIR="${PROJECT_ROOT}/src"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set +u
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
    set -u
fi

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SKIP_INGESTION=false
SKIP_VISUALIZER=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-ingestion)  SKIP_INGESTION=true; shift ;;
        --skip-visualizer) SKIP_VISUALIZER=true; shift ;;
        *)                 EXTRA_ARGS+=("$1"); shift ;;
    esac
done

PYTHON="${PYTHON:-python3}"

echo "============================================================"
echo " IC Temporal Knowledge Graph — Full Database Rebuild"
echo " Database: ${ARANGO_DATABASE:-<not set>}"
echo " Skip ingestion: ${SKIP_INGESTION}"
echo " Skip visualizer: ${SKIP_VISUALIZER}"
echo "============================================================"
echo ""

FAILED=0
step_ok() { echo "[REBUILD] ✓ $1"; }
step_fail() { echo "[REBUILD] ✗ $1 (non-fatal, continuing)"; FAILED=$((FAILED+1)); }

# ===================================================================
# Phase 0: Ensure database exists
# ===================================================================
echo "[REBUILD] Ensuring database exists..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/setup/create_oneshard_database.py"; then
    step_ok "Database exists (or created)"
else
    step_fail "Database creation"
fi

# ===================================================================
# Phase 1: Data Ingestion
# ===================================================================
if [[ "${SKIP_INGESTION}" == "false" ]]; then
    echo ""
    echo "============================================"
    echo " Phase 1: Data Ingestion"
    echo "============================================"

    echo "[REBUILD] Running multi-repo ingestion..."
    if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/multi_repo/ingest_repo.py" \
        --no-clone "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"; then
        step_ok "Multi-repo ingestion"
    else
        step_fail "Multi-repo ingestion"
    fi
else
    echo "[REBUILD] Skipping Phase 1 (--skip-ingestion)"
fi

# ===================================================================
# Phase 2: Graph & Inference
# ===================================================================
echo ""
echo "============================================"
echo " Phase 2: Inference & Graph Definition"
echo "============================================"

# Step 2: Author expertise mapping (before graph def so Author/AUTHORED/MAINTAINS exist)
echo "[REBUILD] Extracting author expertise..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SRC_DIR}/etl_authors.py"; then
    step_ok "Author vertices + AUTHORED/MAINTAINS edges"
else
    step_fail "Author expertise mapping"
fi

# Step 3: Design situations
echo "[REBUILD] Detecting design situations..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SRC_DIR}/situation_detector.py" --all; then
    step_ok "DesignSituation generation"
else
    step_fail "DesignSituation generation"
fi

# Step 4: RESOLVED_TO semantic bridges
echo "[REBUILD] Building RESOLVED_TO semantic bridges..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SRC_DIR}/rtl_semantic_bridge.py" --all; then
    step_ok "RESOLVED_TO bridges"
else
    step_fail "RESOLVED_TO bridges"
fi

# Step 5: Cross-repo bridges
echo "[REBUILD] Building cross-repo bridges..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SRC_DIR}/cross_repo_bridge.py" --all; then
    step_ok "Cross-repo bridges (SIMILAR_TO + EVOLVED_FROM)"
else
    step_fail "Cross-repo bridges"
fi

# Step 6: SNAPSHOT_OF edges
echo "[REBUILD] Creating SNAPSHOT_OF edges..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/setup/create_snapshot_of_edges.py"; then
    step_ok "SNAPSHOT_OF edges"
else
    step_fail "SNAPSHOT_OF edges"
fi

# Step 7: Named graph definition (last — so it introspects ALL collections)
echo "[REBUILD] Creating named graph definition..."
if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/temporal/create_temporal_graph.py"; then
    step_ok "Named graph (IC_Temporal_Knowledge_Graph)"
else
    step_fail "Named graph creation"
fi

# ===================================================================
# Phase 3: Visualizer Setup
# ===================================================================
if [[ "${SKIP_VISUALIZER}" == "false" ]]; then
    echo ""
    echo "============================================"
    echo " Phase 3: Visualizer Setup"
    echo "============================================"
    echo ""

    # Step 8a: Bootstrap Visualizer system collections (idempotent).
    # Eliminates the need to open the graph in the Web UI before installing
    # themes / canvas actions on a freshly-created database.
    echo "[REBUILD] Bootstrapping Visualizer system collections..."
    if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/setup/bootstrap_visualizer_collections.py"; then
        step_ok "Visualizer system collections + Default viewpoint"
    else
        step_fail "Bootstrap visualizer collections"
    fi

    # Step 8b: Epoch timeline edges used by timeline queries/actions.
    echo "[REBUILD] Creating PRECEDES_EPOCH timeline edges..."
    if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/setup/create_precedes_epoch_edges.py" \
        --graph IC_Temporal_Knowledge_Graph; then
        step_ok "PRECEDES_EPOCH timeline edges"
    else
        step_fail "PRECEDES_EPOCH timeline edges"
    fi

    # Step 8c: IC theme + canvas actions
    echo "[REBUILD] Installing Integrated Circuit theme..."
    if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/setup/install_ic_theme.py"; then
        step_ok "Integrated Circuit theme"
    else
        step_fail "Theme installation (open graph in UI first, then re-run)"
    fi

    # Step 9: Saved queries + canvas actions
    echo "[REBUILD] Installing demo saved queries and canvas actions..."
    if PYTHONPATH="${SRC_DIR}" ${PYTHON} "${SCRIPT_DIR}/setup/install_demo_setup.py" \
        --graph IC_Temporal_Knowledge_Graph; then
        step_ok "Saved queries + canvas actions"
    else
        step_fail "Demo setup installation"
    fi
else
    echo "[REBUILD] Skipping Phase 3 (--skip-visualizer)"
fi

# ===================================================================
# Summary
# ===================================================================
echo ""
echo "============================================================"
if [[ ${FAILED} -eq 0 ]]; then
    echo " REBUILD COMPLETE — all steps passed"
else
    echo " REBUILD COMPLETE — ${FAILED} step(s) had warnings"
fi
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Open the ArangoDB Web UI"
echo "  2. Switch to database: ${ARANGO_DATABASE:-ic-knowledge-graph-temporal}"
echo "  3. Navigate to Graphs → IC_Temporal_Knowledge_Graph"
echo "  4. Select 'Integrated Circuit' theme in Legend panel"
echo "  5. Open Queries → select saved queries by [scene] prefix"
echo ""

exit ${FAILED}
