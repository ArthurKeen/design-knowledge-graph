---
name: arangodb-visualizer-customizer
description: Installs and maintains ArangoDB Graph Visualizer customization assets (themes, saved queries, canvas actions, and viewpoint links). Use when the user mentions ArangoDB visualizer, Graph Visualizer, themes, canvas actions, saved queries, stored queries, or collections like _graphThemeStore, _canvasActions, _editor_saved_queries, _viewpoints, or _viewpointActions.
---

# ArangoDB Visualizer Customizer (themes, saved queries, canvas actions)

This skill provides a repeatable, idempotent workflow for customizing the ArangoDB **Graph Visualizer** by installing:

- **Themes** (colors/icons) in `_graphThemeStore`
- **Saved queries** in `_editor_saved_queries` (global AQL editor) and `_queries` (Graph Visualizer Queries panel)
- **Canvas actions** in `_canvasActions`
- **Viewpoint links** in `_viewpointActions` (`_viewpoints/*` → `_canvasActions/*`) and `_viewpointQueries` (`_viewpoints/*` → `_queries/*`)

## Quick start checklist (most reliable order)

1. **Confirm graph + DB**: target database name and graph name (e.g. `DataGraph`).
2. **Create or ensure a viewpoint exists**:
   - **Programmatic** (preferred): use `ensure_default_viewpoint()` below — no manual step.
   - **Manual fallback**: open the graph once in the web UI (Graphs → your graph).
3. **Install theme** (always target DB):
   - Upsert into `_graphThemeStore` by `graphId + name`.
   - Keep the built-in `Default` theme as the **only** `isDefault: true`; install custom themes as `isDefault: false` (a theme marked default **cannot be saved/edited in the UI** — see "Multiple themes per graph").
   - Preserve `createdAt` on updates; always pass `check_rev=False` on `replace()`.
4. **Install saved queries** (target DB for managed/cloud; `_system` for self-hosted):
   - Upsert into `_editor_saved_queries` with **both `content` and `value`** (never `queryText`).
   - For the Graph Visualizer "Queries" panel: upsert into `_queries` with `queryText` and link via `_viewpointQueries`.
5. **Install canvas actions** (target DB for managed/cloud; `_system` for self-hosted):
   - Upsert into `_canvasActions` with `queryText` (never `content`).
   - **Name each action `[<applicable types>] <base name>`** (the Visualizer shows every action on every node; the prefix tells the analyst what it applies to). Store the list in `applicableTypes` too. See "Naming convention" below.
   - Use a stable `_key` derived from `graph_name + action_name` (includes the prefix), and **reconcile** on re-run so renamed actions don't orphan.
6. **Link actions to viewpoint** (target DB):
   - `_viewpointActions` edges: `_viewpoints/{id}` → `_canvasActions/{key}`
   - `_viewpointQueries` edges: `_viewpoints/{id}` → `_queries/{key}`
7. **Verify**:
   - Theme: Legend → custom theme auto-applied; nodes coloured correctly.
   - Saved queries: Queries panel — click query → AQL text appears (not empty).
   - Canvas actions: right-click node → Canvas Actions submenu.

---

## Key implementation details

### `_system` vs target DB

| Deployment type | Saved queries + canvas actions |
|---|---|
| **Managed / cloud (GAE, ArangoGraph)** | **Target DB** — `_system` is read-only or inaccessible |
| **Self-hosted / Docker** | Either `_system` (shared) or target DB (per-graph) |

**Default assumption**: use target DB. Only switch to `_system` if explicitly told it's self-hosted and shared queries are desired.

### Collection creation

Always pass `system=True` for `_`-prefixed collections:

```python
def ensure_collection(db, name: str, edge: bool = False) -> None:
    if not db.has_collection(name):
        db.create_collection(name, edge=edge, system=name.startswith("_"))
```

**Important**: ArangoDB does NOT reliably auto-create `_viewpointActions` or `_viewpointQueries`. Always create them explicitly before inserting edges.

### Viewpoints

Canvas actions and stored queries do **not** show up in the Graph Visualizer until linked to a "viewpoint".

