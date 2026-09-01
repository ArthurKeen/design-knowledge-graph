#!/usr/bin/env python3
"""
src/etl_deep_analysis.py — closes REQ-003 (business-requirements.md): populates
the 8 node types (RTL_Assign, FSM_StateMachine, FSM_State, RTL_Memory,
MemoryPort, ClockDomain, BusInterface, Operator) that extraction code existed
for (etl_fsm.py, etl_clocks.py, etl_bus.py, etl_memory_access.py,
etl_operators.py, etl_assigns.py) but was never wired into the multi-repo
temporal pipeline — the legacy scripts read from local JSON files
(rtl_nodes.json, always_nodes.json, memory_nodes.json, ...) produced by a
single-repo pipeline that no longer exists.

Design: reuse what's genuinely reusable, replace only the I/O layer.
  - FSMExtractor (etl_fsm.py) and AssignExtractor (etl_assigns.py) are pure
    text-processing classes — they take module_name/module_body/module_key/
    resolver and have no file-I/O coupling of their own. Reused unchanged.
  - BUS_PREFIXES (etl_bus.py) and OPERATOR_MAP (etl_operators.py) are reused
    unchanged; the extraction logic around them is rewritten to query live
    RTL_Port / RTL_LogicChunk instead of local JSON.
  - NodeResolver (utils.py) is replaced by _LiveNodeResolver below, which
    loads the same port_ids/signal_ids/module_ids sets from ArangoDB instead
    of local JSON, preserving the exact id-resolution semantics FSMExtractor
    and AssignExtractor depend on.
  - Memory-array detection (RTL_Memory, MemoryPort) has NO legacy precedent —
    etl_memory_access.py assumed RTL_Memory nodes already existed from some
    other extractor, but none does; this is new detection logic (a 2D reg
    declaration: `reg [width] name [depth];`).
  - Clock-domain detection (etl_clocks.py) assumed always_nodes.json/
    always_edges.json (CLOCKED_BY edges) already existed from some other
    extractor; RTL_Always/CLOCKED_BY have never existed live either. Rewritten
    to detect the clock signal directly from each always block's own
    sensitivity list (`@(posedge clk)`) rather than depending on a prior
    CLOCKED_BY pass.

All detectors work per deep-structural module ({PREFIX}_modulename, e.g.
OR1200_or1200_cpu) using RTL_Module.code_content (full text — RTL_LogicChunk.
code is truncated to 500/200 chars by etl_rtl.py, which would silently drop
FSM case-statement bodies and clock-domain dataflow in larger blocks). Each
detector re-derives RTL_LogicChunk._key using the SAME regex + enumeration
order etl_rtl.py used (RE_ALWAYS/RE_ASSIGN over clean_body), so results link
back to the EXISTING logic-chunk nodes rather than creating duplicates.

Usage:
    PYTHONPATH=src python3 src/etl_deep_analysis.py --repo OR1200
    PYTHONPATH=src python3 src/etl_deep_analysis.py --all
    PYTHONPATH=src python3 src/etl_deep_analysis.py --all --only bus,operators
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from db_utils import get_temporal_db  # noqa: E402
from utils import get_edge_key, sanitize_id  # noqa: E402
from etl_fsm import FSMExtractor  # noqa: E402
from etl_assigns import AssignExtractor  # noqa: E402
from etl_bus import BUS_PREFIXES  # noqa: E402
from etl_operators import OPERATOR_MAP  # noqa: E402
from etl_clocks import extract_signals_from_code  # noqa: E402
from utils import VerilogParser  # noqa: E402

# Same patterns etl_rtl.py uses to enumerate RTL_LogicChunk nodes, so chunk
# ids here match the ones already in the live DB.
RE_ALWAYS = re.compile(
    r'^\s*always(?:_ff|_comb|_latch)?\s*(?:@\s*\(.*?\)\s*)?(?:begin\b.*?end\b|[^;]*?;)',
    re.MULTILINE | re.DOTALL)
# Byte-for-byte identical to etl_rtl.py's RE_ASSIGN: MULTILINE only, no
# DOTALL, so `.` cannot cross a newline. A multi-line assign (no semicolon on
# the same line as the `assign` keyword) therefore matches NOTHING at that
# position and is silently skipped by etl_rtl.py -- matching that exactly
# (rather than a more permissive pattern) is what keeps this script's
# re-derived chunk-index numbering in sync with the chunks etl_rtl.py already
# wrote to RTL_LogicChunk.
RE_ASSIGN_CHUNK = re.compile(r'^\s*assign\s+.*?;', re.MULTILINE)
RE_SENSITIVITY = re.compile(r'@\s*\(([^)]*)\)')
RE_EDGE_SIGNAL = re.compile(r'\b(?:posedge|negedge)\s+(\w+)', re.IGNORECASE)
# `reg [width-1:0] name [depth-1:0];` — a SECOND bracket group after the name
# is what distinguishes a memory array from a plain register declaration.
RE_MEMORY_DECL = re.compile(
    r'^\s*reg\s*(?:\[[^\]]*\]\s*)?(\w+)\s*\[([^\]]*)\]\s*;', re.MULTILINE)

REPO_PREFIXES = ["OR1200", "MOR1KX", "MAROCCHINO", "IBEX"]


# ---------------------------------------------------------------------------
# Live-backed NodeResolver replacement
# ---------------------------------------------------------------------------

class _LiveNodeResolver:
    """Drop-in replacement for utils.NodeResolver, backed by ArangoDB instead
    of local JSON. FSMExtractor/AssignExtractor call resolve_id(self.module_name,
    ...) — i.e. the BARE module name (e.g. 'or1200_freeze'), matching the old
    single-repo pipeline where keys had no repo prefix. Real live RTL_Port/
    RTL_Signal _keys ARE prefixed (e.g. 'OR1200_or1200_freeze.clk'), so every
    incoming module_id is normalized to the prefixed form before lookup.

    Also supplies a data_dir attribute purely so FSMExtractor.extract()'s
    `os.path.exists(os.path.join(self.resolver.data_dir, 'rtl_nodes.json'))`
    check (a leftover local-file dependency for its IMPLEMENTED_BY heuristic)
    safely evaluates to False instead of raising — that edge type is populated
    separately in extract_fsms() below using the same code already fetched
    from ArangoDB, so nothing is lost."""

    def __init__(self, db, prefix: str):
        self.prefix = prefix
        self.data_dir = "/__no_local_rtl_nodes_json__"
        self.port_ids = {r["_key"] for r in db.aql.execute(
            "FOR p IN RTL_Port FILTER p.repo == @r RETURN {_key: p._key}", bind_vars={"r": prefix})}
        self.signal_ids = {r["_key"] for r in db.aql.execute(
            "FOR s IN RTL_Signal FILTER s.repo == @r RETURN {_key: s._key}", bind_vars={"r": prefix})}
        self.module_ids = {r["_key"] for r in db.aql.execute(
            "FOR m IN RTL_Module FILTER m.repo == @r RETURN {_key: m._key}", bind_vars={"r": prefix})}
        self.memory_ids: set = set()       # populated after memory detection runs
        # RTL_Parameter uses the same "{module_id}.{name}" key convention as
        # RTL_Port/RTL_Signal (confirmed live: 'OR1200_or1200_iwb_biu.dw').
        param_rows = list(db.aql.execute(
            "FOR p IN RTL_Parameter FILTER p.repo == @r RETURN {_key: p._key, name: p.name}",
            bind_vars={"r": prefix}))
        self.parameter_ids = {r["_key"] for r in param_rows}
        self.global_parameters = {r["name"]: r["_key"] for r in param_rows if r.get("name")}

    def _full_module_id(self, module_id: str) -> str:
        return module_id if module_id.startswith(f"{self.prefix}_") else f"{self.prefix}_{module_id}"

    def resolve_id(self, module_id: str, name: str) -> str:
        module_id = self._full_module_id(module_id)
        clean_name = sanitize_id(name)
        port_id = f"{module_id}.{clean_name}"
        if port_id in self.port_ids:
            return port_id
        sig_id = f"{module_id}.sig_{clean_name}"
        if sig_id in self.signal_ids:
            return sig_id
        sig_id2 = f"{module_id}.{clean_name}"
        if sig_id2 in self.signal_ids:
            return sig_id2
        # Module-scoped parameter check BEFORE the global name map: many
        # modules independently declare same-named parameters (e.g. 'dw' for
        # data-width), so the flat global_parameters map can point at the
        # wrong module's parameter if checked first.
        param_id = f"{module_id}.{clean_name}"
        if param_id in self.parameter_ids:
            return param_id
        if clean_name in self.global_parameters:
            return self.global_parameters[clean_name]
        return sanitize_id(f"{module_id}.{clean_name}")

    def kind_of(self, resolved_id: str) -> str:
        """RTL_Port | RTL_Signal | RTL_Memory | RTL_Parameter | unknown — for
        edges whose target collection depends on what resolve_id() matched."""
        if resolved_id in self.port_ids:
            return "RTL_Port"
        if resolved_id in self.signal_ids:
            return "RTL_Signal"
        if resolved_id in self.memory_ids:
            return "RTL_Memory"
        if resolved_id in self.parameter_ids:
            return "RTL_Parameter"
        return "unknown"


def _rtl_root(prefix: str) -> str | None:
    """Local clone root + rtl subdir for a repo prefix, from repo_registry.yaml
    (via config_temporal), so full source can be read directly from disk."""
    from config_temporal import REPO_REGISTRY, get_local_path
    cfg = next((r for r in REPO_REGISTRY if r["name"].upper() == prefix.upper()), None)
    if not cfg:
        return None
    return os.path.join(get_local_path(cfg), cfg.get("rtl_path", ""))


def _fetch_modules(db, prefix: str) -> list[dict]:
    """RTL_Module.code_content is truncated to 2000 chars by etl_rtl.py ("avoid
    huge docs") — enough for RTL_LogicChunk's already-shorter truncated chunks,
    but not for detectors that need a module's FULL body (FSM case statements,
    clock-domain dataflow can live well past 2000 chars in larger modules).
    Read the full source directly from the local clone on disk instead
    (already checked out for the temporal ETL); fall back to the DB's
    truncated code_content only if the file can't be found (e.g. clone moved).
    """
    rows = list(db.aql.execute("""
        FOR m IN RTL_Module FILTER m.repo == @r AND m.code_content != null
          RETURN {key: m._key, name: m.name, code: m.code_content, file: m.file}
    """, bind_vars={"r": prefix}))
    root = _rtl_root(prefix)
    if root and os.path.isdir(root):
        # Build a filename -> full path index once (handles nested subdirs,
        # e.g. ibex's rtl/ has no /verilog subdir and some vendor files nest
        # under prim/ etc.) rather than assuming a flat directory.
        by_name = {}
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                by_name.setdefault(fn, os.path.join(dirpath, fn))
        for m in rows:
            m["full_file"] = m["code"]  # fallback if the file can't be read below
            path = by_name.get(m.get("file") or "")
            if not path:
                continue
            try:
                with open(path, "r", errors="replace") as f:
                    full_file = f.read()
            except OSError:
                continue  # keep the truncated DB fallback
            m["full_file"] = full_file
            # A file can bundle multiple modules (common for OR1200's FPU
            # helpers) -- scope to just THIS module's own body for structural
            # detectors, or they'd see other modules' code too and massively
            # over-count. Same VerilogParser etl_rtl.py itself uses.
            # full_file is kept separately: FSMExtractor's file_content param
            # specifically needs the whole file, since `define macros (e.g.
            # OR1200's FSM state encodings) commonly live in the file's
            # preamble, BEFORE the `module` keyword -- outside module_body.
            for mod_name, mod_body in VerilogParser.get_module_bodies(full_file):
                if mod_name == m["name"]:
                    m["code"] = mod_body
                    break
            # else: name not found in this file (unusual) -- keep truncated fallback
    return rows


def _clean_body(code: str) -> str:
    code = re.sub(r'//.*', '', code)
    return re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)


def _logic_chunk_ids(mod_key: str, code: str) -> tuple[dict, dict]:
    """Reconstruct RTL_LogicChunk _keys the same way etl_rtl.py does, indexed
    by enumeration order, so results reference existing chunk nodes."""
    clean = _clean_body(code)
    always = {i: (m.group(0).strip(), f"{mod_key}.always_{i}")
             for i, m in enumerate(RE_ALWAYS.finditer(clean))}
    assigns = {i: (m.group(0).strip(), f"{mod_key}.assign_{i}")
              for i, m in enumerate(RE_ASSIGN_CHUNK.finditer(clean))}
    return always, assigns


def _upsert(db, col_name: str, docs: list[dict], edge: bool = False) -> int:
    if not docs:
        return 0
    if not db.has_collection(col_name):
        db.create_collection(col_name, edge=edge)
        print(f"  [deep-analysis] created collection: {col_name} ({'edge' if edge else 'vertex'})")
    db.aql.execute(
        "FOR doc IN @docs INSERT doc INTO @@col OPTIONS {overwriteMode: 'update'} LET x = 1 RETURN x",
        bind_vars={"docs": docs, "@col": col_name})
    return len(docs)


# ---------------------------------------------------------------------------
# 1. Bus interfaces — group RTL_Port by module + prefix, >=3 ports -> a bus
# ---------------------------------------------------------------------------

def extract_bus_interfaces(db, prefix: str) -> tuple[int, int]:
    ports = list(db.aql.execute(
        "FOR p IN RTL_Port FILTER p.repo == @r RETURN {name: p.name, parent_module: p.parent_module, key: p._key}",
        bind_vars={"r": prefix}))
    groups = defaultdict(lambda: defaultdict(list))  # module -> bus_prefix -> [port_key]
    for p in ports:
        for bus_prefix in BUS_PREFIXES:
            if p["name"].startswith(bus_prefix):
                groups[p["parent_module"]][bus_prefix].append(p["key"])
                break

    nodes, edges = [], []
    for module_name, bus_groups in groups.items():
        mod_key = f"{prefix}_{module_name}"
        for bus_prefix, port_keys in bus_groups.items():
            if len(port_keys) < 3:
                continue
            bus_key = sanitize_id(f"{mod_key}.bus_{bus_prefix.strip('_')}")
            nodes.append({
                "_key": bus_key, "type": "BusInterface",
                "name": f"{bus_prefix.strip('_').upper()} Interface",
                "interface_type": BUS_PREFIXES[bus_prefix], "repo": prefix,
                "parent_module": mod_key, "port_count": len(port_keys),
            })
            edges.append({
                "_key": get_edge_key(mod_key, bus_key, "IMPLEMENTS"),
                "_from": f"RTL_Module/{mod_key}", "_to": f"BusInterface/{bus_key}",
                "type": "IMPLEMENTS", "repo": prefix,
            })
            for pk in port_keys:
                edges.append({
                    "_key": get_edge_key(pk, bus_key, "PART_OF_BUS"),
                    "_from": f"RTL_Port/{pk}", "_to": f"BusInterface/{bus_key}",
                    "type": "PART_OF_BUS", "repo": prefix,
                })
    n = _upsert(db, "BusInterface", nodes)
    e = _upsert(db, "IMPLEMENTS", [x for x in edges if x["type"] == "IMPLEMENTS"], edge=True)
    e += _upsert(db, "PART_OF_BUS", [x for x in edges if x["type"] == "PART_OF_BUS"], edge=True)
    return n, e


# ---------------------------------------------------------------------------
# 2. Operators — detect arithmetic/logic operators in each logic chunk
# ---------------------------------------------------------------------------

def extract_operators(db, prefix: str, modules: list[dict]) -> tuple[int, int]:
    op_nodes, has_op_edges, uses_op_edges = {}, {}, []
    for mod in modules:
        mod_key = mod["key"]
        always, assigns = _logic_chunk_ids(mod_key, mod["code"])
        # always/assigns are BOTH keyed by their own 0-based enumeration index
        # -- {**always, **assigns} would silently overwrite/drop entries on
        # colliding integer keys. Chain the values instead of merging dicts.
        for code, chunk_id in list(always.values()) + list(assigns.values()):
            chunk_key = sanitize_id(chunk_id)
            for pattern, op_type in OPERATOR_MAP.items():
                if pattern == r'<=':
                    if not re.search(r'\(\s*.*?' + pattern + r'.*?\)', code):
                        continue
                elif not re.search(pattern, code):
                    continue
                op_key = sanitize_id(f"{mod_key}.op_{op_type.replace(' ', '_')}")
                if op_key not in op_nodes:
                    op_nodes[op_key] = {
                        "_key": op_key, "type": "Operator", "name": op_type,
                        "operator_type": op_type, "repo": prefix, "parent_module": mod_key,
                    }
                    has_op_edges[op_key] = {
                        "_key": get_edge_key(mod_key, op_key, "HAS_OPERATOR"),
                        "_from": f"RTL_Module/{mod_key}", "_to": f"Operator/{op_key}",
                        "type": "HAS_OPERATOR", "repo": prefix,
                    }
                uses_op_edges.append({
                    "_key": get_edge_key(chunk_key, op_key, "USES_OPERATOR"),
                    "_from": f"RTL_LogicChunk/{chunk_key}", "_to": f"Operator/{op_key}",
                    "type": "USES_OPERATOR", "repo": prefix,
                })
    n = _upsert(db, "Operator", list(op_nodes.values()))
    e = _upsert(db, "HAS_OPERATOR", list(has_op_edges.values()), edge=True)
    e += _upsert(db, "USES_OPERATOR", uses_op_edges, edge=True)
    return n, e


# ---------------------------------------------------------------------------
# 3. Assigns — reuse AssignExtractor unchanged
# ---------------------------------------------------------------------------

def extract_assigns(db, prefix: str, modules: list[dict], resolver: _LiveNodeResolver) -> tuple[int, int]:
    all_nodes, all_edges = [], []
    for mod in modules:
        clean = _clean_body(mod["code"])
        # AssignExtractor.resolve_id() calls use mod['name'] (bare), matching
        # what its internal self.module_name is set to below.
        extractor = AssignExtractor(mod["name"], clean, mod["key"], resolver=resolver)
        extractor.extract()  # populates extractor.assigns / extractor.edges

        # Assign node _keys from the extractor are bare (module_name-based,
        # not repo-prefixed) — prefix with the full module key for uniqueness
        # across repos, and remember the mapping to rewrite edge endpoints.
        key_map = {}
        for n in extractor.assigns:
            bare_key = n["_key"]
            n["_key"] = f"{mod['key']}_{bare_key}"
            n["repo"] = prefix
            n["parent_module"] = mod["key"]
            key_map[bare_key] = n["_key"]

        for e in extractor.edges:
            e["repo"] = prefix
            from_, to_ = e.pop("from"), e.pop("to")
            if e["type"] == "HAS_ASSIGN":
                e["_from"] = f"RTL_Module/{mod['key']}"
                e["_to"] = f"RTL_Assign/{key_map.get(to_, to_)}"
            else:  # DRIVES / READS_FROM: from=assign_id, to=resolved signal/port id
                target_kind = resolver.kind_of(to_)
                if target_kind == "unknown":
                    # resolve_id() couldn't match a real port/signal/parameter
                    # (commonly a `define macro constant, which no resolver
                    # here tracks) -- skip rather than write a dangling edge.
                    continue
                e["_from"] = f"RTL_Assign/{key_map.get(from_, from_)}"
                e["_to"] = f"{target_kind}/{to_}"
            all_edges.append(e)
        all_nodes.extend(extractor.assigns)
    n = _upsert(db, "RTL_Assign", all_nodes)
    e = 0
    for etype in ("HAS_ASSIGN", "DRIVES", "READS_FROM"):
        e += _upsert(db, etype, [x for x in all_edges if x["type"] == etype], edge=True)
    return n, e


# ---------------------------------------------------------------------------
# 4. Memory arrays — new detection (no legacy precedent)
# ---------------------------------------------------------------------------

MEMORY_PORT_SUFFIXES = ['addr', 'dat_i', 'dat_o', 'di', 'doq', 'we', 'en', 'ce', 'sel', 'ack', 'cyc', 'stb']


def extract_memories(db, prefix: str, modules: list[dict], resolver: _LiveNodeResolver) -> tuple[int, int]:
    ports_signals = defaultdict(list)  # module_key -> [{id, name}] (RTL_Port + RTL_Signal)
    for r in db.aql.execute(
            "FOR p IN RTL_Port FILTER p.repo == @r RETURN {mod: p.parent_module, id: p._key, name: p.name}",
            bind_vars={"r": prefix}):
        ports_signals[f"{prefix}_{r['mod']}"].append(r)
    for r in db.aql.execute(
            "FOR s IN RTL_Signal FILTER s.repo == @r RETURN {mod: s.parent_module, id: s._key, name: s.name}",
            bind_vars={"r": prefix}):
        ports_signals[f"{prefix}_{r['mod']}"].append(r)

    mem_nodes, edges, port_nodes, access_edges = [], [], {}, []
    for mod in modules:
        mod_key = mod["key"]
        clean = _clean_body(mod["code"])
        always, assigns = _logic_chunk_ids(mod_key, mod["code"])
        chunks = list(always.values()) + list(assigns.values())

        mems_in_module = []
        for m in RE_MEMORY_DECL.finditer(clean):
            name, depth_expr = m.group(1), m.group(2).strip()
            mem_key = sanitize_id(f"{mod_key}.{name}")
            mems_in_module.append((name, mem_key))
            mem_nodes.append({
                "_key": mem_key, "type": "RTL_Memory", "name": name,
                "repo": prefix, "parent_module": mod_key, "depth_expr": depth_expr,
            })
            resolver.memory_ids.add(mem_key)
            edges.append({
                "_key": get_edge_key(mod_key, mem_key, "HAS_MEMORY"),
                "_from": f"RTL_Module/{mod_key}", "_to": f"RTL_Memory/{mem_key}",
                "type": "HAS_MEMORY", "repo": prefix,
            })

        for name, mem_key in mems_in_module:
            write_pat = re.compile(rf'\b{re.escape(name)}\s*\[(.*?)\]\s*(?:<=|=)')
            read_pat = re.compile(rf'=\s*.*?\b{re.escape(name)}\s*\[(.*?)\]')
            for code, chunk_id in chunks:
                chunk_key = sanitize_id(chunk_id)
                for idx_expr in write_pat.findall(code):
                    access_edges.append({
                        "_key": get_edge_key(chunk_key, mem_key, f"ACCESSES_WRITE_{idx_expr.strip()}", truncate=24),
                        "_from": f"RTL_LogicChunk/{chunk_key}", "_to": f"RTL_Memory/{mem_key}",
                        "type": "ACCESSES", "access_type": "write",
                        "index_expression": idx_expr.strip(), "repo": prefix,
                    })
                for idx_expr in read_pat.findall(code):
                    access_edges.append({
                        "_key": get_edge_key(chunk_key, mem_key, f"ACCESSES_READ_{idx_expr.strip()}", truncate=24),
                        "_from": f"RTL_LogicChunk/{chunk_key}", "_to": f"RTL_Memory/{mem_key}",
                        "type": "ACCESSES", "access_type": "read",
                        "index_expression": idx_expr.strip(), "repo": prefix,
                    })

            # MemoryPort: >=2 module ports/signals whose names look like a
            # memory interface (addr/dat_i/we/...) for this specific memory.
            matched = [ps for ps in ports_signals.get(mod_key, [])
                      if any(ps["name"].lower().endswith(f"_{s}") or ps["name"].lower() == s
                             for s in MEMORY_PORT_SUFFIXES)]
            if len(matched) >= 2:
                port_key = sanitize_id(f"{mod_key}.{name}_port")
                if port_key not in port_nodes:
                    port_nodes[port_key] = {
                        "_key": port_key, "type": "MemoryPort", "name": f"{name} Port",
                        "repo": prefix, "parent_module": mod_key, "memory": name,
                    }
                    access_edges.append({
                        "_key": get_edge_key(mem_key, port_key, "MEMORY_PORT"),
                        "_from": f"RTL_Memory/{mem_key}", "_to": f"MemoryPort/{port_key}",
                        "type": "MEMORY_PORT", "repo": prefix,
                    })
                for ps in matched:
                    coll = "RTL_Port" if ps["id"] in resolver.port_ids else "RTL_Signal"
                    access_edges.append({
                        "_key": get_edge_key(ps["id"], port_key, "PART_OF_PORT"),
                        "_from": f"{coll}/{ps['id']}", "_to": f"MemoryPort/{port_key}",
                        "type": "PART_OF_PORT", "repo": prefix,
                    })

    n = _upsert(db, "RTL_Memory", mem_nodes) + _upsert(db, "MemoryPort", list(port_nodes.values()))
    e = _upsert(db, "HAS_MEMORY", edges, edge=True)
    for etype in ("ACCESSES", "MEMORY_PORT", "PART_OF_PORT"):
        e += _upsert(db, etype, [x for x in access_edges if x["type"] == etype], edge=True)
    return n, e


# ---------------------------------------------------------------------------
# 5. Clock domains + CDC — clock signal from each always block's own
#    sensitivity list (no CLOCKED_BY precedent to depend on)
# ---------------------------------------------------------------------------

def extract_clock_domains(db, prefix: str, modules: list[dict], resolver: _LiveNodeResolver) -> tuple[int, int]:
    clock_nodes, has_always_edges, clocked_by_edges, crosses_edges = {}, [], [], []
    for mod in modules:
        mod_key = mod["key"]
        always, _ = _logic_chunk_ids(mod_key, mod["code"])
        # Per-module CDC tracking: which clock domain(s) drive vs. read each
        # signal name (LHS/RHS of assignments within THIS module's always
        # blocks). A signal driven in one domain and read in another crosses.
        driven_in, read_in = defaultdict(set), defaultdict(set)
        for _, (code, chunk_id) in always.items():
            chunk_key = sanitize_id(chunk_id)
            sens = RE_SENSITIVITY.search(code)
            if not sens:
                continue
            has_always_edges.append({
                "_key": get_edge_key(mod_key, chunk_key, "HAS_ALWAYS"),
                "_from": f"RTL_Module/{mod_key}", "_to": f"RTL_LogicChunk/{chunk_key}",
                "type": "HAS_ALWAYS", "repo": prefix,
            })
            edge_sigs = RE_EDGE_SIGNAL.findall(sens.group(1))
            for sig in edge_sigs:
                clk_key = sanitize_id(f"{prefix}.clock_{sig}")
                if clk_key not in clock_nodes:
                    clock_nodes[clk_key] = {
                        "_key": clk_key, "type": "ClockDomain", "name": sig, "repo": prefix,
                    }
                clocked_by_edges.append({
                    "_key": get_edge_key(chunk_key, clk_key, "CLOCKED_BY"),
                    "_from": f"RTL_LogicChunk/{chunk_key}", "_to": f"ClockDomain/{clk_key}",
                    "type": "CLOCKED_BY", "repo": prefix,
                })
            if not edge_sigs:
                continue
            lhs, rhs = extract_signals_from_code(code)
            for sig_name in lhs:
                driven_in[sig_name].update(edge_sigs)
            for sig_name in rhs:
                read_in[sig_name].update(edge_sigs)

        for sig_name, drive_domains in driven_in.items():
            cross_domains = read_in.get(sig_name, set()) - drive_domains
            if not cross_domains:
                continue
            resolved = resolver.resolve_id(mod_key, sig_name)
            kind = resolver.kind_of(resolved)
            if kind == "unknown":
                continue  # not a real port/signal (e.g. a macro/literal) -- skip
            for read_clk in cross_domains:
                clk_key = sanitize_id(f"{prefix}.clock_{read_clk}")
                if clk_key not in clock_nodes:
                    continue
                crosses_edges.append({
                    "_key": get_edge_key(resolved, clk_key, "CROSSES_DOMAIN"),
                    "_from": f"{kind}/{resolved}", "_to": f"ClockDomain/{clk_key}",
                    "type": "CROSSES_DOMAIN", "repo": prefix,
                    "driven_by": sorted(drive_domains),
                })
    n = _upsert(db, "ClockDomain", list(clock_nodes.values()))
    e = _upsert(db, "HAS_ALWAYS", has_always_edges, edge=True)
    e += _upsert(db, "CLOCKED_BY", clocked_by_edges, edge=True)
    e += _upsert(db, "CROSSES_DOMAIN", crosses_edges, edge=True)
    return n, e


# ---------------------------------------------------------------------------
# 6. FSMs — reuse FSMExtractor unchanged
# ---------------------------------------------------------------------------

def extract_fsms(db, prefix: str, modules: list[dict], resolver: _LiveNodeResolver) -> tuple[int, int]:
    all_fsm_docs, all_state_docs, all_edges = [], [], []
    for mod in modules:
        clean = _clean_body(mod["code"])
        extractor = FSMExtractor(mod["name"], clean, mod["key"],
                                 file_content=mod.get("full_file", mod["code"]), resolver=resolver)
        try:
            extractor.extract()
        except Exception as exc:
            print(f"  [deep-analysis] FSM extraction failed for {mod['key']}: {exc}")
            continue
        if not extractor.fsms:
            continue

        # fsm_id/state_id from the extractor are bare (module_name-based, not
        # repo-prefixed) -- prefix for cross-repo uniqueness and rewrite every
        # edge endpoint through the resulting key map.
        key_map = {}
        for fsm in extractor.fsms:
            bare = fsm["_key"]
            fsm["_key"] = f"{mod['key']}_{bare}"
            fsm["repo"] = prefix
            key_map[bare] = fsm["_key"]
        for st in extractor.states:
            bare = st["_key"]
            st["_key"] = f"{mod['key']}_{bare}"
            st["repo"] = prefix
            key_map[bare] = st["_key"]

        always, _ = _logic_chunk_ids(mod["key"], mod["code"])
        for e in extractor.edges:
            e["repo"] = prefix
            from_, to_ = e.pop("from"), e.pop("to")
            etype = e["type"]
            if etype == "HAS_FSM":
                e["_from"], e["_to"] = f"RTL_Module/{mod['key']}", f"FSM_StateMachine/{key_map.get(to_, to_)}"
            elif etype == "HAS_STATE":
                e["_from"] = f"FSM_StateMachine/{key_map.get(from_, from_)}"
                e["_to"] = f"FSM_State/{key_map.get(to_, to_)}"
            elif etype == "TRANSITIONS_TO":
                e["_from"] = f"FSM_State/{key_map.get(from_, from_)}"
                e["_to"] = f"FSM_State/{key_map.get(to_, to_)}"
            elif etype == "STATE_REGISTER":
                e["_from"] = f"FSM_StateMachine/{key_map.get(from_, from_)}"
                target_kind = resolver.kind_of(to_)
                e["_to"] = f"{target_kind if target_kind != 'unknown' else 'RTL_Signal'}/{to_}"
            all_edges.append(e)

        # IMPLEMENTED_BY: the extractor's own attempt always no-ops (its
        # local-file lookup is disabled — see _LiveNodeResolver docstring).
        # Re-derive the same "does this chunk assign to the state register"
        # heuristic using logic chunks already fetched from ArangoDB.
        for fsm in extractor.fsms:
            state_reg = fsm.get("state_register")
            if not state_reg:
                continue
            assign_pat = re.compile(rf'\b{re.escape(state_reg)}\b\s*[<]?=')
            for _, (code, chunk_id) in always.items():
                if assign_pat.search(code):
                    chunk_key = sanitize_id(chunk_id)
                    all_edges.append({
                        "_key": get_edge_key(fsm["_key"], chunk_key, "IMPLEMENTED_BY"),
                        "_from": f"FSM_StateMachine/{fsm['_key']}", "_to": f"RTL_LogicChunk/{chunk_key}",
                        "type": "IMPLEMENTED_BY", "repo": prefix,
                    })

        all_fsm_docs.extend(extractor.fsms)
        all_state_docs.extend(extractor.states)

    n = _upsert(db, "FSM_StateMachine", all_fsm_docs) + _upsert(db, "FSM_State", all_state_docs)
    e = 0
    for etype in ("HAS_FSM", "HAS_STATE", "TRANSITIONS_TO", "STATE_REGISTER", "IMPLEMENTED_BY"):
        e += _upsert(db, etype, [x for x in all_edges if x.get("type") == etype], edge=True)
    return n, e


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

ANALYSES = {
    "bus": lambda db, prefix, modules, resolver: extract_bus_interfaces(db, prefix),
    "operators": lambda db, prefix, modules, resolver: extract_operators(db, prefix, modules),
    "assigns": lambda db, prefix, modules, resolver: extract_assigns(db, prefix, modules, resolver),
    "memory": lambda db, prefix, modules, resolver: extract_memories(db, prefix, modules, resolver),
    "clocks": lambda db, prefix, modules, resolver: extract_clock_domains(db, prefix, modules, resolver),
    "fsm": lambda db, prefix, modules, resolver: extract_fsms(db, prefix, modules, resolver),
}
ORDER = ["bus", "operators", "assigns", "memory", "clocks", "fsm"]


def main():
    parser = argparse.ArgumentParser(description="REQ-003: deep RTL analysis (FSM/clocks/bus/memory/operators/assigns)")
    parser.add_argument("--repo", help="Repo prefix, e.g. OR1200")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--only", help="Comma-separated subset of: " + ",".join(ORDER))
    args = parser.parse_args()
    if not args.repo and not args.all:
        parser.print_help()
        return

    db = get_temporal_db()
    prefixes = REPO_PREFIXES if args.all else [args.repo]
    analyses = args.only.split(",") if args.only else ORDER

    for prefix in prefixes:
        print(f"\n{'='*60}\n{prefix}\n{'='*60}")
        modules = _fetch_modules(db, prefix)
        print(f"  {len(modules)} modules with code_content")
        resolver = _LiveNodeResolver(db, prefix)
        for name in analyses:
            n, e = ANALYSES[name](db, prefix, modules, resolver)
            print(f"  [{name}] {n} nodes, {e} edges")


if __name__ == "__main__":
    main()
