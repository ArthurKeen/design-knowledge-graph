"""
src/rtl_semantic_bridge.py — Build RESOLVED_TO edges linking RTL nodes to Golden Entities.

Connects RTL_Port and RTL_Signal nodes to per-repo Golden Entities using:
  Stage 1 — Exact/normalised name match (score = 1.0)
  Stage 2 — Embedding cosine similarity (score = cosine ≥ min_score)

The expanded_name field (e.g. "lsu_stall" → "Load Store Unit stall") is used
as the primary matching text; the raw name is the fallback.

Usage:
    python src/rtl_semantic_bridge.py --all               # all registered repos
    python src/rtl_semantic_bridge.py --repo OR1200_      # single repo
    python src/rtl_semantic_bridge.py --all --dry-run     # inspect candidates only
    python src/rtl_semantic_bridge.py --all --truncate    # rebuild from scratch
    python src/rtl_semantic_bridge.py --all --min-score 0.75
"""

import os
import sys
import re
import hashlib
import argparse
import json

_src = os.path.dirname(os.path.abspath(__file__))
if _src not in sys.path:
    sys.path.insert(0, _src)

from dotenv import load_dotenv
load_dotenv()

from config_temporal import ARANGO_DATABASE, ARANGO_ENDPOINT, REPO_REGISTRY, load_repo_registry
from db_utils import get_temporal_db, ensure_collection
from utils import cosine_similarity

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESOLVED_TO_EDGE_COL  = "RESOLVED_TO"

# Minimum cosine similarity for embedding-based matches
DEFAULT_MIN_SCORE = float(os.getenv("RESOLVED_TO_MIN_SCORE", "0.72"))

# Golden entity types worth matching RTL names against.
# Excludes pure-doc concepts (EXCEPTION_TYPE, INSTRUCTION, BUS_PROTOCOL, etc.)
RTL_RELEVANT_TYPES = {
    "SIGNAL",
    "PROCESSOR_COMPONENT",
    "HARDWARE_INTERFACE",
    "REGISTER",
    "MEMORY_UNIT",
    "CLOCK_DOMAIN",
    "STATE_MACHINE",
    "TIMING_CONSTRAINT",
    "ARCHITECTURE_FEATURE",
    "EXECUTION_UNIT",
    "PIPELINE_STAGE",
}

# RTL attribute types that are infrastructure noise — skip for embedding match.
# 2-char abbreviations common in digital design (data-in, data-out, write-enable,
# output-enable, chip-select, etc.) are excluded because they are too ambiguous
# to resolve reliably — e.g. "di" (data input on SRAM) was falsely matching the
# "Development Interface" golden entity via its auto-generated DI acronym alias.
SKIP_NAMES = {
    "clk", "rst", "rst_n", "reset", "vcc", "gnd", "clk_i", "clk_o", "rst_i",
    # single-char signals
    "a", "b", "c", "d", "e", "q",
    # common 2-char digital-design abbreviations that are too ambiguous to resolve
    "di", "do", "we", "oe", "cs", "ce", "qi", "qo",
    # generic pin direction / qualifier names
    "en", "sel", "out", "in",
}

# Generic hardware vocabulary that appears in many golden-entity names/descriptions.
# Allowing these into the word-index creates too many false positive exact matches
# (e.g. RTL signal "cnt" expanded to "counter" matching golden "Program Counter"
# solely because the word "counter" appears in its alias).
# These words are blocked from being used as word-index keys in match_exact().
WORD_INDEX_STOPWORDS = {
    "output", "input", "counter", "register", "memory", "address", "control",
    "status", "interface", "unit", "data", "program", "signal", "clock",
    "reset", "enable", "logic", "mode", "type", "state", "stage", "block",
    "module", "system", "core", "port", "write", "read",
}

_ALIAS_OVERRIDES_CACHE: dict[str, dict[str, list[str]]] = {}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    return get_temporal_db()


def _ensure_edge_col(db, name: str) -> None:
    ensure_collection(db, name, edge=True)


def _ensure_vertex_centric_indexes(db, col_name: str) -> None:
    col = db.collection(col_name)
    existing = {frozenset(idx["fields"]) for idx in col.indexes()}
    for fields in (["_from", "toNodeType"], ["_to", "fromNodeType"]):
        if frozenset(fields) not in existing:
            col.add_index({"type": "persistent", "fields": fields, "sparse": False})


