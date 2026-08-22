// timeline.js — multi-repo epoch swimlanes, draggable playhead, epoch stepping.
const Timeline = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  let svg, data = null, activeRepos = [], ts = 0, onSeek = () => {};
  let repoColors = {};
  const M = { left: 96, right: 18, top: 10, bottom: 22 };

  const el = (t, a) => { const e = document.createElementNS(NS, t);
    for (const k in a) e.setAttribute(k, a[k]); return e; };

  function init(svgId, opts) {
    svg = document.getElementById(svgId);
    onSeek = opts.onSeek || onSeek;
    repoColors = opts.repoColors || {};
    // playhead drag
    let dragging = false;
    const seekFromEvent = (ev) => {
      const r = svg.getBoundingClientRect();
      const x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      onSeek(tsOf(x));
    };
    svg.addEventListener('pointerdown', (e) => { dragging = true; svg.setPointerCapture(e.pointerId); seekFromEvent(e); });
    svg.addEventListener('pointermove', (e) => { if (dragging) seekFromEvent(e); });
    svg.addEventListener('pointerup', () => { dragging = false; });
    window.addEventListener('resize', render);
  }

  function W() { return svg.clientWidth || svg.parentElement.clientWidth; }
  function H() { return svg.clientHeight || svg.parentElement.clientHeight; }
  function laneH() { return (H() - M.top - M.bottom) / Math.max(activeRepos.length, 1); }
  function xOf(t) { const w = W() - M.left - M.right;
    return M.left + (t - data.ts_min) / (data.ts_max - data.ts_min) * w; }
  function tsOf(x) { const w = W() - M.left - M.right;
    let f = (x - M.left) / w; f = Math.max(0, Math.min(1, f));
    return Math.round(data.ts_min + f * (data.ts_max - data.ts_min)); }

  function setData(d) { data = d; if (d.epoch_colors) EPOCH_COLORS = d.epoch_colors; }
  let EPOCH_COLORS = {};

  function setRepos(repos) { activeRepos = repos.slice(); render(); }
  function setPlayhead(t) { ts = t; renderPlayhead(); }
  function getTs() { return ts; }

  // sorted unique epoch boundaries across active repos
  function boundaries() {
    const set = new Set([data.ts_min, data.ts_max]);
    (data.epochs || []).forEach(e => {
      if (activeRepos.includes(e.repo)) {
        if (e.start_ts) set.add(e.start_ts);
        if (e.end_ts && e.end_ts < data.ts_max) set.add(e.end_ts);
      }
    });
    return [...set].sort((a, b) => a - b);
  }
  function stepEpoch(dir) {
    const b = boundaries();
    if (dir > 0) { const nxt = b.find(x => x > ts + 1); onSeek(nxt ?? data.ts_max); }
    else { const prev = [...b].reverse().find(x => x < ts - 1); onSeek(prev ?? data.ts_min); }
  }

  function render() {
    if (!data) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const lh = laneH();

    // lanes + labels + epoch bands
    activeRepos.forEach((repo, i) => {
      const y = M.top + i * lh;
      // lane background
      svg.appendChild(el('rect', { x: M.left, y: y + 3, width: W() - M.left - M.right,
        height: lh - 6, fill: 'var(--bg-elev2)', opacity: 0.35, rx: 4 }));
      // label
      const lbl = el('text', { x: 10, y: y + lh / 2 + 4, class: 'tl-lane-label', fill: repoColors[repo] || '#8891a0' });
      lbl.textContent = repo;
      svg.appendChild(lbl);
      // parent lineage marker
      const parent = (data.repos.find(r => r.name === repo) || {}).lineage_parent;
      if (parent) {
        const pm = el('text', { x: 10, y: y + lh / 2 + 16, class: 'tl-axis-label' });
        pm.textContent = '↳ from ' + parent;
        svg.appendChild(pm);
      }
      // epoch bands
      (data.epochs || []).filter(e => e.repo === repo && e.start_ts).forEach(e => {
        const x1 = xOf(e.start_ts), x2 = xOf(e.end_ts || data.ts_max);
        const w = Math.max(1.2, x2 - x1);
        const rect = el('rect', { x: x1, y: y + 5, width: w, height: lh - 10, rx: 2,
          fill: EPOCH_COLORS[e.epoch_type] || '#888', opacity: 0.62, class: 'tl-epoch' });
        rect.addEventListener('click', (ev) => { ev.stopPropagation(); onSeek(e.start_ts + 1); });
        const tip = el('title'); tip.textContent =
          `${repo} · ${e.epoch_type}${e.git_tag ? ' · ' + e.git_tag : ''}\n${e.label}`;
        rect.appendChild(tip);
        svg.appendChild(rect);
      });
    });

    // year axis
    const y0 = new Date(data.ts_min * 1000).getFullYear();
    const y1 = new Date(data.ts_max * 1000).getFullYear();
    for (let yr = y0; yr <= y1; yr++) {
      const t = Date.UTC(yr, 0, 1) / 1000;
      if (t < data.ts_min || t > data.ts_max) continue;
      const x = xOf(t);
      svg.appendChild(el('line', { x1: x, y1: M.top, x2: x, y2: H() - M.bottom,
        stroke: 'var(--border)', 'stroke-width': 0.6, opacity: 0.5 }));
      const tx = el('text', { x: x + 3, y: H() - 8, class: 'tl-axis-label' });
      tx.textContent = yr; svg.appendChild(tx);
    }

    renderPlayhead();
  }

  function renderPlayhead() {
    if (!data) return;
    let ph = svg.querySelector('.tl-playhead-group');
    if (ph) ph.remove();
    const g = el('g', { class: 'tl-playhead-group' });
    const x = xOf(ts);
    g.appendChild(el('line', { x1: x, y1: M.top - 2, x2: x, y2: H() - M.bottom,
      stroke: 'var(--accent)', 'stroke-width': 2, class: 'tl-playhead' }));
    g.appendChild(el('polygon', { points: `${x - 6},${M.top - 8} ${x + 6},${M.top - 8} ${x},${M.top}`,
      fill: 'var(--accent)' }));
    svg.appendChild(g);
  }

  return { init, setData, setRepos, setPlayhead, getTs, stepEpoch, boundaries, render };
})();
