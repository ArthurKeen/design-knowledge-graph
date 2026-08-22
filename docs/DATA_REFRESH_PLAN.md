# Data Refresh Plan — prod.demo temporal knowledge graph

**Status:** EXECUTED 2026-07-21. Backup at `backups/prod_demo_20260720/` (75,048 docs,
JSONL per collection — no `arangodump` binary available locally). Baseline counts in
`backups/baseline_counts_20260720.txt`; diff via `backups/verify_refresh.py`.
**Target:** `https://prod.demo.pilot.arango.ai`, database `ic-knowledge-graph-temporal`.

## Result summary (2026-07-21)

| Repo | Before | After |
|---|---|---|
| or1200 | 1 commit, 1 epoch, 2015-11 upstream unreached | **48 commits, 14 epochs**, through 2015-10 |
| mor1kx | 819 commits (2025-08) | **820 commits** (2026-06) |
| ibex | 2,908 commits (2026-02) | **2,948 commits** (through 2026-07, see note) |
| marocchino | current | unchanged (upstream dormant) |

Root cause of the or1200/mor1kx/ibex staleness was **not** a shallow clone (as
originally suspected) — all three local clones were detached at old commits
from a previously interrupted run. `git checkout -f master && git merge
--ff-only origin/master` fixed all three.

Also fixed in passing: the project's `.venv` pointed at a relocated/broken
interpreter (`/Users/arthurkeen/cadence/.venv/...`, gone) — every `pip`/`python`
call failed. Rebuilt on Homebrew Python 3.11 with `requirements-core.txt` +
`sentence-transformers` + `openai` (needed by `rtl_semantic_bridge.py` /
`cross_repo_bridge.py`) + `fastapi`/`uvicorn` (needed by `viz/server.py`).

**Note on ibex freshness:** `MAX(valid_from_ts)` reads 2026-07-02, one commit
behind upstream HEAD (2026-07-14). All 2,948/2,948 commits were ingested —
nothing is missing. The gap is because `etl_temporal_git.py` keys off git
**author date** (`%at`), and ibex's HEAD commit was rebased before merge:
author date 2026-03-19, committer date 2026-07-14 (Gerrit-style workflow).
This is a pre-existing modeling choice (author date reflects when the code was
actually written, not when it landed on master) — not something this refresh
changed or a bug to silently patch.

GraphRAG (doc chunking/entity extraction) was **not** re-run — `doc/` content
didn't change upstream for any repo, so the golden-entity/chunk layer (858
consolidated entities) is unchanged, only structural/temporal layers grew.

ChronoGraph verified against the refreshed graph: or1200 now shows real epoch
structure (14 epochs vs. the old single tick), time-travel and provenance
drill-down (text + verilog highlighting, cross-repo rows) all confirmed via
the Playwright smoke test.

## Current state (verified 2026-07-20)

The prod.demo database is healthy: all 76 collections match the former
rnd.pilot cluster exactly (RTL_Module 6 404, GitCommit 3 796, DesignEpoch 381,
DesignSituation 721, per-repo GraphRAG layers, RESOLVED_TO 208,
CROSS_REPO_SIMILAR_TO 53 / EVOLVED_FROM 2), the golden→chunk provenance chain
resolves, and ChronoGraph passes its full headless smoke test against it.

### Freshness gaps (identical on both clusters — ingestion-side, not a copy problem)

| Repo | Ingested through | Upstream HEAD | Gap |
|---|---|---|---|
| or1200 | 2009-05-25 (**1 commit**) | 2015-11-11 | local submodule is a **shallow clone**; the replay only ever saw the initial commit |
| mor1kx | 2025-08-21 (819 commits) | 2026-06-06 | ~10 months |
| marocchino | 2022-02-09 (68 commits) | 2022-02-09 | **current** (upstream dormant) |
| ibex | 2026-02-17 (2 908 commits) | 2026-07-14 | ~5 months |

### Why refresh is not automatic

- Re-ingestion **changes epoch boundaries, node counts, and situation sets** —
  anything scripted against today's demo numbers (TEMPORAL_DEMO_SCRIPT.md,
  screenshots) drifts.
- GraphRAG re-runs cost OpenAI tokens (only if docs changed or
  `--embedding-backend openai` is used; git-replay stages are free).
- prod.demo is shared — a long ETL run is visible to other demo users.

## Pre-flight (do these first, in order)

1. **Confirm creds** — `.env` must pair `prod.demo.pilot.arango.ai` with the
   `@Kf…` password (the `YzW…` one belongs to the old a2lc79ac rnd cluster).
   `curl -su "root:$ARANGO_PASSWORD" $ARANGO_ENDPOINT/_api/version` → 200.