_cosine = cosine_similarity


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    t = text.lower().replace("_", " ").replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> set[str]:
    """Token set for lexical gating and alias matching."""
    return {t for t in _normalise(text).split() if len(t) >= 3}


def _acronym(text: str) -> str:
    """Initialism-like acronym from normalized tokens."""
    toks = _normalise(text).split()
    if not toks:
        return ""
    return "".join(t[0] for t in toks if t and t[0].isalnum()).upper()


def _load_alias_overrides(prefix: str) -> dict[str, list[str]]:
    """
    Load per-repo golden alias overrides from src/rtl_semantic_aliases.json.
    JSON shape:
      { "MAROCCHINO_": { "Golden Name": ["alias1", "alias2"] }, ... }
    """
    if prefix in _ALIAS_OVERRIDES_CACHE:
        return _ALIAS_OVERRIDES_CACHE[prefix]

    path = os.path.join(os.path.dirname(__file__), "rtl_semantic_aliases.json")
    if not os.path.exists(path):
        _ALIAS_OVERRIDES_CACHE[prefix] = {}
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        repo_map = data.get(prefix, {}) if isinstance(data, dict) else {}
        if not isinstance(repo_map, dict):
            repo_map = {}
        # Ensure list[str] shape
        cleaned: dict[str, list[str]] = {}
        for k, v in repo_map.items():
            if isinstance(v, list):
                cleaned[k] = [str(x) for x in v if str(x).strip()]
        _ALIAS_OVERRIDES_CACHE[prefix] = cleaned
        return cleaned
    except Exception:
        _ALIAS_OVERRIDES_CACHE[prefix] = {}
        return {}


def _best_text(node: dict) -> str:
    """Choose the richest text representation of an RTL node for embedding."""
    expanded = (node.get("expanded_name") or "").strip()
    name     = (node.get("name") or "").strip()
    module   = (node.get("parent_module") or "").strip()
    desc     = (node.get("description") or "").strip()

    # Use expanded name if we have it; otherwise normalise the raw name
    primary = expanded if expanded else _normalise(name).replace(" ", "_")
    # Provide context — direction for ports, datatype for signals
    direction = node.get("direction", "")
    datatype  = node.get("datatype", "")

    parts = []
    if direction:
        parts.append(f"{direction} signal")
    elif datatype:
        parts.append(datatype)
    parts.append(primary)
    if desc and len(desc) > 3:
        parts.append(f"— {desc}")
    if module:
        parts.append(f"in {_normalise(module)}")
    return " ".join(parts)[:256]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_golden_entities(db, prefix: str) -> list[dict]:
    """
    Fetch golden entities for one repo, filtered to RTL-relevant types only.
    Returns list of dicts with: _key, name, type, description, embedding.
    """
    col = f"{prefix}Golden_Entities"
    if not db.has_collection(col):
        print(f"  [bridge] Collection not found: {col} — skipping")
        return []

    types_aql = "[" + ",".join(f'"{t}"' for t in RTL_RELEVANT_TYPES) + "]"
    # col is derived from prefix, which is validated as alphanumeric/underscore in main()
    results = list(db.aql.execute(f"""
        FOR g IN {col}
          FILTER g.type IN {types_aql}
          FILTER g.embedding != null
          RETURN {{
            _key:        g._key,
            name:        g.name,
            type:        g.type,
            description: g.description,
            embedding:   g.embedding,
            aliases:     g.aliases
          }}
    """))

    # Merge curated alias overrides and add acronym aliases for multi-word names.
    # This improves high-precision exact matching before embedding stage.
    alias_overrides = _load_alias_overrides(prefix)
    for g in results:
        merged = list(g.get("aliases") or [])
        merged += alias_overrides.get(g.get("name", ""), [])
        # Multi-word names get acronym alias (e.g. "Wishbone Clock" -> "WBC")
        ac = _acronym(g.get("name", ""))
        if ac and len(g.get("name", "").split()) >= 2:
            merged.append(ac)
        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for a in merged:
            if not a:
                continue
            k = _normalise(a)
            if not k or k in seen:
                continue
            seen.add(k)
            deduped.append(a)
        g["aliases"] = deduped

    print(f"  [bridge] {col}: {len(results)} eligible golden entities")
    return results