```python
def ensure_default_viewpoint(db, graph_name: str) -> str:
    """Return the _id of the Default viewpoint, creating it if absent."""
    ensure_collection(db, "_viewpoints")
    vp_col = db.collection("_viewpoints")
    for query in [
        {"graphId": graph_name, "name": "Default"},
        {"graphId": graph_name},
    ]:
        existing = list(vp_col.find(query))
        if existing:
            return existing[0]["_id"]
    now = datetime.utcnow().isoformat() + "Z"
    res = vp_col.insert({
        "graphId": graph_name, "name": "Default",
        "description": f"Default viewpoint for {graph_name}",
        "createdAt": now, "updatedAt": now,
    })
    return res["_id"]
```

Viewpoint link collections and their targets:

| Edge collection | `_from` | `_to` | Purpose |
|---|---|---|---|
| `_viewpointActions` | `_viewpoints/{id}` | `_canvasActions/{key}` | Right-click canvas actions |
| `_viewpointQueries` | `_viewpoints/{id}` | **`_queries/{key}`** | Graph Visualizer "Queries" panel |

> **CRITICAL**: `_viewpointQueries` must point to `_queries`, NOT `_editor_saved_queries`. These are entirely different collections used by different UIs.

---

## Themes

### nodeConfigMap and edgeConfigMap structure

```json
{
  "Person": {
    "background": { "color": "#3182ce", "iconName": "fa6-solid:user" },
    "labelAttribute": "label",
    "hoverInfoAttributes": ["label", "riskScore", "inferredRisk"],
    "rules": []
  }
}
```

Fields:
- **`background.color`**: hex color (base/default color)
- **`background.iconName`**: Font Awesome 6 format — `fa6-solid:user`, `fa6-solid:building`, `fa6-solid:ship`, `fa6-solid:plane`, `fa6-solid:server`, `fa6-solid:cube`, `fa6-solid:shapes`, `fa6-solid:database`, `fa6-solid:link`, `fa6-solid:credit-card`, `fa6-solid:vial`, `fa6-solid:triangle-exclamation`
- **`labelAttribute`**: document field to show as the node label. **Must match a field that actually exists on the document** — nodes silently show empty labels if the field is missing.
- **`hoverInfoAttributes`**: fields shown on hover. Include `inferredRisk`/`riskScore` for risk-themed graphs.
- **`rules`**: conditional styling rules (see below)

**DO NOT** use flat fields like `color`, `icon`, `label`, `size`, `tooltip`.

Edge config:
```json
{
  "owned_by": {
    "lineStyle": { "color": "#d69e2e", "thickness": 1.2 },
    "arrowStyle": { "sourceArrowShape": "none", "targetArrowShape": "triangle" },
    "labelStyle": { "color": "#1d2531" },
    "hoverInfoAttributes": [],
    "rules": []
  }
}
```

### Theme rules — the structured attribute-based schema (VERIFIED)

> **This schema was reverse-engineered from a rule authored and saved through the live Graph Visualizer UI.** Earlier versions of this skill documented a flat `{ "name", "condition": "<expr string>", "background" }` shape. **That is WRONG for the current Visualizer** — flat `condition`-string rules render as **black nodes** and show **blank** in the Attribute-based editor. Use the nested object schema below.

Each node rule is an object with this exact shape:

```json
{
  "id": "f426b369-6c11-4d4e-9fdd-2b6aaff27378",
  "attributePath": "inferredRisk",
  "attributeType": "number",
  "conditionType": "singleValue",
  "condition": {
    "op": ">=",
    "right": { "type": "literal", "value": 0.7 },
    "config": {
      "background": { "color": "#e53e3e", "iconName": "mdi:table" },
      "labelAttribute": "",
      "hoverInfoAttributes": [],
      "rules": []
    },
    "enabledFields": { "color": true, "icon": false, "labelAttribute": false, "hoverInfoAttributes": false }
  }
}
```

