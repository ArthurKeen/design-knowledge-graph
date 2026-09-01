#!/usr/bin/env python3
"""
scripts/analysis/bridge_quality_report.py — UC6 Semantic Bridge Quality:
coverage dashboard, resolution score distribution, gap report.

Closes REQ-023 (business-requirements.md). RESOLVED_TO edges already carry
score/method fields (rtl_semantic_bridge.py); this script is the missing
reporting layer on top of that real data.

Coverage is reported BOTH ways, because they answer different questions and
the gap between them is itself the headline finding (see REQ-012 in the
drift report this script was written to close):

  - "RTL-token coverage": resolved / (RTL_Port + RTL_Signal) per repo.
    Denominator includes every internal wire/register; open-source hardware
    specs document architectural CONCEPTS, not individual net names, so this
    number is structurally small (measured ~1-5%) regardless of matcher
    quality.
  - "Concept-grounding rate": of RTL-relevant Golden Entities (documented
    concepts), what fraction have >=1 resolved RTL reference at all. This is
    the metric that reflects whether the semantic bridge is doing its job —
    connecting documented concepts to their RTL realization — without being
    dominated by the undocumentable-internal-wire problem above.

Usage:
    PYTHONPATH=src python3 scripts/analysis/bridge_quality_report.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from db_utils import get_temporal_db  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                       "ic_analysis_output", "temporal_evolution")

RTL_RELEVANT_TYPES = ["REGISTER", "SIGNAL", "HARDWARE_INTERFACE", "CLOCK_DOMAIN",
                      "PROCESSOR_COMPONENT", "MEMORY_UNIT"]


def repo_prefixes(db):
    """Discover live {PREFIX}_Golden_Entities collections rather than hardcoding."""
    return sorted({c["name"].removesuffix("_Golden_Entities")
                  for c in db.collections() if c["name"].endswith("_Golden_Entities")})


def token_coverage(db, prefix):
    ports = list(db.aql.execute("FOR p IN RTL_Port FILTER p.repo == @r RETURN 1", bind_vars={"r": prefix}))
    signals = list(db.aql.execute("FOR s IN RTL_Signal FILTER s.repo == @r RETURN 1", bind_vars={"r": prefix}))
    resolved = list(db.aql.execute("FOR e IN RESOLVED_TO FILTER e.repo == @r RETURN e.score",
                                   bind_vars={"r": prefix}))
    denom = len(ports) + len(signals)
    return {
        "ports": len(ports), "signals": len(signals), "resolved": len(resolved),
        "coverage_pct": round(100 * len(resolved) / denom, 2) if denom else None,
        "scores": resolved,
    }


def concept_grounding(db, prefix):
    total = list(db.aql.execute(f"""
        FOR g IN {prefix}_Golden_Entities FILTER g.labels[1] IN @t RETURN 1
    """, bind_vars={"t": RTL_RELEVANT_TYPES}))
    grounded = list(db.aql.execute(f"""
        FOR g IN {prefix}_Golden_Entities FILTER g.labels[1] IN @t
          FILTER LENGTH(FOR e IN RESOLVED_TO FILTER e._to == g._id LIMIT 1 RETURN 1) > 0
          RETURN 1
    """, bind_vars={"t": RTL_RELEVANT_TYPES}))
    return {"total_concepts": len(total), "grounded": len(grounded),
            "grounding_pct": round(100 * len(grounded) / len(total), 2) if total else None}


def score_histogram(scores, buckets=(0.70, 0.80, 0.90, 0.95, 1.01)):
    hist = {f"[{lo:.2f}-{hi:.2f})": 0 for lo, hi in zip((0.0,) + buckets[:-1], buckets)}
    labels = list(hist)
    for s in scores:
        for i, hi in enumerate(buckets):
            if s < hi:
                hist[labels[i]] += 1
                break
    return hist


def gap_report(db, prefix, limit=25):
    """Top unresolved ports/signals by frequency of their (normalized) name across
    modules — the ones worth prioritizing for a future matcher improvement."""
    return list(db.aql.execute("""
        FOR p IN RTL_Port
          FILTER p.repo == @r
          FILTER LENGTH(FOR e IN RESOLVED_TO FILTER e._from == p._id LIMIT 1 RETURN 1) == 0
          COLLECT name = LOWER(p.name) WITH COUNT INTO n
          SORT n DESC LIMIT @limit
          RETURN {name, occurrences: n}
    """, bind_vars={"r": prefix, "limit": limit}))


def main():
    db = get_temporal_db()
    os.makedirs(OUT_DIR, exist_ok=True)
    prefixes = repo_prefixes(db)
    print(f"[bridge-quality] repos: {prefixes}")

    lines = [
        "# Semantic Bridge Quality Report (UC6)\n",
        f"*Generated: {datetime.datetime.utcnow().isoformat()}Z*\n",
        "## Coverage — two framings\n",
        "RTL-token coverage counts every port/signal in the repo as the denominator; "
        "concept-grounding counts only documented (Golden Entity) concepts. See module "
        "docstring for why both are reported.\n",
        "| Repo | RTL-token coverage | Concept-grounding rate | Resolved edges | Ports | Signals | Concepts |",
        "|---|---|---|---|---|---|---|",
    ]
    all_scores = []
    gap_sections = []
    for prefix in prefixes:
        tc = token_coverage(db, prefix)
        cg = concept_grounding(db, prefix)
        all_scores.extend(tc["scores"])
        lines.append(
            f"| {prefix} | {tc['coverage_pct']}% | {cg['grounding_pct']}% | {tc['resolved']} "
            f"| {tc['ports']} | {tc['signals']} | {cg['total_concepts']} |"
        )
        gaps = gap_report(db, prefix)
        if gaps:
            gap_sections.append(f"\n### Top unresolved ports — {prefix}\n")
            gap_sections.append("| Port name | Occurrences across modules |")
            gap_sections.append("|---|---|")
            gap_sections += [f"| `{g['name']}` | {g['occurrences']} |" for g in gaps]

    hist = score_histogram(all_scores)
    lines.append("\n## Resolution score distribution (all repos)\n")
    lines.append("| Score bucket | Count |")
    lines.append("|---|---|")
    lines += [f"| {k} | {v} |" for k, v in hist.items()]
    lines.append(f"\nTotal resolved edges: {len(all_scores)}")

    lines.append("\n## Gap report — most common unresolved port names\n")
    lines.append(
        "Ports with no RESOLVED_TO match, ranked by how many modules repeat the same "
        "name — these are the highest-leverage targets for a future matcher improvement "
        "(a single new alias/rule resolves every occurrence at once)."
    )
    lines += gap_sections

    out_path = os.path.join(OUT_DIR, "bridge_quality_report.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[bridge-quality] wrote {out_path}")


if __name__ == "__main__":
    main()
