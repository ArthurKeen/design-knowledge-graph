"""
config_temporal.py — Temporal and multi-repo configuration for feature/temporal-kg branch.

Extends config.py with:
  - Temporal graph constants
  - Multi-repo registry (REPO_REGISTRY)
  - Epoch detection thresholds
  - Cross-repo bridge constants
  - Phase 5 agentic swarm settings (disabled by default)
"""

import os
import yaml
from config import (
    PROJECT_ROOT, ARANGO_ENDPOINT, ARANGO_USERNAME, ARANGO_PASSWORD,
    GRAPH_NAME,
)

# Temporal branch uses its own database: ic-knowledge-graph-temporal
# This is set in .env as ARANGO_DATABASE=ic-knowledge-graph-temporal
# and picked up by config.py at import time. We re-export it here
# so temporal scripts can reference TEMPORAL_DB directly.
import os as _os
ARANGO_DATABASE = _os.getenv("ARANGO_DATABASE", "ic-knowledge-graph-temporal")

# ---------------------------------------------------------------------------
# Temporal Graph Constants
# ---------------------------------------------------------------------------

# Named graph for temporal data (separate from existing IC_Knowledge_Graph)
TEMPORAL_GRAPH_NAME = os.getenv("TEMPORAL_GRAPH_NAME", "IC_Temporal_Knowledge_Graph")

# Threshold: fraction of RTL files changed in one commit to qualify as a major refactor epoch
# 0.15 = 15% of all RTL files touched in one commit — catches significant rewrites
# without triggering on routine incremental commits.
MAJOR_REFACTOR_THRESHOLD = float(os.getenv("MAJOR_REFACTOR_THRESHOLD", "0.15"))

# Time-window epoch: if this many days elapse since the last epoch boundary, start a
# new period_YYYY_MM epoch.  Set to 0 to disable.  180 days ≈ 2 major releases/year.
EPOCH_WINDOW_DAYS = int(os.getenv("EPOCH_WINDOW_DAYS", "180"))

# Branch to replay history from in each source repo
TEMPORAL_CHECKOUT_BRANCH = os.getenv("TEMPORAL_CHECKOUT_BRANCH", "master")