Field meanings:
- **`id`**: unique per-rule UUID. Required — the Visualizer keys rules by `id`. Generate a fresh `uuid4` per rule (do not hardcode/duplicate). `install_theme.py` injects these on install.
- **`attributePath`**: the document attribute the rule keys off (e.g. `inferredRisk`, `dataSource`).
- **`attributeType`**: `"number"` or `"string"`.
- **`conditionType`**: `"singleValue"` for a single comparison (the only verified type).
- **`condition.op`**: operator string — `">="`, `"<="`, `"<"`, `">"`, `"=="`, etc.
- **`condition.right`**: `{ "type": "literal", "value": <number-or-string> }` — the comparison value. Numbers are bare; strings are plain (no quotes).
- **`condition.config`**: the style applied when the rule matches. Mirrors a node config (`background.color` / `background.iconName`, `labelAttribute`, `hoverInfoAttributes`, nested `rules: []`). `iconName` defaults to `"mdi:table"` even when unused.
- **`condition.enabledFields`**: which parts of `config` are active — `{ "color": true/false, "icon": …, "labelAttribute": …, "hoverInfoAttributes": … }`. To color only, set `color: true` and the rest `false`.

> **CRITICAL gotchas that look like "corruption":**
> - Flat `condition`-string rules → **black nodes** + blank editor. Always use the nested object above.
> - Missing `attributePath` / `attributeType` → editor can't populate the attribute dropdown.
> - Missing/duplicate `id` → editor misbehaves (stuck toggles, etc.).
> - To apply a color you must set `enabledFields.color: true` **and** put the color in `condition.config.background.color`.

Rules are evaluated in order; **the first matching rule wins** (there is no `AND` within one rule — model ranges as ordered rules).

**Risk threshold example** — order matters (narrower bounds first):

```json
"rules": [
  { "id": "<uuid>", "attributePath": "inferredRisk", "attributeType": "number", "conditionType": "singleValue",
    "condition": { "op": ">=", "right": { "type": "literal", "value": 0.7 },
      "config": { "background": { "color": "#e53e3e", "iconName": "mdi:table" }, "labelAttribute": "", "hoverInfoAttributes": [], "rules": [] },
      "enabledFields": { "color": true, "icon": false, "labelAttribute": false, "hoverInfoAttributes": false } } },
  { "id": "<uuid>", "attributePath": "inferredRisk", "attributeType": "number", "conditionType": "singleValue",
    "condition": { "op": "<=", "right": { "type": "literal", "value": 0.3 },
      "config": { "background": { "color": "#48bb78", "iconName": "mdi:table" }, "labelAttribute": "", "hoverInfoAttributes": [], "rules": [] },
      "enabledFields": { "color": true, "icon": false, "labelAttribute": false, "hoverInfoAttributes": false } } },
  { "id": "<uuid>", "attributePath": "inferredRisk", "attributeType": "number", "conditionType": "singleValue",
    "condition": { "op": "<", "right": { "type": "literal", "value": 0.7 },
      "config": { "background": { "color": "#d69e2e", "iconName": "mdi:table" }, "labelAttribute": "", "hoverInfoAttributes": [], "rules": [] },
      "enabledFields": { "color": true, "icon": false, "labelAttribute": false, "hoverInfoAttributes": false } } }
]
```

Why this order: "Medium" (`< 0.7`) would also match the Low bucket if it came first, so High (`>= 0.7`) then Low (`<= 0.3`) then Medium (`< 0.7`).

**Nodes with a missing/null attribute** match no rule and fall back to the base `background.color`.

> **Authoring tip:** when in doubt about the exact shape for a new operator/condition type, create one rule in the Visualizer UI, **Save** the theme, then read the stored doc back from `_graphThemeStore` and use it as the template (the theme must be non-default to be saveable — see below). Edge rules use an analogous nested `condition` but with `lineStyle` in `config`; author one in the UI to confirm before scripting.

### Multiple themes per graph

A graph can have multiple themes with only one `isDefault: true`. Example:

