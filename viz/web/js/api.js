// api.js — thin fetch wrapper for the ChronoGraph backend
const API = {
  async _get(path, params) {
    const url = new URL(path, window.location.origin);
    if (params) Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, v);
    });
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${path} → ${r.status} ${await r.text().catch(() => '')}`);
    return r.json();
  },
  health()          { return this._get('/api/health'); },
  repos()           { return this._get('/api/repos'); },
  timeline()        { return this._get('/api/timeline'); },
  slice(ts, repos, projection) {
    return this._get('/api/slice', { ts, repos: repos.join(','), projection });
  },
  provenance(id)    { return this._get('/api/provenance', { id }); },
  source(kind, ref, terms) {
    return this._get('/api/source', { kind, ref, terms: (terms || []).join('|') });
  },
  search(q, repos)  { return this._get('/api/search', { q, repos: (repos || []).join(',') }); },
};