def load_rtl_nodes(db, repo: str) -> tuple[list[dict], list[dict]]:
    """
    Fetch RTL_Port and RTL_Signal nodes for one repo.
    Returns (ports, signals).
    """
    ports = list(db.aql.execute("""
        FOR p IN RTL_Port
          FILTER p.repo == @repo
          RETURN {
            _key: p._key, name: p.name, parent_module: p.parent_module,
            direction: p.direction, expanded_name: p.expanded_name,
            description: p.description, node_type: "port"
          }
    """, bind_vars={"repo": repo}))

    signals = list(db.aql.execute("""
        FOR s IN RTL_Signal
          FILTER s.repo == @repo
          RETURN {
            _key: s._key, name: s.name, parent_module: s.parent_module,
            datatype: s.datatype, expanded_name: s.expanded_name,
            description: s.description, node_type: "signal"
          }
    """, bind_vars={"repo": repo}))

    print(f"  [bridge] RTL nodes for {repo}: {len(ports)} ports, {len(signals)} signals")
    return ports, signals


# ---------------------------------------------------------------------------
# Stage 1 — Exact name matching
# ---------------------------------------------------------------------------

def match_exact(rtl_nodes: list[dict], golden_entities: list[dict]) -> list[dict]:
    """
    Stage 1: match by normalised name.
    Returns list of match dicts: {rtl, golden, score, method}.
    """
    # Build lookup: normalized golden names/aliases → golden entity
    lookup: dict[str, dict] = {}
    for g in golden_entities:
        name_keys = [g.get("name", "")]
        name_keys += list(g.get("aliases") or [])
        for raw in name_keys:
            norm = _normalise(raw)
            if norm and norm not in lookup:
                lookup[norm] = g
            # Also index individual meaningful words (≥ 4 chars, not generic stopwords)
            for word in norm.split():
                if len(word) >= 4 and word not in lookup and word not in WORD_INDEX_STOPWORDS:
                    lookup[word] = g

    matches = []
    for node in rtl_nodes:
        name = node.get("name", "")
        if name.lower() in SKIP_NAMES:
            continue
        expanded = (node.get("expanded_name") or "").strip()

        candidates = []
        if expanded:
            candidates.append(_normalise(expanded))
        candidates.append(_normalise(name))

        for cand in candidates:
            if cand and cand in lookup:
                matches.append({
                    "rtl":    node,
                    "golden": lookup[cand],
                    "score":  1.0,
                    "method": "exact",
                })
                break

    print(f"  [bridge] Stage 1 (exact): {len(matches)} matches")
    return matches


def _embedding_gate(node: dict, golden: dict, score: float, second_best: float, min_score: float) -> bool:
    """
    Precision-first acceptance gate for embedding candidates.
    Keep matches that have lexical support, or exceptionally strong semantic lead.
    """
    rtl_text = node.get("expanded_name") or node.get("name") or ""
    rtl_tokens = _tokens(rtl_text)
    golden_texts = [golden.get("name", "")]
    golden_texts += list(golden.get("aliases") or [])
    golden_tokens = set()
    for t in golden_texts:
        golden_tokens |= _tokens(t)

    overlap = len(rtl_tokens & golden_tokens)
    margin = score - second_best

    rtl_has_wishbone = "wishbone" in rtl_tokens or "wb" in rtl_tokens
    golden_has_wishbone = "wishbone" in golden_tokens or "wb" in golden_tokens
    rtl_has_clk_rst = bool({"clock", "clk", "reset", "rst"} & rtl_tokens)
    golden_type = golden.get("type", "")

    # Reject likely context drift unless confidence is unusually high.
    if rtl_has_wishbone and not golden_has_wishbone and score < max(min_score + 0.05, 0.78):
        return False
    if rtl_has_clk_rst and golden_type not in {"SIGNAL", "CLOCK_DOMAIN", "HARDWARE_INTERFACE"} and score < max(min_score + 0.05, 0.78):
        return False

    # Prefer lexical support; otherwise require a strong semantic lead.
    if overlap >= 1:
        return True
    if score >= max(min_score + 0.08, 0.80) and margin >= 0.05:
        return True
    return False


# ---------------------------------------------------------------------------
# Stage 2 — Embedding cosine similarity
# ---------------------------------------------------------------------------