| Theme name | `isDefault` | Purpose |
|---|---|---|
| `Default` (UI built-in) | **true** | Auto-applied default; fallback styling |
| `sentries_standard` | false | Fixed colors by entity type |
| `sentries_risk_heatmap` | false | Dynamic risk-based coloring |

The `isDefault: true` theme auto-applies when the graph is opened. Install custom themes as `isDefault: false` so users opt into them via the Legend.

> **GOTCHA — a theme with `isDefault: true` cannot be saved/edited in the Visualizer UI.** If you mark a custom theme (e.g. `sentries_risk_heatmap`) as the default, "Save changes" is disabled for it and edits silently fail to persist. Keep the built-in `Default` theme as the only `isDefault: true`, and make custom themes non-default. Also ensure exactly **one** `isDefault: true` per `graphId` — multiple defaults (or a custom default) is a common corruption source.

### Helper functions

```python
import copy

def prune_theme(theme_raw: dict, vertex_colls: set, edge_colls: set) -> dict:
    """Keep only collections that exist in the graph schema."""
    theme = copy.deepcopy(theme_raw)
    if "nodeConfigMap" in theme:
        theme["nodeConfigMap"] = {k: v for k, v in theme["nodeConfigMap"].items() if k in vertex_colls}
    if "edgeConfigMap" in theme:
        theme["edgeConfigMap"] = {k: v for k, v in theme["edgeConfigMap"].items() if k in edge_colls}
    return theme


def ensure_visualizer_shape(theme: dict) -> None:
    """Add required default fields that the Visualizer expects."""
    for node_cfg in theme.get("nodeConfigMap", {}).values():
        node_cfg.setdefault("rules", [])
        node_cfg.setdefault("hoverInfoAttributes", [])
    for edge_cfg in theme.get("edgeConfigMap", {}).values():
        edge_cfg.setdefault("rules", [])
        edge_cfg.setdefault("hoverInfoAttributes", [])
        edge_cfg.setdefault("arrowStyle", {"sourceArrowShape": "none", "targetArrowShape": "triangle"})
        edge_cfg.setdefault("labelStyle", {"color": "#1d2531"})
```

---

## Canvas actions

### Schema-driven generation

Always introspect the graph schema first — generate `WITH` clauses and per-collection actions dynamically rather than hardcoding:

```python
def get_graph_schema(db, graph_name: str, exclude_vertex_colls: set = None):
    """Return (vertex_colls, edge_colls) sets, or (None, None) if graph not found."""
    if not db.has_graph(graph_name):
        return None, None
    g = db.graph(graph_name)

    # WARNING: g.edge_definitions() returns dicts with key "edge_collection" (python-arango
    # normalized format). Do NOT use AQL on _graphs to get edge defs — that returns "collection"
    # (raw ArangoDB format). Mixing the two causes silent lookup failures (KeyError → False).
    vertex_colls = set(g.vertex_collections())
    edge_colls = set(ed["edge_collection"] for ed in g.edge_definitions())
    if exclude_vertex_colls:
        vertex_colls -= exclude_vertex_colls
    return vertex_colls, edge_colls


def build_with_clause(vertex_colls: set, edge_colls: set) -> str:
    return "WITH " + ", ".join(sorted(vertex_colls | edge_colls))
```

### Action document shape

```python
import re

def _slugify(s: str) -> str:
    """Derive a stable _key from a human-readable string."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

# Stable key: graph_name + action_name slugified
action_key = _slugify(f"{graph_name}_{action_name}")
```

Key fields:
- **`_key`**: stable, derived from `graph_name + action_name` (never auto-generated)
- **`name`**: display name in right-click menu — **must follow the type-prefix naming convention below**
- **`queryText`**: the AQL (NOT `content` / `value` — those are for saved queries)
- **`graphId`**: must match the graph name exactly
- **`bindVariables`**: `{"nodes": []}` for node-based actions; `{}` for canvas-level actions
- **`applicableTypes`** (recommended): the list of vertex types the action applies to, stored explicitly so the name can be regenerated and tooling can reason about it (mirrors the name prefix — see below)

### Naming convention — prepend the applicable vertex type(s) ★