2. **Snapshot for rollback** — Enterprise hot backup if available, else
   `arangodump` of the database (a prior dump layout exists under
   `arangodump_ic-knowledge-graph-temporal_oneshard_backup/`):
   ```bash
   arangodump --server.endpoint http+ssl://prod.demo.pilot.arango.ai:443 \
     --server.username root --server.database ic-knowledge-graph-temporal \
     --output-directory backup_$(date +%Y%m%d) --overwrite true
   ```
3. **Capture baseline counts** — save the collection-count table (probe script
   or `db_stats.py`) to diff after the refresh.
4. **Freeze the demo** — don't run during a demo window; ChronoGraph reads
   live, so users will see counts shift mid-refresh.

## Phase 1 — Fix the clones

```bash
# or1200: the submodule is shallow — this is the root cause of the 1-commit history
cd or1200 && git fetch --unshallow origin master && git pull && cd ..

# mor1kx / ibex: clone_manager pulls automatically, but verify:
cd data/repos/mor1kx && git pull && cd ../ibex && git pull && cd ../../..
```

Sanity: `git -C or1200 log --oneline | wc -l` should jump from 1 to ~500+.

## Phase 2 — Temporal ETL (git replay, no LLM cost)

Per repo, or all at once. Start with or1200 alone as the pilot — it's the
smallest and the one with the real defect:

```bash
# pilot: or1200 only, temporal replay only
./scripts/multi_repo/run_all_repos.sh --repo or1200 --no-graphrag

# then the rest (ibex is the long pole: ~200 new commits to replay)
./scripts/multi_repo/run_all_repos.sh --repo mor1kx --no-graphrag
./scripts/multi_repo/run_all_repos.sh --repo ibex  --no-graphrag
```

Notes:
- `--dry-run` first if unsure; `--commit-limit N` to smoke-test the pipeline.
- Skip `marocchino` (already current).
- GraphRAG (`--no-graphrag` omitted) is only needed if `doc/` content changed
  upstream; check with `git -C data/repos/ibex log --stat -- doc | head` before
  paying for extraction. If docs did change, bump `--doc-version`.

## Phase 3 — Post-ingestion derived layers (order matters)

These are the steps `ingest_repo.py` prints at the end; they rebuild everything
that hangs off the temporal spine:

```bash
PYTHONPATH=src python3 src/etl_authors.py                         # Author/AUTHORED/MAINTAINS
PYTHONPATH=src python3 src/situation_detector.py --all            # DesignSituation
PYTHONPATH=src python3 src/rtl_semantic_bridge.py --all           # RESOLVED_TO
PYTHONPATH=src python3 src/cross_repo_bridge.py --all             # CROSS_REPO_*
PYTHONPATH=src python3 scripts/setup/create_snapshot_of_edges.py  # SNAPSHOT_OF
PYTHONPATH=src python3 scripts/temporal/create_temporal_graph.py  # named graph refresh
```

## Phase 4 — Verify

1. Diff collection counts vs the Phase-0 baseline; expect growth in
   GitCommit / RTL_Module / DesignEpoch / MODIFIED / BELONGS_TO_EPOCH only for
   refreshed repos.
2. Re-run the freshness query — per-repo `MAX(valid_from_ts)` should be within
   days of upstream HEAD.
3. ChronoGraph smoke: `cd viz && python server.py`, then run the Playwright
   driver — boot, provenance click, verilog jump, consolidated projection,
   time-travel (node counts must *change* across epoch steps).
4. Spot-check or1200 specifically: it should now have >1 epoch and a real
   timeline lane in ChronoGraph instead of a single tick at 2009.

## Rollback

`arangorestore` the Phase-0 dump into the same database name (or restore the
hot backup). ChronoGraph needs no changes — it reads whatever is there.

## Known cluster quirks to keep in mind

- **ERR 4 planner bug is present on prod.demo** (same AMP build as rnd):
  string predicates (`CONTAINS`/`LIKE`) on collections with the MDI temporal
  index fail. Pipeline code already avoids this (see `cross_repo_bridge.py`,
  `viz/ic_viz/datasource.py`); don't add new string-predicate AQL on
  `RTL_Module`.
- **Insert-then-embed fails** on collections with a non-sparse vector index —
  embed first, insert with the vector (shared_patterns pattern
  `arango-shared-memory_data-model_20260708_032708`). Currently no vector index
  exists on the prod.demo golden-entity collections, so this only matters if
  one is added.

## Estimated scope

| Phase | Time | Cost |
|---|---|---|
| Clones | minutes | — |
| Temporal ETL (or1200 ~500 commits, mor1kx ~10, ibex ~200) | ~1–3 h total, dominated by per-commit RTL parsing on ibex | — |
| GraphRAG (only if docs changed) | ~30 min | OpenAI embeddings + extraction, small |
| Post-ingestion layers | ~15 min | embedding calls in the two bridge steps |
| Verification | ~10 min | — |
