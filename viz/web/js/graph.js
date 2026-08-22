// graph.js — Cytoscape canvas: styling, smooth time-travel reconciliation, selection.
const Graph = (() => {
  let cy = null;
  let onSelect = () => {};
  let repoColors = {};

  const STYLE = [
    { selector: 'node[ntype="repo"]', style: {
      'background-opacity': 0.06, 'background-color': 'data(color)',
      'border-width': 1, 'border-color': 'data(color)', 'border-opacity': 0.5,
      'shape': 'round-rectangle', 'label': 'data(label)', 'font-size': 12, 'font-weight': 700,
      'color': 'data(color)', 'text-valign': 'top', 'text-halign': 'center',
      'text-margin-y': -4, 'padding': '18px', 'text-opacity': 0.9,
    }},
    { selector: 'node[ntype="module"]', style: {
      'background-color': 'data(color)', 'label': 'data(label)', 'font-size': 9,
      'color': '#e6edf3', 'text-valign': 'bottom', 'text-margin-y': 3,
      'width': 'mapData(deg, 0, 12, 16, 46)', 'height': 'mapData(deg, 0, 12, 16, 46)',
      'text-outline-width': 2, 'text-outline-color': '#0d1117', 'text-outline-opacity': 0.7,
      'border-width': 0,
    }},
    { selector: 'node[ntype="module"][?prov]', style: {
      'border-width': 2, 'border-color': '#e6edf3', 'border-opacity': 0.5,
    }},
    { selector: 'node[ntype="consolidated"]', style: {
      'background-color': 'data(color)', 'label': 'data(disp)', 'font-size': 9.5,
      'color': '#e6edf3', 'text-valign': 'bottom', 'text-margin-y': 3, 'text-wrap': 'wrap',
      'shape': 'round-rectangle', 'width': 'mapData(mag, 0, 120, 26, 62)',
      'height': 'mapData(mag, 0, 120, 20, 40)',
      'text-outline-width': 2, 'text-outline-color': '#0d1117', 'text-outline-opacity': 0.75,
      'border-width': 2, 'border-color': '#e6edf3', 'border-opacity': 0.45,
    }},
    { selector: 'edge[etype="DEPENDS_ON"]', style: {
      'width': 1.2, 'line-color': '#4b5563', 'target-arrow-color': '#4b5563',
      'target-arrow-shape': 'triangle', 'arrow-scale': 0.7, 'curve-style': 'bezier', 'opacity': 0.55,
    }},
    { selector: 'edge[etype="CROSS_REPO_EVOLVED_FROM"]', style: {
      'width': 2.2, 'line-color': '#e0724a', 'line-style': 'dashed',
      'target-arrow-color': '#e0724a', 'target-arrow-shape': 'triangle', 'arrow-scale': 1,
      'curve-style': 'unbundled-bezier', 'control-point-distances': [40], 'opacity': 0.85,
      'label': 'data(label)', 'font-size': 8, 'color': '#e0724a', 'text-rotation': 'autorotate',
      'text-background-color': '#0d1117', 'text-background-opacity': 0.7, 'text-background-padding': 2,
    }},
    { selector: 'edge[etype="CROSS_REPO_SIMILAR_TO"]', style: {
      'width': 1.4, 'line-color': '#94a3b8', 'line-style': 'dotted',
      'curve-style': 'unbundled-bezier', 'control-point-distances': [30], 'opacity': 0.5,
    }},
    { selector: '.faded', style: { 'opacity': 0.12, 'text-opacity': 0.05 } },
    { selector: 'node.sel', style: {
      'border-width': 3, 'border-color': '#4f8cff', 'border-opacity': 1,
      'background-blacken': -0.15,
    }},
    { selector: '.neighbor', style: { 'opacity': 1 } },
    { selector: 'edge.hl', style: { 'opacity': 1, 'width': 3 } },
  ];

  function init(containerId, opts) {
    repoColors = opts.repoColors || {};
    onSelect = opts.onSelect || onSelect;
    cy = cytoscape({
      container: document.getElementById(containerId),
      style: STYLE, wheelSensitivity: 0.25,
      minZoom: 0.15, maxZoom: 3, boxSelectionEnabled: false,
    });
    cy.on('tap', 'node[ntype="module"], node[ntype="consolidated"]', (e) => {
      selectNode(e.target.id());
      onSelect(e.target.id());
    });
    cy.on('tap', (e) => { if (e.target === cy) clearSelection(); });
    return cy;
  }

  // augment a slice's nodes/edges with rendering data (color, degree, display)
  function _prepare(slice) {
    const deg = {};
    slice.edges.forEach(e => { deg[e.data.source] = (deg[e.data.source] || 0) + 1;
                               deg[e.data.target] = (deg[e.data.target] || 0) + 1; });
    const nodes = slice.nodes.map(n => {
      const d = { ...n.data };
      d.color = repoColors[d.repo] || '#4f8cff';
      d.deg = deg[d.id] || 0;
      d.prov = d.has_provenance ? 1 : 0;
      if (d.ntype === 'consolidated') {
        d.mag = (d.signal_count || 0) + (d.port_count || 0);
        const t = d.text_refs || 0;
        d.disp = t ? `${d.label}\n▤${t}` : d.label;
      }
      return { data: d };
    });
    return { nodes, edges: slice.edges };
  }

  // reconcile current graph → new slice, preserving positions for stability
  function setData(slice, { relayout = false } = {}) {
    const prepared = _prepare(slice);
    const incoming = new Set([...prepared.nodes, ...prepared.edges].map(e => e.data.id));
    const existingPos = {};
    cy.nodes().forEach(n => { existingPos[n.id()] = n.position(); });

    cy.batch(() => {
      // remove gone
      cy.elements().forEach(el => { if (!incoming.has(el.id())) el.remove(); });
      // add / update
      const newNodeIds = new Set();
      prepared.nodes.forEach(n => {
        const ex = cy.getElementById(n.data.id);
        if (ex.nonempty()) { ex.data(n.data); }
        else { cy.add({ group: 'nodes', data: n.data }); newNodeIds.add(n.data.id); }
      });
      prepared.edges.forEach(e => {
        const ex = cy.getElementById(e.data.id);
        if (ex.empty()) cy.add({ group: 'edges', data: e.data });
        else ex.data(e.data);
      });
      cy._newNodeIds = newNodeIds;
    });

    const newIds = cy._newNodeIds || new Set();
    if (relayout || cy.nodes().length === newIds.size) {
      _layout(null);
    } else if (newIds.size > 0) {
      // keep existing fixed, settle only new nodes
      const fixed = cy.nodes(':childless').filter(n => !newIds.has(n.id()) && existingPos[n.id()])
        .map(n => ({ nodeId: n.id(), position: existingPos[n.id()] }));
      _layout(fixed);
    }
  }

  function _layout(fixed) {
    const opts = {
      name: 'fcose', animate: true, animationDuration: 420, quality: 'default',
      randomize: !fixed, packComponents: true, nodeSeparation: 78,
      idealEdgeLength: e => (e.data('cross') ? 190 : 70),
      nodeRepulsion: 8500, gravity: 0.25, gravityCompound: 0.5,
    };
    if (fixed && fixed.length) opts.fixedNodeConstraint = fixed;
    cy.layout(opts).run();
  }

  function selectNode(id) {
    clearSelection();
    const n = cy.getElementById(id);
    if (n.empty()) return false;
    const hood = n.closedNeighborhood();
    cy.elements().addClass('faded');
    hood.removeClass('faded').addClass('neighbor');
    hood.edges().addClass('hl');
    n.addClass('sel');
    return true;
  }
  function clearSelection() {
    cy.elements().removeClass('faded neighbor hl sel');
  }
  function centerOn(id) {
    const n = cy.getElementById(id);
    if (n.nonempty()) cy.animate({ center: { eles: n }, zoom: 1.1 }, { duration: 350 });
  }
  function fit() { cy.animate({ fit: { padding: 40 } }, { duration: 350 }); }
  function relayout() { _layout(null); }
  function setEdgeVisibility({ depends, cross }) {
    cy.edges('[etype="DEPENDS_ON"]').style('display', depends ? 'element' : 'none');
    cy.edges('[cross]').style('display', cross ? 'element' : 'none');
  }
  function setProvenanceOnly(on) {
    cy.nodes('[ntype="module"]').forEach(n => {
      n.style('display', (!on || n.data('prov')) ? 'element' : 'none');
    });
  }

  return { init, setData, selectNode, clearSelection, centerOn, fit, relayout,
           setEdgeVisibility, setProvenanceOnly, has: (id) => cy.getElementById(id).nonempty(),
           nodeCount: () => cy.nodes('[ntype != "repo"]').length,
           edgeCount: () => cy.edges().length,
           firstProvNode: () => { let n = cy.nodes('[prov = 1]').first();
             if (n.empty()) n = cy.nodes('[ntype = "consolidated"]').first();
             if (n.empty()) n = cy.nodes('[ntype = "module"]').first();
             return n.nonempty() ? n.id() : null; } };
})();
