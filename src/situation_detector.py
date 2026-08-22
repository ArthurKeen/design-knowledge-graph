"""
src/situation_detector.py — Design situation detection and indexing.

Scans the temporal knowledge graph to identify named DesignSituation nodes
representing structural patterns at specific design epochs.

DesignSituation nodes are the core of the "Déjà Vu of Design" capability:
they capture what happened at a pivotal moment in a repo's history so that
the same pattern can be recognized in future work.

Usage:
    python src/situation_detector.py --repo or1200 --repo-name openrisc/or1200
    python src/situation_detector.py --all          # scan all registered repos
"""

import os
import sys
import json
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import COL_COMMIT, COL_MODULE
from config_temporal import (
    ARANGO_DATABASE, COL_DESIGN_SITUATION,
    REPO_REGISTRY,
)
from db_utils import get_temporal_db
from utils import get_edge_key


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

SITUATION_HEURISTICS = {
    "subsystem_addition": "New top-level RTL module(s) introduced at this epoch.",
    "major_refactor":     "More than 30% of RTL files changed in a short commit burst.",
    "bus_protocol_adoption": "New bus interface signals appeared across multiple modules.",
    "release_prep":       "Commit activity concentrated within 14 days before a release tag.",
    "doc_ahead_of_impl":  "Documentation section created with no corresponding RTL element yet.",
    "bug_fix_wave":       "5+ commits touching the same module cluster within 7 days.",
}


def _sha_key(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode()).hexdigest()[:16]


def _make_situation_node(
    situation_class: str,
    repo: str,
    epoch: str,
    commit_range_start: str,
    commit_range_end: str,
    valid_from_ts: int,
    valid_to_ts: int,
    tags: list[str],
    outcome: str = "unknown",
    notes: str = "",
) -> dict:
    key = _sha_key(f"{repo}:{epoch}:{situation_class}")
    return {
        "_key":               key,
        "label":              f"{situation_class.replace('_', ' ').title()} — {repo} @ {epoch}",
        "type":               COL_DESIGN_SITUATION,
        "repo":               repo,
        "epoch":              epoch,
        "situation_class":    situation_class,
        "commit_range_start": commit_range_start,
        "commit_range_end":   commit_range_end,
        "valid_from_ts":      valid_from_ts,
        "valid_to_ts":        valid_to_ts,
        "tags":               tags,
        "outcome":            outcome,
        "outcome_notes":      notes,
        "description":        SITUATION_HEURISTICS.get(situation_class, ""),
        "structural_sig_hash": None,   # computed separately when embeddings available
        "embedding":          None,
    }


# ---------------------------------------------------------------------------
# Situation detectors (each takes commits for an epoch, returns list of situations)
# ---------------------------------------------------------------------------

def _detect_subsystem_addition(
    repo_name: str, epoch: str, commits: list[dict],
    new_modules_by_commit: dict,          # pre-fetched: sha → [module dicts]
) -> list[dict]:
    """Detect when a new top-level RTL module was added.

    Uses a pre-fetched dict (one bulk AQL query for the whole repo) rather
    than issuing one DB round-trip per commit.
    """
    situations = []
    for commit in commits:
        sha = commit.get("_key") or commit.get("sha")
        ts  = commit.get("valid_from_ts") or commit.get("metadata", {}).get("timestamp", 0)

        new_modules = new_modules_by_commit.get(sha, [])
        if new_modules:
            tags = [m.get("label", "") for m in new_modules[:5]]
            situations.append(_make_situation_node(
                "subsystem_addition", repo_name, epoch,
                commit_range_start=sha, commit_range_end=sha,
                valid_from_ts=ts, valid_to_ts=ts,
                tags=tags,
                notes=f"New module(s): {', '.join(tags)}",
            ))
    return situations



def _detect_release_prep(repo_name: str, epoch: str, commits: list[dict]) -> list[dict]:
    """Detect release preparation: an epoch that ends with a milestone tag."""
    if not epoch.startswith("milestone_"):
        return []
    if not commits:
        return []

    # The whole epoch is the release prep window
    start = commits[0]
    end   = commits[-1]
    start_sha = start.get("_key") or start.get("sha", "")
    end_sha   = end.get("_key") or end.get("sha", "")
    start_ts  = start.get("valid_from_ts") or 0
    end_ts    = end.get("valid_from_ts")   or 0

    tag = epoch.replace("milestone_", "").replace("_", ".")
    return [_make_situation_node(
        "release_prep", repo_name, epoch,
        commit_range_start=start_sha, commit_range_end=end_sha,
        valid_from_ts=start_ts, valid_to_ts=end_ts,
        tags=["release", tag],
        outcome="milestone_release",
        notes=f"Commits leading to release tag {tag}",
    )]


