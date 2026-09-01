#!/usr/bin/env python3
"""
scripts/analysis/clock_bus_report.py — UC10 Cross-Domain Analysis.

Closes REQ-025 (business-requirements.md): clock domain map, CDC signal
report, bus interface usage by module, and modules spanning multiple clock
domains (high CDC risk) — computed from the ClockDomain/CLOCKED_BY/
CROSSES_DOMAIN/BusInterface/IMPLEMENTS/PART_OF_BUS data populated by
src/etl_deep_analysis.py (REQ-003).

CDC detection caveat (documented, not hidden): a signal is flagged as
crossing domains when it's driven inside an always block whose sensitivity
list names one clock and read inside a block naming a different one. This
can occasionally misfire on a non-clock signal that happens to appear
directly after a `posedge`/`negedge` keyword in an unrelated construct (one
such case was found and left in — see module docstring of
etl_deep_analysis.py); treat CDC report entries as a prioritized review list
for a hardware engineer, not a certified defect list.

Usage:
    PYTHONPATH=src python3 scripts/analysis/clock_bus_report.py
"""
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from db_utils import get_temporal_db  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                       "ic_analysis_output", "temporal_evolution")


def clock_domain_map(db) -> dict:
    """repo -> clock_name -> {chunks_clocked, modules}"""
    rows = list(db.aql.execute("""
        FOR e IN CLOCKED_BY
          LET clk = DOCUMENT(e._to)
          LET chunk = DOCUMENT(e._from)
          FILTER clk != null AND chunk != null
          RETURN {repo: e.repo, clock: clk.name, module: chunk.parent_module}
    """))
    by_repo = defaultdict(lambda: defaultdict(lambda: {"chunks": 0, "modules": set()}))
    for r in rows:
        entry = by_repo[r["repo"]][r["clock"]]
        entry["chunks"] += 1
        entry["modules"].add(r["module"])
    return by_repo


def cdc_report(db) -> list:
    rows = list(db.aql.execute("""
        FOR e IN CROSSES_DOMAIN
          LET sig = DOCUMENT(e._from)
          LET clk = DOCUMENT(e._to)
          FILTER sig != null AND clk != null
          RETURN {repo: e.repo, signal: sig.name, module: sig.parent_module,
                  driven_by: e.driven_by, read_in: clk.name}
    """))
    return rows


def bus_usage(db) -> list:
    rows = list(db.aql.execute("""
        FOR e IN IMPLEMENTS
          LET bus = DOCUMENT(e._to)
          LET mod = DOCUMENT(e._from)
          FILTER bus != null AND mod != null
          RETURN {repo: e.repo, module: mod.name, interface: bus.interface_type, port_count: bus.port_count}
    """))
    return rows


def multi_domain_modules(clock_map: dict) -> dict:
    """repo -> [modules that appear under >1 clock in the domain map]"""
    by_repo = {}
    for repo, clocks in clock_map.items():
        module_clocks = defaultdict(set)
        for clk_name, entry in clocks.items():
            for m in entry["modules"]:
                module_clocks[m].add(clk_name)
        by_repo[repo] = {m: sorted(cs) for m, cs in module_clocks.items() if len(cs) > 1}
    return by_repo


def main():
    db = get_temporal_db()
    os.makedirs(OUT_DIR, exist_ok=True)

    clock_map = clock_domain_map(db)
    cdc = cdc_report(db)
    bus = bus_usage(db)
    multi_domain = multi_domain_modules(clock_map)

    lines = [
        "# Clock Domain & Bus Interface Report (UC10)\n",
        f"*Generated: {datetime.datetime.utcnow().isoformat()}Z*\n",
        "## Clock domain map\n",
        "| Repo | Clock | Always-blocks clocked | Modules |",
        "|---|---|---|---|",
    ]
    for repo, clocks in sorted(clock_map.items()):
        for clk_name, entry in sorted(clocks.items(), key=lambda x: -x[1]["chunks"]):
            mods = sorted(entry["modules"])
            mods_str = ", ".join(mods[:6]) + (f" (+{len(mods)-6} more)" if len(mods) > 6 else "")
            lines.append(f"| {repo} | {clk_name} | {entry['chunks']} | {mods_str} |")

    lines.append("\n## Modules spanning multiple clock domains (CDC risk)\n")
    any_multi = False
    for repo, mods in sorted(multi_domain.items()):
        if not mods:
            continue
        any_multi = True
        lines.append(f"\n### {repo}")
        for m, clks in sorted(mods.items()):
            lines.append(f"- **{m}**: {', '.join(clks)}")
    if not any_multi:
        lines.append("None found — no module's always blocks name more than one distinct clock.")

    lines.append("\n## CDC signal report\n")
    if not cdc:
        lines.append("No cross-domain signals detected.")
    else:
        lines.append("| Repo | Signal | Module | Driven by | Read in |")
        lines.append("|---|---|---|---|---|")
        for c in cdc:
            lines.append(f"| {c['repo']} | {c['signal']} | {c['module']} | "
                         f"{', '.join(c['driven_by'])} | {c['read_in']} |")
        lines.append(
            "\n**Synchronization status:** not tracked — the graph records that a crossing "
            "exists, not whether it passes through a synchronizer (a 2-flop synchronizer, "
            "async FIFO, or handshake would need to be identified from surrounding code, "
            "which this detector does not attempt). Treat every row as needing manual review.")

    lines.append("\n## Bus interface usage by module\n")
    lines.append("| Repo | Module | Interface | Port count |")
    lines.append("|---|---|---|---|")
    for b in sorted(bus, key=lambda x: (x["repo"], -x["port_count"])):
        lines.append(f"| {b['repo']} | {b['module']} | {b['interface']} | {b['port_count']} |")

    out_path = os.path.join(OUT_DIR, "clock_bus_report.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[clock-bus-report] wrote {out_path}")
    print(f"[clock-bus-report] {sum(len(m) for m in multi_domain.values())} multi-domain modules, "
          f"{len(cdc)} CDC signals, {len(bus)} bus interfaces")


if __name__ == "__main__":
    main()
