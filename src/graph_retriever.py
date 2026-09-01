"""
graph_retriever.py - GraphRAG query layer for the IC Knowledge Graph.

Pipeline: embed question -> anchor lookup in Golden Entities ->
          RESOLVED_TO reverse walk -> CROSS_REPO_SIMILAR_TO expansion ->
          community peer lookup -> MentionedIn chunk fetch ->
          context assembly -> LLM synthesis.

CLI:
    PYTHONPATH=src python3 src/graph_retriever.py "What is the TLB flush mechanism?"
    PYTHONPATH=src python3 src/graph_retriever.py "..." --explain --no-llm
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from dataclasses import dataclass, field
from typing import Callable, Optional

from arango import ArangoClient
from arango.database import StandardDatabase
from dotenv import load_dotenv

# Every other entry point picks up .env as a side effect of `from config import
# ...` (config.py calls load_dotenv() at import time). This module doesn't
# import config, so without this it silently falls through to the hardcoded
# localhost:8530/root/"" defaults below instead of the real cluster.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

try:
    from sentence_transformers import SentenceTransformer
    _ST_AVAILABLE = True
except ImportError:
    _ST_AVAILABLE = False

try:
    from openai import OpenAI as _OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
_LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
_LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))

# ---------------------------------------------------------------------------
# AQL templates
# ---------------------------------------------------------------------------

_AQL_EMBED_SEARCH = """
FOR doc IN @@coll
    FILTER doc.embedding != null
    LET score = (
        FOR i IN 0..LENGTH(doc.embedding)-1
            COLLECT AGGREGATE s = SUM(doc.embedding[i] * @qvec[i])
            RETURN s
    )[0]
    SORT score DESC
    LIMIT @top_k
    RETURN MERGE(doc, {_score: score, _collection: @coll_name})
"""
# NOTE: @@coll is a collection-name bind param, valid only where a collection
# is syntactically expected (here: `FOR doc IN @@coll`). This cluster (same
# AMP planner as the ERR 4 CONTAINS/LIKE issue documented in
# cross_repo_bridge.py) rejects reusing it as a plain value with
# "ERR 1568 collection used as expression operand" -- hence the separate
# @coll_name plain-string bind param for the _collection field below.

_AQL_RESOLVED_TO = """
FOR edge IN RESOLVED_TO
    FILTER edge._to == @golden_id
    LET rtl = DOCUMENT(edge._from)
    RETURN {
        rtl_id: rtl._id,
        name:   rtl.name,
        type:   SPLIT(edge._from, "/")[0],
        score:  edge.score,
        method: edge.method,
        repo:   rtl.repo
    }
"""

_AQL_CROSS_REPO = """
FOR edge IN CROSS_REPO_SIMILAR_TO
    FILTER edge._from == @golden_id OR edge._to == @golden_id
    LET other = edge._from == @golden_id ? DOCUMENT(edge._to) : DOCUMENT(edge._from)
    RETURN MERGE(other, {_sim_score: edge.similarity_score})
"""

_AQL_COMMUNITY_MEMBERS = """
FOR doc IN @@coll
    FILTER doc.community == @community_id AND doc._id != @anchor_id
    LIMIT @limit
    RETURN {name: doc.name, description: doc.description, _id: doc._id}
"""

_AQL_MENTIONED_IN = """
LET chunk_ids = UNIQUE(
    FOR c IN @@consolidates_edge
        FILTER c._from == @golden_id
        FOR e IN @@mention_edge
            FILTER e._from == c._to
            RETURN e._to
)
FOR cid IN chunk_ids
    LET chunk = DOCUMENT(cid)
    FILTER chunk != null
    LIMIT @limit
    RETURN {content: chunk.text, source: chunk.doc_basename, section: chunk.section_header}
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GoldenHit:
    id: str
    collection: str
    name: str
    description: str
    community: Optional[int]
    score: float
    rtl_nodes: list = field(default_factory=list)
    cross_repo: list = field(default_factory=list)
    community_peers: list = field(default_factory=list)
    doc_chunks: list = field(default_factory=list)


@dataclass
class RetrievalResult:
    question: str
    golden_hits: list
    context_text: str
    answer: Optional[str] = None


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------