def _detect_major_refactor(repo_name: str, epoch: str, commits: list[dict]) -> list[dict]:
    """Any epoch labelled major_refactor_* is itself a design situation."""
    if not epoch.startswith("major_refactor_"):
        return []
    if not commits:
        return []
    start = commits[0]; end = commits[-1]
    return [_make_situation_node(
        "major_refactor", repo_name, epoch,
        commit_range_start=start.get("_key", ""),
        commit_range_end=end.get("_key", ""),
        valid_from_ts=start.get("valid_from_ts", 0),
        valid_to_ts=end.get("valid_from_ts", 0),
        tags=["refactor", repo_name.split("/")[-1]],
        notes=f"Major RTL refactor in epoch {epoch}",
    )]


# ---------------------------------------------------------------------------
# Main detection function
# ---------------------------------------------------------------------------

def detect_design_situations(repo_name: str, db) -> list[dict]:
    """
    Scan the temporal graph for a given repo and generate DesignSituation nodes.

    Args:
        repo_name:  Canonical repo name (e.g. "openrisc/or1200").
        db:         ArangoDB database handle.

    Returns:
        List of DesignSituation node dicts.
    """
    print(f"\n[situation] Detecting design situations for {repo_name} …")

    # Fetch all commits for this repo, grouped by epoch
    commits_by_epoch = defaultdict(list)
    commit_cursor = db.aql.execute(
        "FOR c IN GitCommit FILTER c.repo == @repo "
        "SORT c.valid_from_ts ASC RETURN c",
        bind_vars={"repo": repo_name}
    )
    for c in commit_cursor:
        epoch = c.get("design_epoch", "development")
        commits_by_epoch[epoch].append(c)

    if not commits_by_epoch:
        print(f"[situation] No commits found for {repo_name} — run temporal ETL first.")
        return []

    print(f"[situation] Found {sum(len(v) for v in commits_by_epoch.values())} commits "
          f"across {len(commits_by_epoch)} epochs.")

    all_situations: dict[str, dict] = {}

    # Pre-fetch all newly-introduced modules for this repo in ONE query.
    # Group by introducing commit SHA so _detect_subsystem_addition doesn't
    # need to issue a separate DB query per commit.
    new_modules_by_commit: dict[str, list] = defaultdict(list)
    try:
        for m in db.aql.execute(
            "FOR m IN RTL_Module FILTER m.repo == @repo AND m.valid_from_commit != null "
            "RETURN {sha: m.valid_from_commit, label: m.label, _id: m._id}",
            bind_vars={"repo": repo_name}
        ):
            new_modules_by_commit[m["sha"]].append(m)
    except Exception as e:
        print(f"[situation] WARN: could not pre-fetch new modules: {e}")

    for epoch, commits in commits_by_epoch.items():
        # Run each heuristic (no per-commit DB queries — all data is pre-fetched)
        new = []
        new += _detect_subsystem_addition(repo_name, epoch, commits, new_modules_by_commit)
        new += _detect_release_prep(repo_name, epoch, commits)
        new += _detect_major_refactor(repo_name, epoch, commits)

        for sit in new:
            all_situations[sit["_key"]] = sit

    print(f"[situation] {len(all_situations)} design situations detected.")
    return list(all_situations.values())


def save_situations(db, situations: list[dict]) -> int:
    """Upsert DesignSituation nodes into ArangoDB."""
    if not situations:
        return 0
    from db_utils import ensure_collection
    ensure_collection(db, COL_DESIGN_SITUATION)
    col = db.collection(COL_DESIGN_SITUATION)
    written = 0
    for sit in situations:
        try:
            col.insert(sit, overwrite=True)
            written += 1
        except Exception as e:
            print(f"  [situation] WARN: {e}")
    print(f"[situation] Saved {written} DesignSituation nodes.")
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Design situation detector")
    parser.add_argument("--repo-name", default="openrisc/or1200",
                        help="Canonical repo name to scan")
    parser.add_argument("--all",       action="store_true",
                        help="Scan all registered repos")
    args = parser.parse_args()

    db = get_temporal_db()

    repos_to_scan: list[str]
    if args.all:
        # Discover actual repo names from DB — matches exactly what ETL wrote
        repos_to_scan = [
            r["repo"] for r in db.aql.execute(
                "FOR c IN GitCommit COLLECT repo = c.repo RETURN {repo}"
            )
        ]
        print(f"[situation] Scanning {len(repos_to_scan)} repos from DB: {repos_to_scan}")
    else:
        repos_to_scan = [args.repo_name]

    for repo_name in repos_to_scan:
        situations = detect_design_situations(repo_name, db)
        save_situations(db, situations)