# Whether to replay the full git history (True) or only ingest latest snapshot (False)
TEMPORAL_REPLAY_ENABLED = os.getenv("TEMPORAL_REPLAY_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# New Vertex & Edge Collections (Temporal + Cross-Repo)
# ---------------------------------------------------------------------------

# Temporal design metadata
COL_DESIGN_EPOCH = "DesignEpoch"
COL_DESIGN_SITUATION = "DesignSituation"

# Phase 5 only
COL_DESIGN_ALERT = "DesignAlert"
COL_AGENT_RUN = "AgentRun"
COL_WATCHED_REPO = "WatchedRepo"

# Cross-repo edges
EDGE_CROSS_REPO_SIMILAR = "CROSS_REPO_SIMILAR_TO"
EDGE_CROSS_REPO_EVOLVED = "CROSS_REPO_EVOLVED_FROM"
EDGE_BELONGS_TO_EPOCH = "BELONGS_TO_EPOCH"
EDGE_EXEMPLIFIES = "EXEMPLIFIES"

# ---------------------------------------------------------------------------
# Temporal metadata field names (added to all RTL nodes during temporal ETL)
# ---------------------------------------------------------------------------
TEMPORAL_FIELDS = [
    "valid_from_commit",    # SHA of commit that introduced this element
    "valid_from_ts",         # Unix timestamp of valid_from_commit
    "valid_to_commit",       # SHA where element was removed/replaced (None = still valid)
    "valid_to_ts",           # Unix timestamp of valid_to_commit (None = still valid)
    "design_epoch",          # Named epoch label string
    "repo",                  # e.g. "openrisc/or1200"
]

# ---------------------------------------------------------------------------
# Multi-Repo Registry
# Loaded from YAML file; fallback to hardcoded defaults if file not present.
# ---------------------------------------------------------------------------

REPOS_DIR = os.path.join(PROJECT_ROOT, "data", "repos")
REPO_REGISTRY_PATH = os.getenv(
    "REPO_REGISTRY_PATH",
    os.path.join(PROJECT_ROOT, "scripts", "multi_repo", "repo_registry.yaml")
)

_REPO_REGISTRY_DEFAULTS = [
    {
        "name":        "or1200",
        "github_url":  "https://github.com/openrisc/or1200.git",
        "branch":      "master",
        "rtl_path":    "rtl/verilog",
        "doc_path":    "doc",
        "prefix":      "OR1200_",
        "priority":    1,
        "local_path":  os.path.join(PROJECT_ROOT, "or1200"),  # already a submodule
    },
    {
        "name":        "mor1kx",
        "github_url":  "https://github.com/openrisc/mor1kx.git",
        "branch":      "master",
        "rtl_path":    "rtl/verilog",
        "doc_path":    "doc",
        "prefix":      "MOR1KX_",
        "priority":    1,
        "local_path":  None,  # will be cloned
    },
    {
        "name":        "ibex",
        "github_url":  "https://github.com/lowRISC/ibex.git",
        "branch":      "master",
        "rtl_path":    "rtl",
        "doc_path":    "doc",
        "prefix":      "IBEX_",
        "priority":    2,
        "local_path":  None,
    },
    {
        "name":        "marocchino",
        "github_url":  "https://github.com/openrisc/marocchino.git",
        "branch":      "master",
        "rtl_path":    "rtl/verilog",
        "doc_path":    "doc",
        "prefix":      "MAROCCHINO_",
        "priority":    2,
        "local_path":  None,
    },
]


def load_repo_registry() -> list[dict]:
    """Load repo registry from YAML file, falling back to hardcoded defaults."""
    if os.path.exists(REPO_REGISTRY_PATH):
        with open(REPO_REGISTRY_PATH, "r") as f:
            data = yaml.safe_load(f)
        return sorted(data.get("repos", []), key=lambda r: r.get("priority", 99))
    return sorted(_REPO_REGISTRY_DEFAULTS, key=lambda r: r.get("priority", 99))


REPO_REGISTRY = load_repo_registry()


def get_repo_config(name: str) -> dict | None:
    """Look up a repo config by name."""
    return next((r for r in REPO_REGISTRY if r["name"] == name), None)


def get_local_path(repo_config: dict) -> str:
    """Return the local filesystem path for a repo clone."""
    lp = repo_config.get("local_path")
    if lp:
        if os.path.isabs(lp):
            return lp
        return os.path.join(PROJECT_ROOT, lp)
    return os.path.join(REPOS_DIR, repo_config["name"])


# ---------------------------------------------------------------------------
# Cross-Repo Bridge Settings
# ---------------------------------------------------------------------------
CROSS_REPO_MIN_SIMILARITY = float(os.getenv("CROSS_REPO_MIN_SIMILARITY", "0.70"))
CROSS_REPO_BRIDGE_BATCH_SIZE = int(os.getenv("CROSS_REPO_BRIDGE_BATCH_SIZE", "500"))

# Sentinel timestamp for open-ended validity (node is still current / not yet expired).
# Used in temporal nodes (valid_to_ts) and AQL filters.
OPEN_VALIDITY_TS = int(os.getenv("OPEN_VALIDITY_TS", "9999999999"))

# Lineage rules for CROSS_REPO_EVOLVED_FROM edges (rule-based, no ML needed)
LINEAGE_RULES = [
    {
        "from_repo":    "openrisc/mor1kx",
        "to_repo":      "openrisc/or1200",
        "match_by":     "suffix_after_prefix",  # mor1kx_cpu → or1200_cpu
        "confidence":   0.90,
        "lineage":      "direct_architectural_successor",
    },
    {
        "from_repo":    "openrisc/marocchino",
        "to_repo":      "openrisc/mor1kx",
        "match_by":     "embedding",
        "confidence":   0.85,
        "lineage":      "functional_extension",
    },
]

# ---------------------------------------------------------------------------
# Local GraphRAG Settings
# ---------------------------------------------------------------------------
LOCAL_GRAPHRAG_BACKEND = os.getenv("LOCAL_GRAPHRAG_BACKEND", "openai")  # "openai" | "ollama"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
LOCAL_GRAPHRAG_CHUNK_SIZE = int(os.getenv("LOCAL_GRAPHRAG_CHUNK_SIZE", "1200"))
LOCAL_GRAPHRAG_CHUNK_OVERLAP = int(os.getenv("LOCAL_GRAPHRAG_CHUNK_OVERLAP", "100"))

# ---------------------------------------------------------------------------
# Phase 5 — Agentic Swarm (disabled by default)
# ---------------------------------------------------------------------------
AGENTS_ENABLED = os.getenv("AGENTS_ENABLED", "false").lower() == "true"
COMMIT_WATCHER_INTERVAL = int(os.getenv("COMMIT_WATCHER_INTERVAL", "300"))
PATTERN_MATCHER_INTERVAL = int(os.getenv("PATTERN_MATCHER_INTERVAL", "120"))
DOC_DRIFT_THRESHOLD = float(os.getenv("DOC_DRIFT_THRESHOLD", "0.60"))
DEJA_VU_ALERT_THRESHOLD = float(os.getenv("DEJA_VU_ALERT_THRESHOLD", "0.75"))

# ---------------------------------------------------------------------------
# Temporal Output Files
# ---------------------------------------------------------------------------
TEMPORAL_DATA_DIR = os.path.join(PROJECT_ROOT, "data", "temporal")
os.makedirs(TEMPORAL_DATA_DIR, exist_ok=True)

TEMPORAL_NODES_FILE = os.path.join(TEMPORAL_DATA_DIR, "temporal_nodes.jsonl")
TEMPORAL_EDGES_FILE = os.path.join(TEMPORAL_DATA_DIR, "temporal_edges.jsonl")
EPOCHS_FILE = os.path.join(TEMPORAL_DATA_DIR, "epochs.json")
INGESTION_LOG_FILE = os.path.join(PROJECT_ROOT, "data", "ingestion_log.jsonl")
