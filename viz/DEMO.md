# ChronoGraph — 5-minute demo script

A guided walkthrough that hits all four pillars. Assumes `python server.py` is
running at http://127.0.0.1:8700.

---

### 0. Setup (10s)
Open the app. You land in **Traceability** projection with **or1200 + mor1kx**
enabled and the time cursor at *now* (2026). The badge top-right reads
`SNAPSHOT` (or `LIVE` if the DB is connected).

> Talking point: "Every node here has bitemporal validity. The graph you see is
> a *slice* — exactly what was valid at the instant under the playhead."

### 1. Time-travel & epochs (60s)
1. Click **⟸ origin**. The graph collapses to the earliest state (2009, or1200's
   initial commit — 58 modules).
2. Press **▶**. Watch the graph grow as commits land and modules appear; the
   left panel's "AT THIS MOMENT" counts climb per project.
3. Press **⏸**, then step with **⏭** through epoch boundaries. The left panel
   shows the *current epoch* per project (type + git tag).
4. Point at the timeline: orange bands = `major_refactor`, green = `milestone_tag`.
   "mor1kx has 100 epochs of real git history; ibex has 268."

> Pillar: **travel backwards/forwards in time; step through epochs.**

### 2. Provenance to the exact source location (75s)
1. Search **`cpu`** (top bar) → pick **or1200_cpu**. It centers and its
   neighborhood highlights.
2. In the inspector:
   - **Temporal**: introduced by commit `09f75354c7` (unneback), "or1200 added
     from or1k subversion repository".
   - **Source text — 13 chunks**: each is a spec passage with the matched term
     (`cpu`) highlighted.
3. Click **↪ open in document** on the "CPU/FPU/DSP" chunk → the full spec text
   opens with the supporting sentence highlighted and scrolled into view.
4. Close, then click **↪ open source** under **Verilog** → `or1200_cpu.v` opens
   with the module highlighted.

> Pillar: **provenance of entities derived from documents; jump to the exact
> location in the text/Verilog that supports a concept.**

### 3. Traceability ↔ Consolidated projection (60s)
1. Toggle to **Consolidated**. or1200's raw web collapses into golden entities —
   square nodes sized by port/signal count, badged with their text-reference
   count (`▤N`).
2. Select **or1200_cpu** again. Now it's one consolidated entity with **direct**
   links to its chunks and its Verilog, and its **Relations** carry consolidated
   evidence badges: `verilog`, `N doc`.

> Pillar: **projections from full traceability to a consolidated entity that
> references the chunks and Verilog it was found in, with relations carrying
> consolidated reference info.**

### 4. Cross-project epoch relatedness (60s)
1. Enable **marocc.** and **ibex** chips. The timeline now shows four swimlanes;
   note the `↳ from or1200` / `↳ from mor1kx` lineage annotations.
2. In the graph, dashed **orange** edges = `evolved from`; dotted = `similar
   concept`. Follow **or1200_cpu → mor1kx_cpu_fourstage → or1k_marocchino_cpu`.
3. Select **or1200_cpu** → the inspector's **Cross-project lineage** lists the
   sibling concept in each other project, tagged `evolved` / `similar`.
4. Scrub time: the cross-project links appear only once both projects have that
   concept alive — you're watching a concept propagate across the OpenRISC family.

> Pillar: **view across projects and see how an epoch in one project relates to
> another.**

---

### Reset
**now ⟹** returns to the present; toggle back to **Traceability** + or1200/mor1kx
for the next run.
