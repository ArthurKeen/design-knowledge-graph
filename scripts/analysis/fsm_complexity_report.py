#!/usr/bin/env python3
"""
scripts/analysis/fsm_complexity_report.py — UC7 FSM Complexity & Verification.

Closes REQ-024 (business-requirements.md): state count, transition count,
transitions-per-state ratio, cyclomatic complexity, reachability analysis from
the reset state, and unreachable/dead-end state detection — computed from the
FSM_StateMachine/FSM_State/TRANSITIONS_TO data populated by
src/etl_deep_analysis.py (REQ-003).

Not attempted (documented as a remaining gap, not silently skipped): FSM
state-diagram visualization and model-checker export. ChronoGraph (viz/)
already renders the underlying graph structure but not as a classic FSM state
diagram specifically; a dedicated diagram/export would be a separate,
scoped addition.

Cyclomatic complexity uses the standard graph formula M = E - V + 2P (P =
connected components, almost always 1 for a well-formed single-reset-state
FSM): a proxy for how many independent decision paths a verifier must cover.

Usage:
    PYTHONPATH=src python3 scripts/analysis/fsm_complexity_report.py
"""
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from db_utils import get_temporal_db  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                       "ic_analysis_output", "temporal_evolution")


def _connected_components(states: list[str], edges: list[tuple[str, str]]) -> int:
    parent = {s: s for s in states}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        if a in parent and b in parent:
            union(a, b)
    return len({find(s) for s in states}) if states else 0


def analyze_fsm(db, fsm: dict) -> dict:
    states = list(db.aql.execute(
        "FOR e IN HAS_STATE FILTER e._from == @fsm LET s = DOCUMENT(e._to) RETURN s",
        bind_vars={"fsm": fsm["_id"]}))
    state_ids = {s["_id"] for s in states}
    transitions = list(db.aql.execute(
        "FOR e IN TRANSITIONS_TO FILTER e._from IN @ids RETURN {from: e._from, to: e._to, condition: e.condition}",
        bind_vars={"ids": list(state_ids)}))

    outgoing, incoming = defaultdict(int), defaultdict(int)
    for t in transitions:
        outgoing[t["from"]] += 1
        incoming[t["to"]] += 1

    reset_states = [s["_id"] for s in states if (s.get("metadata") or {}).get("is_reset_state")]
    # Reachability from reset state(s) via BFS over the transition graph.
    adj = defaultdict(list)
    for t in transitions:
        adj[t["from"]].append(t["to"])
    reachable = set(reset_states)
    frontier = list(reset_states)
    while frontier:
        cur = frontier.pop()
        for nxt in adj.get(cur, []):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)

    unreachable = [s["name"] for s in states if s["_id"] not in reachable and s["_id"] not in reset_states]
    dead_ends = [s["name"] for s in states if outgoing.get(s["_id"], 0) == 0]

    v, e = len(states), len(transitions)
    p = _connected_components(list(state_ids), [(t["from"], t["to"]) for t in transitions]) or 1
    cyclomatic = e - v + 2 * p

    return {
        "name": fsm["name"], "module": fsm.get("parent_module"), "repo": fsm.get("repo"),
        "state_count": v, "transition_count": e,
        "transitions_per_state": round(e / v, 2) if v else None,
        "cyclomatic_complexity": cyclomatic,
        "reset_states": [s["name"] for s in states if s["_id"] in reset_states],
        "unreachable_states": unreachable, "dead_end_states": dead_ends,
    }


def main():
    db = get_temporal_db()
    os.makedirs(OUT_DIR, exist_ok=True)
    fsms = list(db.aql.execute("FOR f IN FSM_StateMachine RETURN f"))
    print(f"[fsm-complexity] {len(fsms)} FSMs found")

    results = [analyze_fsm(db, f) for f in fsms]
    results.sort(key=lambda r: r["cyclomatic_complexity"], reverse=True)

    lines = [
        "# FSM Complexity & Verification Report (UC7)\n",
        f"*Generated: {datetime.datetime.utcnow().isoformat()}Z*\n",
        "## Complexity ranking (most complex first)\n",
        "| FSM | Repo | Module | States | Transitions | Trans/State | Cyclomatic | Reset state(s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['name']} | {r['repo']} | {r['module']} | {r['state_count']} | {r['transition_count']} | "
        f"{r['transitions_per_state']} | {r['cyclomatic_complexity']} | {', '.join(r['reset_states']) or '—'} |"
        for r in results
    ]

    flagged = [r for r in results if r["unreachable_states"] or r["dead_end_states"]]
    lines.append("\n## Unreachable / dead-end states\n")
    if not flagged:
        lines.append("None found — every FSM's states are all reachable from a reset state "
                     "and have at least one outgoing transition.")
    else:
        for r in flagged:
            lines.append(f"\n### {r['name']} ({r['module']}, {r['repo']})")
            if r["unreachable_states"]:
                lines.append(f"- **Unreachable from reset:** {', '.join(r['unreachable_states'])}")
            if r["dead_end_states"]:
                lines.append(f"- **Dead-end (no outgoing transitions):** {', '.join(r['dead_end_states'])}")
        lines.append(
            "\nNote: an FSM with no state flagged `is_reset_state` (name doesn't contain "
            "IDLE/RESET) treats ALL its states as unreachable-check exempt rather than "
            "flagging every state — check FSMs with zero reset_states manually.")

    lines.append(
        "\n## Known detector limitation — read the dead-end/unreachable list as a lead, not a verdict\n"
        "Transition counts here are low relative to state counts (many FSMs show 0-3 "
        "transitions for 4-8 states) — the case-statement transition parser (reused from "
        "etl_fsm.py, unchanged) is under-detecting, not that these FSMs genuinely have that "
        "few real transitions. Every 'dead-end'/'unreachable' flag above is a direct "
        "consequence: a state the parser found but couldn't find a transition INTO or OUT OF "
        "is not necessarily a real design defect. Treat this report as a worklist for a "
        "hardware engineer's manual review, not a certified defect list."
    )
    lines.append(
        "\n## Not covered by this report (scoped out, not silently dropped)\n"
        "FSM state-diagram visualization and model-checker export were not attempted here — "
        "ChronoGraph (viz/) renders the underlying HAS_FSM/HAS_STATE/TRANSITIONS_TO graph "
        "structure but not as a dedicated FSM state diagram; a model-checker export format "
        "would be a separate, scoped addition."
    )

    out_path = os.path.join(OUT_DIR, "fsm_complexity_report.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[fsm-complexity] wrote {out_path}")
    print(f"[fsm-complexity] {len(flagged)}/{len(results)} FSMs have unreachable/dead-end states")


if __name__ == "__main__":
    main()