**The Visualizer shows every registered canvas action on every node's right-click
menu — it does NOT filter by node type.** So the action's *name* is the only cue
an analyst has for which node types the action is meaningful on. The house
convention is therefore: **prepend a bracketed list of applicable vertex types to
the action name.**

```
[<Type1, Type2, …>] <Action base name>
```

Examples:
- `[Organization, GoldenEntity] UBO breakdown (owners)`   ← applies to owned entities
- `[Organization, GoldenEntity, Person] Trace to sanctioned source`
- `[Person, Organization] Show duplicate candidates`

Build it from an explicit type list so the `_key`, the display `name`, and the
stored `applicableTypes` field never drift:

```python
def action_name(types: list[str], base: str) -> str:
    return f"[{', '.join(types)}] {base}"

name = action_name(types, base)                 # "[Organization, GoldenEntity] UBO breakdown"
key  = _slugify(f"{graph_name}_{name}")          # stable _key includes the prefix
doc  = {"_key": key, "graphId": graph_name, "name": name,
        "applicableTypes": types, "queryText": aql, "bindVariables": {"nodes": []}}
```

Rules of thumb for the type list:
- List the vertex type(s) the action's traversal is **rooted on / designed for**
  (e.g. a `1..5 INBOUND node owns_resolved` UBO action → the *owned* types).
- Order from most-specific/primary to least; keep it short.
- **Applies to every node?** Don't enumerate — use the wildcard **`[*]`**
  (e.g. `[*] Expand relationships`). Every action still carries a bracket prefix,
  so a *missing* prefix always reads as an omission, never as "applies to all".
  In code, pass `types=["*"]`; `action_name` renders it as `[*]`.
- **Renaming implication:** because the prefix is part of `name`, it's part of the
  `_key`. Changing an action's types/name changes its `_key`, so the old document
  becomes an orphan. Re-runs must **reconcile** — after upserting the current set,
  delete `_canvasActions` for the graph whose `_key` isn't in the new set (and
  their `_viewpointActions` edges). See the reconcile helper below.

```python
def reconcile_actions(db, graph_name, keep_keys: set[str]) -> None:
    """Idempotency across renames: drop canvas actions (and their viewpoint
    edges) for this graph that weren't (re)installed this run."""
    actions, links = db.collection("_canvasActions"), db.collection("_viewpointActions")
    for a in list(actions.find({"graphId": graph_name})):
        if a["_key"] not in keep_keys:
            for e in links.find({"_to": a["_id"]}):
                links.delete(e["_key"])
            actions.delete(a["_key"])
```

Reference implementation: `project-sentinel/scripts/install_visualizer_assets.py`
(`action_name`, `upsert_action` returning its key, `reconcile_actions`).

### Action query patterns

```python
with_clause = build_with_clause(vertex_colls, edge_colls)
edge_list_str = ", ".join(sorted(edge_colls))

# General 2-hop explorer — use RETURN e (edges only; Visualizer resolves vertices)
default_query = f"""{with_clause}
FOR node IN @nodes
  FOR v, e IN 1..2 ANY node GRAPH "{graph_name}"
  LIMIT 100
  RETURN e"""

# Per-collection 1-hop expand — use RETURN p (full path including start node)
per_coll_query = f"""{with_clause}
FOR node IN @nodes
  FILTER IS_SAME_COLLECTION("{v_coll}", node)
  FOR v, e, p IN 1..1 ANY node {edge_list_str}
  LIMIT 20
  RETURN p"""
```

**`RETURN e` vs `RETURN p`**:
- `RETURN e` — edges only; the Visualizer auto-adds connected vertices. Best for multi-hop explorers where start node is already on canvas.
- `RETURN p` — full paths (start vertex + edge + end vertex). Best for per-collection expand actions where you want the complete subgraph rendered.

Never `RETURN v` — vertex-only results are not rendered as graph expansions.

### Stable upsert helper

