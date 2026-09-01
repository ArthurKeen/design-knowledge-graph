#!/usr/bin/env python3
"""
scripts/analysis/churn_and_author_report.py — UC4 Temporal Analysis & Design
Evolution: module churn heatmap + author impact report.

Closes the reporting half of REQ-022 (business-requirements.md). Time-travel
queries and epoch stepping already exist (etl_temporal_git.py's bitemporal
fields + the ChronoGraph visualizer in viz/); this script is the piece that
was missing: a concrete churn-over-time view and a per-author impact ranking,
computed directly from the MODIFIED / AUTHORED / MAINTAINS edges already in
the live temporal graph.

Outputs (per repo, or --all repos):
  - A module x epoch churn matrix (commit-touch counts), rendered as a
    Plotly heatmap HTML (matches this project's existing plotly dependency).
  - An author impact table: commit count, modules touched, module-breadth
    (MAINTAINS edge count), and first/last activity — sorted by impact.

Usage:
    PYTHONPATH=src python3 scripts/analysis/churn_and_author_report.py --repo or1200
    PYTHONPATH=src python3 scripts/analysis/churn_and_author_report.py --all
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from config_temporal import REPO_REGISTRY  # noqa: E402
from db_utils import get_temporal_db  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                       "ic_analysis_output", "temporal_evolution")


def _canonical_repo(name: str) -> str | None:
    cfg = next((r for r in REPO_REGISTRY if r["name"] == name), None)
    if not cfg:
        return None
    return cfg["github_url"].split("github.com/")[-1].removesuffix(".git") + ".git" \
        if "github.com" in cfg["github_url"] else name


def churn_matrix(db, canonical_repo: str):
    """Returns (module_labels, epoch_labels, matrix[module][epoch] = commit count)."""
    rows = list(db.aql.execute("""
        FOR e IN MODIFIED
          LET c = DOCUMENT(e._from)
          FILTER c.repo == @repo
          LET m = DOCUMENT(e._to)
          FILTER m != null
          RETURN {module: m.label, epoch: m.design_epoch}
    """, bind_vars={"repo": canonical_repo}))
    epochs_seen = list(db.aql.execute("""
        FOR ep IN DesignEpoch FILTER ep.repo == @repo SORT ep.start_ts ASC
          RETURN ep.label
    """, bind_vars={"repo": canonical_repo}))
    matrix = defaultdict(lambda: defaultdict(int))
    for r in rows:
        matrix[r["module"]][r["epoch"] or "unknown"] += 1
    # Rank modules by total churn, cap to top 40 for a readable heatmap
    totals = {m: sum(v.values()) for m, v in matrix.items()}
    top_modules = sorted(totals, key=totals.get, reverse=True)[:40]
    return top_modules, epochs_seen, matrix


def author_impact(db, canonical_repo: str):
    rows = list(db.aql.execute("""
        FOR a IN Author
          LET commits = (FOR e IN AUTHORED FILTER e._from == a._id
                          LET c = DOCUMENT(e._to)
                          FILTER c != null AND c.repo == @repo
                          RETURN c)
          FILTER LENGTH(commits) > 0
          LET modules = (FOR e IN MAINTAINS FILTER e._from == a._id
                          LET m = DOCUMENT(e._to)
                          FILTER m != null AND m.repo == @repo
                          RETURN {module: m.label, score: e.maintenance_score})
          LET ts = commits[*].valid_from_ts
          RETURN {
            author: a.name, commit_count: LENGTH(commits),
            modules_maintained: LENGTH(modules),
            top_modules: (FOR mm IN modules SORT mm.score DESC LIMIT 5 RETURN mm.module),
            first_ts: MIN(ts), last_ts: MAX(ts),
          }
    """, bind_vars={"repo": canonical_repo}))
    return sorted(rows, key=lambda r: (r["modules_maintained"], r["commit_count"]), reverse=True)


def render_heatmap_html(repo_name, modules, epochs, matrix, out_path):
    import plotly.graph_objects as go
    z = [[matrix[m].get(e, 0) for e in epochs] for m in modules]
    fig = go.Figure(data=go.Heatmap(
        z=z, x=epochs, y=modules, colorscale="OrRd",
        colorbar=dict(title="commits"),
    ))
    fig.update_layout(
        title=f"Module Churn Heatmap — {repo_name} (top {len(modules)} modules by total churn)",
        xaxis_title="design epoch", yaxis_title="module",
        height=max(500, 22 * len(modules)), template="plotly_dark",
    )
    fig.write_html(out_path, include_plotlyjs="cdn")


def render_author_markdown(repo_name, authors, out_path):
    import datetime
    lines = [f"# Author Impact Report — {repo_name}\n",
             f"*Generated: {datetime.datetime.utcnow().isoformat()}Z*\n",
             "| Author | Commits | Modules maintained | Top modules | First activity | Last activity |",
             "|---|---|---|---|---|---|"]
    for a in authors[:50]:
        fmt = lambda ts: datetime.datetime.utcfromtimestamp(ts).date().isoformat() if ts else "—"
        lines.append(
            f"| {a['author']} | {a['commit_count']} | {a['modules_maintained']} "
            f"| {', '.join(a['top_modules']) or '—'} | {fmt(a['first_ts'])} | {fmt(a['last_ts'])} |"
        )
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="UC4 churn heatmap + author impact report")
    parser.add_argument("--repo", help="Repo name from repo_registry.yaml (e.g. or1200)")
    parser.add_argument("--all", action="store_true", help="Run for all registered repos")
    args = parser.parse_args()

    if not args.repo and not args.all:
        parser.print_help()
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    db = get_temporal_db()
    repos = [r["name"] for r in REPO_REGISTRY] if args.all else [args.repo]

    for name in repos:
        canonical = _canonical_repo(name)
        if not canonical:
            print(f"[churn-report] Unknown repo '{name}' — skipping")
            continue
        print(f"\n[churn-report] {name} ({canonical}) …")

        modules, epochs, matrix = churn_matrix(db, canonical)
        if modules and epochs:
            heatmap_path = os.path.join(OUT_DIR, f"{name}_churn_heatmap.html")
            render_heatmap_html(name, modules, epochs, matrix, heatmap_path)
            print(f"  wrote {heatmap_path} ({len(modules)} modules x {len(epochs)} epochs)")
        else:
            print("  no commit/epoch data — skipping heatmap")

        authors = author_impact(db, canonical)
        if authors:
            author_path = os.path.join(OUT_DIR, f"{name}_author_impact.md")
            render_author_markdown(name, authors, author_path)
            print(f"  wrote {author_path} ({len(authors)} authors)")
        else:
            print("  no author data — skipping report")


if __name__ == "__main__":
    main()