def match_embedding(
    rtl_nodes: list[dict],
    golden_entities: list[dict],
    already_matched_keys: set[str],
    min_score: float = DEFAULT_MIN_SCORE,
    backend: str = "sentence_transformers",
) -> list[dict]:
    """
    Stage 2: embed unmatched RTL nodes and compare against golden embeddings.
    Returns list of match dicts: {rtl, golden, score, method}.
    """
    unmatched = [
        n for n in rtl_nodes
        if n["_key"] not in already_matched_keys
        and n.get("name", "").lower() not in SKIP_NAMES
        and len(n.get("name", "")) > 1  # skip degenerate 1-char port names
    ]
    if not unmatched:
        print("  [bridge] Stage 2 (embedding): no unmatched nodes — skipping")
        return []

    print(f"  [bridge] Stage 2 (embedding): embedding {len(unmatched)} RTL nodes …")

    # Build text and embed
    texts = [_best_text(n) for n in unmatched]

    # Use embedder.py to stay consistent with golden entity embedding model
    from local_graphrag.embedder import _embed_sentence_transformers, _embed_openai
    if backend == "openai":
        vectors = _embed_openai(texts)
    else:
        vectors = _embed_sentence_transformers(texts)

    # Precompute golden embeddings as list for speed
    golden_embs = [(g, g["embedding"]) for g in golden_entities if g.get("embedding")]

    matches = []
    for node, vec in zip(unmatched, vectors):
        best_score  = 0.0
        second_best = 0.0
        best_golden = None
        for g, emb in golden_embs:
            s = _cosine(vec, emb)
            if s > best_score:
                second_best = best_score
                best_score  = s
                best_golden = g
            elif s > second_best:
                second_best = s

        if best_golden and best_score >= min_score and _embedding_gate(
            node=node, golden=best_golden, score=best_score, second_best=second_best, min_score=min_score
        ):
            matches.append({
                "rtl":    node,
                "golden": best_golden,
                "score":  round(best_score, 4),
                "method": "embedding",
            })

    print(f"  [bridge] Stage 2 (embedding): {len(matches)} matches above {min_score}")
    return matches


# ---------------------------------------------------------------------------
# Edge builder
# ---------------------------------------------------------------------------

def build_edges(
    matches: list[dict],
    prefix: str,
    repo: str,
) -> list[dict]:
    """Assemble RESOLVED_TO edge dicts with full LPG schema."""
    golden_col = f"{prefix}Golden_Entities"
    edges = []

    for m in matches:
        node    = m["rtl"]
        golden  = m["golden"]
        method  = m["method"]
        score   = m["score"]

        rtl_col    = "RTL_Port" if node["node_type"] == "port" else "RTL_Signal"
        from_type  = "RTLPort"  if node["node_type"] == "port" else "RTLSignal"

        edge_key = hashlib.md5(
            f"{node['_key']}:{golden['_key']}:{method}".encode()
        ).hexdigest()[:16]

        edges.append({
            "_key":         edge_key,
            "_from":        f"{rtl_col}/{node['_key']}",
            "_to":          f"{golden_col}/{golden['_key']}",
            "type":         "RESOLVED_TO",
            "labels":       ["RESOLVED_TO"],
            "fromNodeType": from_type,
            "toNodeType":   "GoldenEntity",
            "repo":         repo,
            "score":        score,
            "method":       method,
            "rtl_name":     node.get("name", ""),
            "rtl_expanded": node.get("expanded_name") or "",
            "rtl_module":   node.get("parent_module", ""),
            "rtl_type":     node["node_type"],
            "golden_name":  golden.get("name", ""),
            "golden_type":  golden.get("type", ""),
        })

    return edges


# ---------------------------------------------------------------------------
# Main pipeline for one repo
# ---------------------------------------------------------------------------