```python
def _upsert_canvas_action(canvas_col, vp_act_col, vp_id, graph_name, name,
                           description, query_text, bind_vars, now):
    """Upsert with stable _key; deduplicate orphans created before stable keys."""
    existing = sorted(canvas_col.find({"name": name, "graphId": graph_name}),
                      key=lambda d: d.get("_key", ""))
    if existing:
        for orphan in existing[1:]:          # remove duplicates
            for e in vp_act_col.find({"_to": orphan["_id"]}):
                vp_act_col.delete(e["_key"])
            canvas_col.delete(orphan["_key"])
        doc = {
            "_key": existing[0]["_key"], "_id": existing[0]["_id"],
            "graphId": graph_name, "name": name, "description": description,
            "queryText": query_text, "bindVariables": bind_vars,
            "createdAt": existing[0].get("createdAt", now), "updatedAt": now,
        }
        canvas_col.replace(doc, check_rev=False)
        action_id = existing[0]["_id"]
    else:
        doc = {
            "_key": _slugify(f"{graph_name}_{name}"),
            "graphId": graph_name, "name": name, "description": description,
            "queryText": query_text, "bindVariables": bind_vars,
            "createdAt": now, "updatedAt": now,
        }
        action_id = canvas_col.insert(doc)["_id"]

    if not list(vp_act_col.find({"_from": vp_id, "_to": action_id})):
        vp_act_col.insert({"_from": vp_id, "_to": action_id,
                            "createdAt": now, "updatedAt": now})
    return action_id
```

---

## Saved queries

### Field name summary (critical — do not confuse)

| Collection | AQL field(s) | Notes |
|---|---|---|
| `_editor_saved_queries` | **`content`** + **`value`** | Set BOTH for cross-version compatibility. `queryText` is IGNORED by the query editor UI |
| `_queries` | **`queryText`** | Used by the Graph Visualizer Queries panel |
| `_canvasActions` | **`queryText`** | `content` / `value` are NOT used |

### Two separate systems

| Feature | Collection | AQL field | Linked via | Purpose |
|---|---|---|---|---|
| **Global query editor** (sidebar) | `_editor_saved_queries` | `content` + `value` | none needed | AQL queries in the editor UI |
| **Graph Visualizer "Queries" panel** | `_queries` | `queryText` | `_viewpointQueries` edges | Starter queries for loading graph data |

### Saved query document shape (`_editor_saved_queries`)

```json
{
  "_key": "my_stable_key",
  "title": "Load Demo Scenarios",
  "name": "Load Demo Scenarios",
  "description": "Loads all synthetic scenario nodes",
  "content": "FOR d IN Person FILTER d.dataSource == 'Synthetic' RETURN d",
  "value":   "FOR d IN Person FILTER d.dataSource == 'Synthetic' RETURN d",
  "bindVariables": {},
  "databaseName": "risk-intelligence"
}
```

### Visualizer Queries panel document shape (`_queries`)

```json
{
  "_key": "my_stable_key_datagraph",
  "name": "Load Demo Scenarios",
  "title": "Load Demo Scenarios",
  "description": "Loads all synthetic scenario nodes",
  "graphId": "DataGraph",
  "queryText": "FOR d IN Person FILTER d.dataSource == 'Synthetic' RETURN d",
  "bindVariables": {}
}
```

---

## Idempotency rules

- **Never create duplicates.** Always check for existing docs by `_key` or (`name` + `graphId`).
- Preserve `createdAt` on updates — only update `updatedAt`.
- Use `check_rev=False` on all `replace()` calls to avoid revision-mismatch errors.
- For viewpoint link edges: check `{"_from": vp_id, "_to": action_id}` before inserting.
- On upsert: prefer `_key`-based lookup over name-based — name changes create orphan duplicates.

---

## Ontology vs data graphs

For **ontology graphs** (Class, Property, Ontology, ObjectProperty, etc.):
- Restrict `nodeConfigMap` and `edgeConfigMap` to ontology collections only.
- Canvas actions: ontology edges only (`domain`, `range`, `subClassOf`). Omit `type` if not needed.
- For `DatatypeProperty`/`ObjectProperty` expand actions: filter on the *traversed* vertex `v`, not `node` (e.g. `FILTER IS_SAME_COLLECTION("DatatypeProperty", v)`).
- If multiple themes installed per project, install only the standard/structural theme for OntologyGraph (skip risk-based themes — there are no risk scores on ontology nodes).

