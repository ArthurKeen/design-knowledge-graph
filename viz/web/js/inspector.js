// inspector.js — provenance panel + source viewer ("jump to exact location").
const Inspector = (() => {
  let repoColors = {}, onRelation = () => {}, lastPv = null;

  function init(opts) {
    repoColors = opts.repoColors || {};
    onRelation = opts.onRelation || onRelation;
    document.getElementById('srcClose').addEventListener('click', closeSource);
    document.getElementById('sourceOverlay').addEventListener('click', (e) => {
      if (e.target.id === 'sourceOverlay') closeSource();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSource(); });
  }

  const esc = (s) => (s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  const fmtDate = (ts) => (!ts || ts >= 9999999999) ? '—' :
    new Date(ts * 1000).toISOString().slice(0, 10);

  // wrap spans (sorted, may overlap) with <mark>; mark the first hit specially
  function highlight(text, spans) {
    if (!spans || !spans.length) return esc(text);
    const merged = [];
    spans.slice().sort((a, b) => a.start - b.start).forEach(s => {
      const last = merged[merged.length - 1];
      if (last && s.start <= last.end) last.end = Math.max(last.end, s.end);
      else merged.push({ start: s.start, end: s.end });
    });
    let out = '', cur = 0;
    merged.forEach((s, i) => {
      out += esc(text.slice(cur, s.start));
      out += `<mark${i === 0 ? ' class="first"' : ''}>` + esc(text.slice(s.start, s.end)) + '</mark>';
      cur = s.end;
    });
    out += esc(text.slice(cur));
    return out;
  }

  function snippet(text, spans, term) {
    let idx = spans && spans.length ? spans[0].start : (text.toLowerCase().indexOf((term || '').toLowerCase()));
    if (idx < 0) idx = 0;
    const a = Math.max(0, idx - 70), b = Math.min(text.length, idx + 150);
    const pre = a > 0 ? '…' : '', post = b < text.length ? '…' : '';
    const localSpans = (spans || []).filter(s => s.start >= a && s.end <= b)
      .map(s => ({ start: s.start - a, end: s.end - a }));
    return pre + highlight(text.slice(a, b), localSpans) + post;
  }

  function render(pv) {
    lastPv = pv;
    const body = document.getElementById('inspectorBody');
    const rc = repoColors[pv.repo] || '#4f8cff';
    const t = pv.temporal || {};
    let html = `
      <div class="insp-head">
        <button class="icon-btn insp-close" onclick="Inspector.close()">✕</button>
        <div class="insp-title">${esc(pv.label || pv.id)}</div>
        <div class="insp-badges">
          <span class="badge repo" style="background:${rc}">${esc(pv.repo)}</span>
          ${pv.entity_type ? `<span class="badge type">${esc(String(pv.entity_type).toLowerCase())}</span>` : ''}
          ${pv.temporal ? (t.still_current ? '<span class="badge" style="color:var(--good)">current</span>'
                                           : '<span class="badge">retired</span>') : ''}
          ${t.epoch ? `<span class="badge type">${esc(t.epoch)}</span>` : ''}
        </div>
      </div>`;

    // Description (golden entities)
    if (pv.description) {
      html += `<div class="insp-section" style="font-size:12px;color:var(--text-dim);line-height:1.5">${esc(pv.description)}</div>`;
    }

    // Temporal (module versions only — golden entities are atemporal)
    if (pv.temporal) {
      html += `<div class="insp-section"><h4>⏱ Temporal</h4>`;
      if (t.file) html += kv('file', t.file);
      html += kv('valid from', fmtDate(t.valid_from_ts));
      html += kv('valid to', t.valid_to_ts >= 9999999999 ? 'present' : fmtDate(t.valid_to_ts));
      if (t.introduced_by) {
        const ib = t.introduced_by;
        html += kv('introduced', `${ib.sha || ''} · ${esc(ib.author || '')}`);
        if (ib.message) html += `<div class="kv"><span class="k"></span><span class="v" style="color:var(--text-dim)">“${esc((ib.message || '').slice(0, 120))}”</span></div>`;
      }
      html += `</div>`;
    }

    // Cross-repo lineage
    if (pv.cross_repo && pv.cross_repo.length) {
      html += `<div class="insp-section"><h4>⇄ Cross-project lineage <span class="count">${pv.cross_repo.length}</span></h4>`;
      pv.cross_repo.forEach(x => {
        const ev = x.type.endsWith('EVOLVED_FROM');
        html += `<div class="xrepo-row">
          <span class="badge repo" style="background:${repoColors[x.repo] || '#888'}">${esc(x.repo)}</span>
          <span style="font-family:var(--mono)">${esc(x.label)}</span>
          <span class="lin ${ev ? 'evolved' : 'similar'}">${ev ? 'evolved' : 'similar'}${x.score ? ' ' + Number(x.score).toFixed(2) : ''}</span>
        </div>`;
      });
      html += `</div>`;
    }

    // Text sources (provenance to exact location)
    if (pv.text && pv.text.length) {
      html += `<div class="insp-section"><h4>📄 Source text <span class="count">${pv.text.length} chunk(s)</span></h4>`;
      pv.text.forEach(c => {
        html += `<div class="prov-card">
          <div class="pc-head">
            <span class="pc-title">${esc(c.doc_title || c.chunk_id)}</span>
            <span class="term">${esc(c.matched_term || '')}</span>
          </div>
          <div class="snippet">${snippet(c.text, c.spans, c.matched_term)}</div>
          <button class="jump-btn" onclick='Inspector.jumpChunk(${JSON.stringify(c.chunk_id)}, ${JSON.stringify(c.matched_term || '')})'>↪ open in document</button>
        </div>`;
      });
      html += `</div>`;
    }

    // Verilog
    if (pv.verilog && pv.verilog.file) {
      const hlTerms = pv.verilog.terms && pv.verilog.terms.length ? pv.verilog.terms : [pv.label];
      html += `<div class="insp-section"><h4>⌗ Verilog</h4>`;
      html += kv('file', pv.verilog.file);
      html += `<button class="jump-btn" onclick='Inspector.jumpVerilog()'>↪ open source (highlight “${esc(hlTerms[0])}”)</button>`;
      html += `</div>`;
    }

    // Structure
    const st = pv.structure || {};
    if ((st.ports && st.ports.length) || (st.signals && st.signals.length) || (st.fsms && st.fsms.length)) {
      html += `<div class="insp-section"><h4>▤ Structure</h4>`;
      if (st.ports && st.ports.length) {
        html += `<div class="kv"><span class="k">ports ${st.ports.length}</span></div><div class="chips">`;
        st.ports.slice(0, 24).forEach(p => html += `<span class="chip" title="${esc(p.expanded_name || '')}">${esc(p.label)}${p.direction ? ' <span class="dir-' + (p.direction === 'input' ? 'in' : 'out') + '">' + p.direction[0] + '</span>' : ''}</span>`);
        html += `</div>`;
      }
      if (st.signals && st.signals.length) {
        html += `<div class="kv" style="margin-top:8px"><span class="k">signals ${st.signals.length}</span></div><div class="chips">`;
        st.signals.slice(0, 24).forEach(s => html += `<span class="chip" title="${esc(s.expanded_name || '')}">${esc(s.label)}</span>`);
        html += `</div>`;
      }
      if (st.fsms && st.fsms.length) {
        html += `<div class="kv" style="margin-top:8px"><span class="k">FSMs</span></div><div class="chips">`;
        st.fsms.forEach(f => html += `<span class="chip">${esc(f.label)}</span>`);
        html += `</div>`;
      }
      html += `</div>`;
    }

    // Relations with consolidated evidence
    if (pv.relations && pv.relations.length) {
      html += `<div class="insp-section"><h4>🔗 Relations <span class="count">${pv.relations.length}</span></h4>`;
      pv.relations.forEach(r => {
        const nv = r.text_evidence ? r.text_evidence.length : 0;
        html += `<div class="rel-row" title="${esc(r.context || r.type || '')}" onclick='Inspector.rel(${JSON.stringify(r.other_id)})'>
          <span class="arrow">${r.direction === 'out' ? '→' : '←'}</span>
          <span>${esc(r.other_label)}${r.type && r.type !== 'DEPENDS_ON' ? ` <span style="color:var(--text-faint);font-size:10px">${esc(String(r.type).toLowerCase())}</span>` : ''}</span>
          <span class="ev">
            ${r.verilog_evidence ? '<span class="ev-badge v">verilog</span>' : ''}
            ${nv ? `<span class="ev-badge t">${nv} doc</span>` : ''}
          </span>
        </div>`;
      });
      html += `</div>`;
    }

    body.innerHTML = html;
    document.querySelector('main').classList.add('inspector-open');
    document.getElementById('inspector').classList.remove('collapsed');
  }

  function kv(k, v) { return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`; }

  function close() {
    document.querySelector('main').classList.remove('inspector-open');
    document.getElementById('inspector').classList.add('collapsed');
    Graph.clearSelection();
  }

  // ── source viewer ──
  async function jumpChunk(chunkId, term) {
    const s = await API.source('chunk', chunkId, term ? [term] : []);
    showSource('document', s);
  }
  async function jumpVerilog() {
    // ref + highlight terms come from the provenance payload (snapshot: module
    // key == label; live: deep structural key like OR1200_or1200_cpu)
    const v = (lastPv || {}).verilog;
    if (!v) return;
    const ref = v.ref || lastPv.label;
    const terms = v.terms && v.terms.length ? v.terms : [lastPv.label];
    const s = await API.source('verilog', ref, terms);
    showSource('verilog', s);
  }
  function showSource(kind, s) {
    if (s.error) { alert(s.error); return; }
    document.getElementById('srcKind').textContent = kind;
    document.getElementById('srcTitle').textContent = s.title || '';
    const body = document.getElementById('srcBody');
    body.innerHTML = highlight(s.text || '', s.spans || []);
    document.getElementById('sourceOverlay').classList.remove('hidden');
    requestAnimationFrame(() => {
      const first = body.querySelector('mark.first') || body.querySelector('mark');
      if (first) first.scrollIntoView({ block: 'center' });
    });
  }
  function closeSource() { document.getElementById('sourceOverlay').classList.add('hidden'); }
  function rel(id) { onRelation(id); }

  return { init, render, close, jumpChunk, jumpVerilog, rel };
})();