class GraphRetriever:
    """GraphRAG retriever for the IC Knowledge Graph.

    Parameters
    ----------
    host, db_name, username, password : str
        ArangoDB connection. Fall back to ARANGO_HOST / ARANGO_DB /
        ARANGO_USER / ARANGO_PASSWORD env vars.
    embedding_model : str
        SentenceTransformer model name (MPS-aware on Apple Silicon).
    top_k_golden : int
        Number of anchor entities retrieved per question.
    top_k_community : int
        Number of community peers per anchor.
    top_k_chunks : int
        Number of source-doc chunks per anchor.
    llm_fn : callable, optional
        fn(prompt: str) -> str.  Auto-detected from env if None.
    repos : list of str
        Which repo collections to search (default: all four).
    """

    # TODO: Load from config_temporal.REPO_REGISTRY
    REPOS = ["OR1200", "MOR1KX", "MAROCCHINO", "IBEX"]
    GOLDEN_SUFFIX = "_Golden_Entities"
    # TODO: Load from config_temporal.REPO_REGISTRY
    MENTION_EDGES = {
        "OR1200":     "OR1200_MentionedIn",
        "MOR1KX":     "MOR1KX_MentionedIn",
        "MAROCCHINO": "MAROCCHINO_MentionedIn",
        "IBEX":       "IBEX_MentionedIn",
    }

    def __init__(
        self,
        host: str = None,
        db_name: str = None,
        username: str = None,
        password: str = None,
        embedding_model: str = _EMBEDDING_MODEL,
        top_k_golden: int = 5,
        top_k_community: int = 4,
        top_k_chunks: int = 3,
        llm_fn: Optional[Callable] = None,
        repos: list = None,
    ):
        self.host     = host     or os.getenv("ARANGO_HOST",
                                              os.getenv("ARANGO_ENDPOINT", "http://localhost:8530"))
        self.db_name  = db_name  or os.getenv("ARANGO_DB",
                                              os.getenv("ARANGO_DATABASE", "ic-knowledge-graph-temporal"))
        self.username = username or os.getenv("ARANGO_USER",
                                              os.getenv("ARANGO_USERNAME", "root"))
        self.password = password or os.getenv("ARANGO_PASSWORD", "")
        self.top_k_golden    = top_k_golden
        self.top_k_community = top_k_community
        self.top_k_chunks    = top_k_chunks
        self.llm_fn  = llm_fn if llm_fn is not None else self._default_llm()
        self.repos   = repos or self.REPOS

        self._db       = self._connect()
        self._embedder = self._load_embedder(embedding_model)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, question: str, verbose: bool = False) -> RetrievalResult:
        """Full pipeline: embed -> traverse -> assemble -> synthesise."""
        qvec = self._embed(question)
        hits = self._anchor_lookup(qvec)
        if verbose:
            print(f"[retriever] Anchors: {[h.name for h in hits]}")
        for hit in hits:
            self._expand_hit(hit)
        ctx = self._format_context(question, hits)
        answer = self.llm_fn(ctx) if self.llm_fn else None
        return RetrievalResult(question=question, golden_hits=hits,
                               context_text=ctx, answer=answer)

    def explain(self, question: str) -> dict:
        """Structured breakdown of retrieval (no LLM call)."""
        result = self.query(question)
        return {
            "question": result.question,
            "anchors": [
                {
                    "name":            h.name,
                    "collection":      h.collection,
                    "score":           round(h.score, 4),
                    "community":       h.community,
                    "rtl_nodes":       h.rtl_nodes[:5],
                    "cross_repo":      [c.get("name") for c in h.cross_repo],
                    "community_peers": [p.get("name") for p in h.community_peers],
                    "doc_chunks_n":    len(h.doc_chunks),
                }
                for h in result.golden_hits
            ],
            "context_length": len(result.context_text),
        }

    # ------------------------------------------------------------------
    # Step 1 - anchor lookup
    # ------------------------------------------------------------------

    def _anchor_lookup(self, qvec: list) -> list:
        candidates = []
        for repo in self.repos:
            coll = f"{repo}{self.GOLDEN_SUFFIX}"
            if not self._db.has_collection(coll):
                continue
            try:
                rows = list(self._db.aql.execute(
                    _AQL_EMBED_SEARCH,
                    bind_vars={"@coll": coll, "coll_name": coll, "qvec": qvec,
                              "top_k": self.top_k_golden},
                ))
                candidates.extend(rows)
            except Exception:
                candidates.extend(self._word_fallback(coll))

        candidates.sort(key=lambda d: d.get("_score", 0), reverse=True)
        seen: set = set()
        hits = []
        for doc in candidates:
            key = (doc.get("name", ""), (doc.get("description") or "")[:80])
            if key in seen:
                continue
            seen.add(key)
            hits.append(GoldenHit(
                id=doc["_id"],
                collection=doc.get("_collection", ""),
                name=doc.get("name", ""),
                description=doc.get("description") or "",
                community=doc.get("community"),
                score=doc.get("_score", 0.0),
            ))
            if len(hits) >= self.top_k_golden:
                break
        return hits

    def _word_fallback(self, coll: str) -> list:
        try:
            return list(self._db.aql.execute(
                "FOR d IN @@c LIMIT 20 RETURN MERGE(d, {_score: 0, _collection: @cname})",
                bind_vars={"@c": coll, "cname": coll},
            ))
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Step 2 - expand each anchor
    # ------------------------------------------------------------------

    def _expand_hit(self, hit: GoldenHit) -> None:
        hit.rtl_nodes       = self._get_rtl_nodes(hit.id)
        hit.cross_repo      = self._get_cross_repo(hit.id)
        hit.community_peers = self._get_community_peers(hit)
        hit.doc_chunks      = self._get_doc_chunks(hit)

    def _get_rtl_nodes(self, golden_id: str) -> list:
        if not self._db.has_collection("RESOLVED_TO"):
            return []
        try:
            return list(self._db.aql.execute(
                _AQL_RESOLVED_TO, bind_vars={"golden_id": golden_id}
            ))
        except Exception:
            return []

    def _get_cross_repo(self, golden_id: str) -> list:
        if not self._db.has_collection("CROSS_REPO_SIMILAR_TO"):
            return []
        try:
            return list(self._db.aql.execute(
                _AQL_CROSS_REPO, bind_vars={"golden_id": golden_id}
            ))
        except Exception:
            return []

    def _get_community_peers(self, hit: GoldenHit) -> list:
        if hit.community is None:
            return []
        try:
            return list(self._db.aql.execute(
                _AQL_COMMUNITY_MEMBERS,
                bind_vars={
                    "@coll": hit.collection,
                    "community_id": hit.community,
                    "anchor_id": hit.id,
                    "limit": self.top_k_community,
                },
            ))
        except Exception:
            return []

    def _get_doc_chunks(self, hit: GoldenHit) -> list:
        # Golden Entities have no direct edge to Chunks: the real path is
        # Golden --Consolidates--> (raw) Entities --MentionedIn--> Chunks.
        # Consolidates collections follow the same {repo}_Consolidates naming
        # convention as MENTION_EDGES's {repo}_MentionedIn.
        repo = hit.collection.replace(self.GOLDEN_SUFFIX, "")
        edge = self.MENTION_EDGES.get(repo)
        consolidates = f"{repo}_Consolidates"
        if not edge or not self._db.has_collection(edge) or not self._db.has_collection(consolidates):
            return []
        try:
            return list(self._db.aql.execute(
                _AQL_MENTIONED_IN,
                bind_vars={"golden_id": hit.id, "@mention_edge": edge,
                           "@consolidates_edge": consolidates,
                           "limit": self.top_k_chunks},
            ))
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Step 3 - context assembly
    # ------------------------------------------------------------------

    def _format_context(self, question: str, hits: list) -> str:
        lines = [
            "You are a hardware architecture expert with access to a multi-repository",
            "IC knowledge graph covering OR1200, MOR1KX, MAROCCHINO, and IBEX cores.",
            "",
            f"QUESTION: {question}",
            "",
            "RELEVANT GRAPH CONTEXT",
            "=" * 60,
        ]
        for rank, hit in enumerate(hits, 1):
            lines.append(f"\n## [{rank}] {hit.name}  (score={hit.score:.3f}, {hit.collection})")
            if hit.description:
                lines.append(f"Description: {textwrap.fill(hit.description, 100)}")
            if hit.community_peers:
                lines.append("Community peers: " +
                             ", ".join(p.get("name", "?") for p in hit.community_peers))
            if hit.cross_repo:
                xr = ", ".join(
                    f"{c.get('name','?')} ({round(c.get('_sim_score',0),2)})"
                    for c in hit.cross_repo[:4]
                )
                lines.append(f"Cross-repo analogues: {xr}")
            if hit.rtl_nodes:
                lines.append("RTL implementations:")
                for n in hit.rtl_nodes[:6]:
                    lines.append(
                        f"  * {n.get('name','?')} [{n.get('type','?')}] in {n.get('repo','?')}"
                        f" (match={n.get('method','?')}, score={round(n.get('score',0),2)})"
                    )
            if hit.doc_chunks:
                lines.append("Source document excerpts:")
                for c in hit.doc_chunks:
                    content = (c.get("content") or "")[:400].replace("\n", " ")
                    lines.append(f"  [{c.get('source','?')}] {content}")
        lines += [
            "",
            "=" * 60,
            "Answer concisely and accurately. Cite RTL modules or doc sources where relevant.",
            "If the context is insufficient, say so explicitly.",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> StandardDatabase:
        client = ArangoClient(hosts=self.host)
        return client.db(self.db_name, username=self.username, password=self.password)

    def _load_embedder(self, model_name: str):
        if not _ST_AVAILABLE:
            return None
        model = SentenceTransformer(model_name)
        try:
            import torch
            if torch.backends.mps.is_available():
                model = model.to("mps")
        except Exception:
            pass
        return model

    def _embed(self, text: str) -> list:
        if self._embedder is None:
            raise RuntimeError(
                "sentence_transformers not installed. Run: pip install sentence-transformers"
            )
        vec = self._embedder.encode(text)
        return vec.tolist() if hasattr(vec, "tolist") else list(vec)

    @staticmethod
    def _default_llm() -> Optional[Callable]:
        if _OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
            client = _OpenAI()
            def _openai(prompt: str) -> str:
                resp = client.chat.completions.create(
                    model=_LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                return resp.choices[0].message.content
            return _openai
        if _ANTHROPIC_AVAILABLE and os.getenv("ANTHROPIC_API_KEY"):
            # Fallback when OpenAI is unavailable
            client = _anthropic.Anthropic()
            def _claude(prompt: str) -> str:
                resp = client.messages.create(
                    model="claude-3-5-haiku-20241022",
                    max_tokens=_LLM_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text
            return _claude
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_retriever_from_env() -> GraphRetriever:
    # config.py defines ARANGO_ENDPOINT/ARANGO_DATABASE/ARANGO_USERNAME, not the
    # ARANGO_HOST/ARANGO_DB/ARANGO_USER names this used to import (always raised
    # ImportError, silently falling through to GraphRetriever()'s own hardcoded
    # localhost:8530 defaults). GraphRetriever.__init__ already reads the real
    # env vars directly (with these names as optional aliases) now that .env is
    # actually loaded at module import time, so no explicit args are needed.
    return GraphRetriever()


def main() -> None:
    parser = argparse.ArgumentParser(description="IC Knowledge Graph -- GraphRAG retriever")
    parser.add_argument("question", help="Natural-language question to answer")
    parser.add_argument("--top-k", type=int, default=5,
                        help="Number of golden entity anchors (default 5)")
    parser.add_argument("--explain", action="store_true",
                        help="Print retrieval breakdown only (no LLM)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM; print raw assembled context")
    args = parser.parse_args()

    retriever = _build_retriever_from_env()
    retriever.top_k_golden = args.top_k
    if args.no_llm:
        retriever.llm_fn = None

    if args.explain:
        print(json.dumps(retriever.explain(args.question), indent=2))
        return

    result = retriever.query(args.question, verbose=True)
    print("\n" + "=" * 70)
    print("QUESTION:", result.question)
    print("=" * 70)
    if result.answer:
        print("\nANSWER:\n")
        print(result.answer)
    else:
        print("\nCONTEXT (no LLM configured):\n")
        print(result.context_text)


if __name__ == "__main__":
    main()