---

## Troubleshooting

### Actions don't appear
- `_viewpoints` empty → use `ensure_default_viewpoint()`.
- Wrong viewpoint → `graphId` on the viewpoint must match the actions' `graphId`.
- Missing `_viewpointActions` edge collection or edges not created.
- Action `_key` collided with an existing action for a different graph → always scope key with graph name.

### Queries appear but are empty in the editor
- **Wrong field**: query editor reads `content` and/or `value`. Set BOTH. Do NOT use `queryText` for `_editor_saved_queries`.
- Stale fields from a previous `col.update()` — use `col.replace(doc, check_rev=False)` instead.

### Queries don't appear in Graph Visualizer "Queries" panel
- **Wrong collection**: Visualizer reads from `_queries`, NOT `_editor_saved_queries`.
- **Missing `_viewpointQueries` edges**: insert edges from `_viewpoints/{id}` to `_queries/{key}`.
- `_viewpointQueries` edge collection may not have been created — use `ensure_collection(db, "_viewpointQueries", edge=True)`.

### Theme not selectable
- `_graphThemeStore` missing → create with `system=True`.
- Theme `graphId` does not match actual graph name in DB.

### Theme doesn't auto-apply
- Missing `isDefault: true`. Set it on the built-in `Default` theme document (not a custom theme — those can't be saved when default).

### Attribute-based rules show blank in the editor / nodes render black ("corrupted")
- Rules use the old **flat** shape (`{ "name", "condition": "<expr string>", "background" }`). The current Visualizer needs the **nested** schema (`conditionType` + `condition.op` + `condition.right.value` + `condition.config` + `enabledFields`) and a per-rule `id`. Flat rules render black and show blank in the editor. See "Theme rules — the structured attribute-based schema" above. Rebuild rules in the nested shape (easiest: author one in the UI, save, read it back as a template).

### Can't save edits to a theme in the UI
- The theme is marked `isDefault: true`. Set it to `false` (and make the built-in `Default` theme the default instead), then reload the Visualizer and re-edit.

### Icons show as generic shapes
- Wrong field structure. Use `"background": {"color": "#hex", "iconName": "fa6-solid:user"}`. NOT flat `"color"`, `"icon"`.
- Invalid FA6 icon name — must start with `fa6-solid:` or `fa6-regular:`.

### Default theme lost after database recreation
- Always install a plain "Default" theme (`isDefault: false`) alongside custom themes.

### `edge_definitions()` key ambiguity (python-arango vs AQL)
- `g.edge_definitions()` (python-arango SDK) returns dicts with key `"edge_collection"`.
- Querying `_graphs` via AQL returns `"collection"` (raw ArangoDB format).
- **Never mix the two** — use the SDK consistently. Mixing causes silent `KeyError → False` in lookup, then a `create_edge_definition()` call on an already-existing definition, which raises `ERR 1921`.

### Collection creation fails with "illegal name"
- `_`-prefixed collections require `system=True`: `db.create_collection("_graphThemeStore", system=True)`.

---

## Concrete reference implementations

| Project | File | Notable for |
|---|---|---|
| `risk-intelligence` | `scripts/install_theme.py` | Multi-theme per graph, ontology/data split, `_queries` panel, schema-driven canvas actions, stable keys |
| `fraud-intelligence` | `scripts/install_graph_themes.py` | `ensure_default_viewpoint()`, `ensure_visualizer_shape()`, `_upsert_canvas_action()` with deduplication |
| `ic-knowledge-graph` | `scripts/setup/install_demo_setup.py` | Saved queries using `content` field |
| `ic-knowledge-graph` | `scripts/setup/install_graphrag_queries.py` | Saved queries + canvas actions combined |
| `network-asset-management-demo` | `scripts/setup/install_visualizer.py` | Consolidated installer for multiple graphs |