def build_for_repo(
    db,
    prefix: str,
    repo: str,
    min_score: float = DEFAULT_MIN_SCORE,
    dry_run: bool = False,
    backend: str = "sentence_transformers",
) -> dict:
    """Run both stages for one repo and optionally write edges to ArangoDB."""
    print(f"\n{'='*60}")
    print(f"  RESOLVED_TO: {repo}  (prefix={prefix})")
    print(f"{'='*60}")

    golden_entities = load_golden_entities(db, prefix)
    if not golden_entities:
        return {"repo": repo, "exact": 0, "embedding": 0, "total": 0}

    ports, signals = load_rtl_nodes(db, repo)
    all_rtl = ports + signals

    # Stage 1 — exact
    exact_matches = match_exact(all_rtl, golden_entities)
    matched_keys  = {m["rtl"]["_key"] for m in exact_matches}

    # Stage 2 — embedding
    emb_matches = match_embedding(
        all_rtl, golden_entities, matched_keys,
        min_score=min_score, backend=backend,
    )

    all_matches = exact_matches + emb_matches
    edges = build_edges(all_matches, prefix, repo)

    if dry_run:
        print(f"\n[dry-run] {repo}: {len(exact_matches)} exact + {len(emb_matches)} embedding "
              f"= {len(edges)} RESOLVED_TO edges (not written)")
        _print_sample(all_matches)
        return {"repo": repo, "exact": len(exact_matches),
                "embedding": len(emb_matches), "total": len(edges)}

    # Write
    _ensure_edge_col(db, RESOLVED_TO_EDGE_COL)
    _ensure_vertex_centric_indexes(db, RESOLVED_TO_EDGE_COL)

    if edges:
        db.aql.execute(
            "FOR doc IN @docs INSERT doc INTO @@col "
            "OPTIONS {overwriteMode: 'replace'} LET x=1 RETURN x",
            bind_vars={"docs": edges, "@col": RESOLVED_TO_EDGE_COL},
        )
        print(f"  [bridge] Written {len(edges)} RESOLVED_TO edges for {repo}")

    _print_sample(all_matches)
    return {"repo": repo, "exact": len(exact_matches),
            "embedding": len(emb_matches), "total": len(edges)}


def _print_sample(matches: list[dict], n: int = 5) -> None:
    """Print a sample of match pairs for manual QA."""
    sample = sorted(matches, key=lambda m: -m["score"])[:n]
    if not sample:
        return
    print("\n  Sample matches (top by score):")
    for m in sample:
        rtl = m["rtl"]
        g   = m["golden"]
        expanded = rtl.get("expanded_name") or rtl.get("name")
        print(f"    [{m['method']:9s} {m['score']:.3f}]  "
              f"{rtl['node_type']:6s} '{expanded}' ({rtl['_key'][:30]}…)"
              f"  →  '{g['name']}' [{g['type']}]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build RESOLVED_TO edges from RTL nodes to Golden Entities."
    )
    parser.add_argument("--repo",      type=str, help="Prefix of one repo, e.g. OR1200_")
    parser.add_argument("--all",       action="store_true", help="Process all registered repos")
    parser.add_argument("--dry-run",   action="store_true", help="Inspect candidates; no DB writes")
    parser.add_argument("--truncate",  action="store_true", help="Truncate RESOLVED_TO before rebuild")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help=f"Cosine similarity threshold (default: {DEFAULT_MIN_SCORE})")
    parser.add_argument("--backend",   choices=["sentence_transformers", "openai"],
                        default="sentence_transformers",
                        help="Embedding backend")
    args = parser.parse_args()

    if args.repo and not re.match(r'^[A-Za-z0-9_]+$', args.repo):
        parser.error(f"Invalid repo prefix: {args.repo!r} — must be alphanumeric/underscore only")

    if not args.repo and not args.all:
        parser.error("Specify --repo PREFIX or --all")

    db = get_db()
    print(f"[bridge] Connected to {ARANGO_DATABASE} @ {ARANGO_ENDPOINT}")

    if args.truncate and not args.dry_run:
        if db.has_collection(RESOLVED_TO_EDGE_COL):
            db.collection(RESOLVED_TO_EDGE_COL).truncate()
            print(f"[bridge] Truncated: {RESOLVED_TO_EDGE_COL}")

    registry = load_repo_registry()

    if args.repo:
        repos = [r for r in registry if r["prefix"].rstrip("_") == args.repo.rstrip("_")
                                     or r["prefix"] == args.repo]
        if not repos:
            # Treat --repo argument as the prefix directly
            repos = [{"prefix": args.repo, "name": args.repo.rstrip("_")}]
    else:
        repos = registry

    results = []
    for repo_cfg in repos:
        prefix = repo_cfg["prefix"]
        repo   = prefix.rstrip("_")
        result = build_for_repo(
            db, prefix, repo,
            min_score=args.min_score,
            dry_run=args.dry_run,
            backend=args.backend,
        )
        results.append(result)

    print(f"\n{'='*60}")
    print("  RESOLVED_TO Summary")
    print(f"{'='*60}")
    total = 0
    for r in results:
        print(f"  {r['repo']:20s}  exact={r['exact']:4d}  "
              f"embedding={r['embedding']:4d}  total={r['total']:4d}")
        total += r["total"]
    print(f"  {'TOTAL':20s}  {total:4d} edges")


if __name__ == "__main__":
    main()
