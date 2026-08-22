"""
build_snapshot.py — assemble an offline snapshot for ChronoGraph.

Reads the exported IC knowledge-graph data (repo root ``data/``) and produces a
single ``snapshot.json`` the SnapshotSource can slice in memory. Where derived
layers (cross-repo lineage, consolidated golden entities) only exist in the live
DB, we reconstruct an honest approximation here and tag it ``derived: true`` so
the UI can label it.

Run:
    python -m ic_viz.build_snapshot            # writes viz/snapshot/snapshot.json
    python -m ic_viz.build_snapshot --out X    # custom path
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict, Counter

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
VIZ_DIR = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(VIZ_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
TEMPORAL_DIR = os.path.join(DATA_DIR, "temporal")
DEFAULT_OUT = os.path.join(VIZ_DIR, "snapshot", "snapshot.json")

OPEN_TS = 9999999999

# Repo → display metadata. Colors chosen to read in both light/dark.
REPOS = {
    "or1200":     {"short": "or1200",  "canonical": "openrisc/or1200",         "color": "#4f8cff", "lineage_parent": None},
    "mor1kx":     {"short": "mor1kx",  "canonical": "openrisc/mor1kx",         "color": "#22c1a4", "lineage_parent": "or1200"},
    "marocchino": {"short": "marocc.", "canonical": "openrisc/or1k_marocchino","color": "#e0724a", "lineage_parent": "mor1kx"},
    "ibex":       {"short": "ibex",    "canonical": "lowRISC/ibex",            "color": "#b47cff", "lineage_parent": None},
}

# Prefix tokens stripped when deriving a module's functional concept (for cross-repo matching).
_FAMILY_PREFIXES = {"or1200", "mor1kx", "or1k", "marocchino", "pfpu", "ibex", "prim"}
# Synonym normalization so equivalent functions across repos map to one concept.
_FUNC_SYNONYMS = {
    "dc": "dcache", "ic": "icache", "dctop": "dcache", "ictop": "icache",
    "tt": "ticktimer", "du": "debug", "rf": "regfile", "regfile": "regfile",
    "immutop": "immu", "dmmutop": "dmmu", "cpu": "cpu", "top": "top",
    "lsu": "lsu", "ctrl": "control", "control": "control", "fetch": "fetch",
    "decode": "decode", "id": "decode", "if": "fetch", "sprs": "spr", "spr": "spr",
    "pic": "pic", "alu": "alu", "mul": "multiplier", "div": "divider",
    "fpu": "fpu", "except": "exception", "exception": "exception",
}


def _load(path):
    with open(path) as f:
        return json.load(f)


def _load_jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _meta(d):
    """metadata may be a dict or a repr-string (some exports stringified it)."""
    m = d.get("metadata")
    if isinstance(m, dict):
        return m
    if isinstance(m, str):
        try:
            import ast
            return ast.literal_eval(m)
        except Exception:
            return {"text": m}
    return {}


def _functional_concept(label: str) -> str:
    """Reduce an RTL module name to a cross-repo functional concept key."""
    s = label.lower().replace("-", "_")
    toks = [t for t in s.split("_") if t]
    core = [t for t in toks if t not in _FAMILY_PREFIXES]
    core = core or toks
    # collapse to a canonical function using synonyms, keep the most specific token
    mapped = [_FUNC_SYNONYMS.get(t, t) for t in core]
    # heuristics: prefer a recognisable function token if present
    known = [t for t in mapped if t in set(_FUNC_SYNONYMS.values())]
    if known:
        return known[0]
    return "_".join(mapped)


# --------------------------------------------------------------------------
# Temporal spine (all repos)
# --------------------------------------------------------------------------
def build_temporal():
    epochs, commits, modules = [], [], []
    belongs = []          # (module_uid, epoch_uid)
    modified = []         # (commit_uid, module_uid)
    repo_bounds = {}

    for repo, info in REPOS.items():
        npath = os.path.join(TEMPORAL_DIR, f"{repo}_temporal_nodes.jsonl")
        epath = os.path.join(TEMPORAL_DIR, f"{repo}_temporal_edges.jsonl")
        if not os.path.exists(npath):
            continue
        nodes = _load_jsonl(npath)
        edges = _load_jsonl(epath)

        # local key -> uid map
        def uid(k):
            return f"{repo}__{k}"

        ts_vals = []
        for n in nodes:
            t = n["type"]
            k = n["_key"]
            if t == "DesignEpoch":
                start = n.get("start_ts")
                end = n.get("end_ts")
                epochs.append({
                    "id": uid(k), "repo": repo, "epoch_type": n.get("epoch_type", "other"),
                    "label": n.get("label", k), "start_ts": start, "end_ts": end,
                    "start_commit": n.get("start_commit"), "end_commit": n.get("end_commit"),
                    "git_tag": n.get("git_tag"),
                })
                if start:
                    ts_vals.append(start)
                if end:
                    ts_vals.append(end)
            elif t == "GitCommit":
                m = n.get("metadata", {}) or {}
                ts = n.get("valid_from_ts") or m.get("timestamp")
                commits.append({
                    "id": uid(k), "repo": repo, "ts": ts,
                    "author": m.get("author"), "message": (m.get("message") or "")[:280],
                    "epoch": n.get("design_epoch"),
                })
                if ts:
                    ts_vals.append(ts)
            elif t == "RTL_Module":
                vf = n.get("valid_from_ts")
                vt = n.get("valid_to_ts") or OPEN_TS
                modules.append({
                    "id": uid(k), "repo": repo, "label": n.get("label", k),
                    "file": n.get("file"), "file_hash": n.get("file_hash"),
                    "epoch": n.get("design_epoch"),
                    "valid_from_ts": vf, "valid_to_ts": vt,
                    "valid_from_commit": n.get("valid_from_commit"),
                    "valid_to_commit": n.get("valid_to_commit"),
                    "concept": _functional_concept(n.get("label", k)),
                })
                if vf:
                    ts_vals.append(vf)

        for e in edges:
            et = e.get("type")
            if et == "BELONGS_TO_EPOCH":
                belongs.append({"module": uid(e["from"]), "epoch": uid(e["to"]), "role": e.get("role")})
            elif et == "MODIFIED":
                modified.append({"commit": uid(e["from"]), "module": uid(e["to"]),
                                 "file": (e.get("metadata", {}) or {}).get("file_path")})

        if ts_vals:
            repo_bounds[repo] = {"ts_min": min(ts_vals), "ts_max": max(t for t in ts_vals if t < OPEN_TS)}

    return {
        "epochs": epochs, "commits": commits, "modules": modules,
        "belongs_to_epoch": belongs, "modified": modified, "repo_bounds": repo_bounds,
    }


# --------------------------------------------------------------------------
# Structural detail + text provenance (or1200 @ HEAD)
# --------------------------------------------------------------------------
def build_structural():
    def load(name):
        p = os.path.join(DATA_DIR, f"{name}.json")
        return _load(p) if os.path.exists(p) else []

    modules = {}
    for m in load("import_RTL_Module"):
        md = _meta(m)
        modules[m["_key"]] = {
            "label": m.get("label", m["_key"]),
            "file": md.get("file"),
            "code": md.get("code_content", ""),
        }

    def simple_nodes(name, keep=("direction", "datatype", "expanded_name", "description", "width")):
        out = {}
        for d in load(name):
            md = _meta(d)
            out[d["_key"]] = {"label": d.get("label", d["_key"]),
                              **{k: md.get(k) for k in keep if md.get(k) is not None}}
        return out

    ports = simple_nodes("import_RTL_Port")
    signals = simple_nodes("import_RTL_Signal")

    fsms = {}
    for d in load("import_FSM_StateMachine"):
        fsms[d["_key"]] = {"label": d.get("name", d["_key"]), "state_count": d.get("state_count"),
                           "parent_module": d.get("parent_module")}
    fsm_states = {d["_key"]: {"label": d.get("label", d["_key"])} for d in load("import_FSM_State")}

    def edges(name):
        out = []
        for e in load(name):
            out.append({"from": e["_from"], "to": e["_to"], "type": e.get("type"),
                        **({"instance_names": e.get("instance_names")} if e.get("instance_names") else {})})
        return out

    has_port = edges("import_HAS_PORT")
    has_signal = edges("import_HAS_SIGNAL")
    has_fsm = edges("import_HAS_FSM")
    has_state = edges("import_HAS_STATE")
    depends_on = edges("import_DEPENDS_ON")
    contains = edges("import_CONTAINS")

    # Doc chunks + text provenance
    chunks = {}
    for c in load("import_DocChunk"):
        md = _meta(c)
        chunks[c["_key"]] = {"doc_title": c.get("label", ""), "text": md.get("text", "")}

    documented_by = []
    for e in load("import_DOCUMENTED_BY"):
        md = _meta(e)
        documented_by.append({
            "module_id": e["_from"].split("/")[-1],
            "chunk_id": e["_to"].split("/")[-1],
            "matched_term": md.get("matched_term"),
            "score": md.get("score"),
        })

    return {
        "modules": modules, "ports": ports, "signals": signals,
        "fsms": fsms, "fsm_states": fsm_states,
        "has_port": has_port, "has_signal": has_signal, "has_fsm": has_fsm,
        "has_state": has_state, "depends_on": depends_on, "contains": contains,
        "chunks": chunks, "documented_by": documented_by,
    }


# --------------------------------------------------------------------------
# Derived: cross-repo lineage / similarity
# --------------------------------------------------------------------------
def build_cross_repo(temporal):
    """Link the latest version of each module across repos by functional concept.

    EVOLVED_FROM follows the known OpenRISC lineage (child -> parent) where a
    shared functional concept exists. SIMILAR_TO links concept matches across
    any repo pair not already covered by lineage.  DERIVED offline heuristic;
    live mode uses the DB's embedding-based CROSS_REPO_* edges.
    """
    # latest (open-ended, else max valid_from) module per (repo, label)
    latest = {}
    for m in temporal["modules"]:
        key = (m["repo"], m["label"])
        cur = latest.get(key)
        if cur is None or (m["valid_to_ts"] >= cur["valid_to_ts"] and m["valid_from_ts"] >= cur["valid_from_ts"]):
            latest[key] = m
    mods = list(latest.values())

    # index by (repo, concept)
    by_repo_concept = defaultdict(list)
    for m in mods:
        by_repo_concept[(m["repo"], m["concept"])].append(m)

    edges = []
    seen = set()

    def add(a, b, etype, **extra):
        key = (a["id"], b["id"], etype)
        if key in seen or a["id"] == b["id"]:
            return
        seen.add(key)
        edges.append({"from": a["id"], "to": b["id"], "type": etype,
                      "from_repo": a["repo"], "to_repo": b["repo"],
                      "concept": a["concept"], "derived": True, **extra})

    # lineage chain
    for repo, info in REPOS.items():
        parent = info.get("lineage_parent")
        if not parent:
            continue
        for m in mods:
            if m["repo"] != repo:
                continue
            for pm in by_repo_concept.get((parent, m["concept"]), []):
                add(m, pm, "CROSS_REPO_EVOLVED_FROM", confidence=0.85, lineage="architectural_successor")

    # similarity across every other repo pair sharing a concept
    repos = list(REPOS.keys())
    for i, ra in enumerate(repos):
        for rb in repos[i + 1:]:
            # skip direct lineage pairs (already EVOLVED)
            lineage_pair = (REPOS[ra].get("lineage_parent") == rb) or (REPOS[rb].get("lineage_parent") == ra)
            concepts = {m["concept"] for m in mods if m["repo"] == ra} & \
                       {m["concept"] for m in mods if m["repo"] == rb}
            for c in concepts:
                if c in ("", "defines", "top"):
                    continue
                a = by_repo_concept[(ra, c)][0]
                b = by_repo_concept[(rb, c)][0]
                if lineage_pair:
                    continue
                add(a, b, "CROSS_REPO_SIMILAR_TO", similarity_score=0.8, similarity_type="concept")
    return edges


# --------------------------------------------------------------------------
# Derived: consolidated golden layer (or1200)
# --------------------------------------------------------------------------
def build_consolidated(structural):
    """Each RTL module @HEAD becomes a consolidated entity with direct links to
    the chunks it was found in (text evidence) and its verilog (code evidence).
    Relations = module dependencies carrying consolidated cross-source evidence.
    DERIVED; live mode uses Golden_Entities / RESOLVED_TO / CONSOLIDATES.
    """
    docs_by_mod = defaultdict(list)
    for d in structural["documented_by"]:
        docs_by_mod[d["module_id"]].append(d)

    ports_by_mod = defaultdict(int)
    for e in structural["has_port"]:
        ports_by_mod[e["from"].split("/")[-1]] += 1
    signals_by_mod = defaultdict(int)
    for e in structural["has_signal"]:
        signals_by_mod[e["from"].split("/")[-1]] += 1

    entities = []
    for key, m in structural["modules"].items():
        chunk_ids = [d["chunk_id"] for d in docs_by_mod.get(key, [])]
        entities.append({
            "id": f"or1200__{key}", "module_key": key, "label": m["label"],
            "entity_type": "processor_component", "file": m["file"],
            "concept": _functional_concept(m["label"]),
            "chunk_ids": sorted(set(chunk_ids)),
            "matched_terms": sorted({d["matched_term"] for d in docs_by_mod.get(key, []) if d.get("matched_term")}),
            "port_count": ports_by_mod.get(key, 0),
            "signal_count": signals_by_mod.get(key, 0),
            "derived": True,
        })

    # relations from module dependencies, enriched with shared-chunk text evidence
    chunks_of = {e["module_key"]: set(e["chunk_ids"]) for e in entities}
    relations = []
    for e in structural["depends_on"]:
        fk = e["from"].split("/")[-1]
        tk = e["to"].split("/")[-1]
        shared = sorted(chunks_of.get(fk, set()) & chunks_of.get(tk, set()))
        relations.append({
            "from": f"or1200__{fk}", "to": f"or1200__{tk}", "type": "DEPENDS_ON",
            "verilog_evidence": True,
            "instance_names": e.get("instance_names"),
            "text_evidence": shared,
            "derived": True,
        })
    return {"entities": entities, "relations": relations}


# --------------------------------------------------------------------------
# Assemble
# --------------------------------------------------------------------------
def build(out_path=DEFAULT_OUT):
    temporal = build_temporal()
    structural = build_structural()
    cross_repo = build_cross_repo(temporal)
    consolidated = build_consolidated(structural)

    all_ts = [c["ts"] for c in temporal["commits"] if c.get("ts")] + \
             [m["valid_from_ts"] for m in temporal["modules"] if m.get("valid_from_ts")]
    ts_min, ts_max = (min(all_ts), max(all_ts)) if all_ts else (0, 0)

    repo_stats = []
    ep_by_repo = Counter(e["repo"] for e in temporal["epochs"])
    cm_by_repo = Counter(c["repo"] for c in temporal["commits"])
    md_by_repo = Counter()
    for m in temporal["modules"]:
        md_by_repo[m["repo"]] += 1
    for repo, info in REPOS.items():
        b = temporal["repo_bounds"].get(repo, {})
        repo_stats.append({
            "name": repo, "short": info["short"], "canonical": info["canonical"],
            "color": info["color"], "lineage_parent": info["lineage_parent"],
            "epoch_count": ep_by_repo.get(repo, 0),
            "commit_count": cm_by_repo.get(repo, 0),
            "module_version_count": md_by_repo.get(repo, 0),
            "ts_min": b.get("ts_min"), "ts_max": b.get("ts_max"),
            "has_structural": repo == "or1200",
        })

    snapshot = {
        "meta": {
            "source": "snapshot",
            "ts_min": ts_min, "ts_max": ts_max,
            "open_ts": OPEN_TS,
            "epoch_types": ["initial_commit", "development", "major_refactor", "milestone_tag", "other"],
        },
        "repos": repo_stats,
        "temporal": temporal,
        "structural": structural,
        "cross_repo": cross_repo,
        "consolidated": consolidated,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snapshot, f)

    size_mb = os.path.getsize(out_path) / 1e6
    print(f"[snapshot] wrote {out_path}  ({size_mb:.1f} MB)")
    print(f"[snapshot] repos={len(repo_stats)} epochs={len(temporal['epochs'])} "
          f"commits={len(temporal['commits'])} module_versions={len(temporal['modules'])}")
    print(f"[snapshot] cross_repo_edges={len(cross_repo)} "
          f"consolidated_entities={len(consolidated['entities'])} "
          f"consolidated_relations={len(consolidated['relations'])}")
    print(f"[snapshot] structural: modules={len(structural['modules'])} "
          f"ports={len(structural['ports'])} signals={len(structural['signals'])} "
          f"chunks={len(structural['chunks'])} documented_by={len(structural['documented_by'])}")
    ts_span = ""
    if ts_min:
        import datetime
        ts_span = f"{datetime.date.fromtimestamp(ts_min)} .. {datetime.date.fromtimestamp(ts_max)}"
    print(f"[snapshot] time span: {ts_span}")
    return snapshot


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    build(args.out)
