/* ============================================================================
   Electoral Roll — Command Center  (vanilla JS SPA, no build step)
   Sections:
     1. State + storage
     2. API fetch helper + toasts
     3. Small DOM/component helpers (el, badge, photoImg, spinner, statCard...)
     4. Auth gate
     5. Shell (rail + top bar) + hash router
     6. Views: Overview, Suspects, Review, Reviewed, Explore, Ingest, Enrich, Reports
   ========================================================================== */

'use strict';

/* ------------------------------------------------------------------ *
 * 1. Global state + persisted preferences
 * ------------------------------------------------------------------ */
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

const state = {
  config: null,          // /api/config
  me: null,              // /api/me
  years: [],             // /api/years
  year: LS.get('er_year', ''),
  reviewer: LS.get('er_reviewer', ''),
  dbOffline: false,      // set true when a 503 is seen
  rules: null,           // cached /api/rules
};

const APP = document.getElementById('app');
const $toasts = document.getElementById('toasts');

/* ------------------------------------------------------------------ *
 * 2. Fetch helper + toasts
 * ------------------------------------------------------------------ */
function toast(msg, kind = 'info', title = '') {
  const t = el('div', { class: `toast ${kind}` });
  if (title) t.appendChild(el('div', { class: 'tt' }, title));
  t.appendChild(el('div', {}, msg));
  $toasts.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 300); }, kind === 'error' ? 6000 : 3800);
}

class ApiError extends Error {
  constructor(status, detail) { super(detail || ('HTTP ' + status)); this.status = status; this.detail = detail; }
}

/**
 * Core fetch helper.
 *  - sends cookies (credentials: same-origin)
 *  - JSON-encodes plain-object bodies (FormData passes through)
 *  - 401 -> show login gate; 503 -> flag DB offline; other !ok -> throw ApiError
 *  - `raw:true` returns the Response (for blobs / downloads)
 */
async function api(path, opts = {}) {
  const o = { credentials: 'same-origin', headers: {}, ...opts };
  if (o.body && !(o.body instanceof FormData) && typeof o.body === 'object') {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  let res;
  try {
    res = await fetch(path, o);
  } catch (e) {
    throw new ApiError(0, 'Network error — the server is unreachable.');
  }

  if (res.status === 401) {
    state.me = { authenticated: false, user: null };
    renderLogin('Your session expired. Please sign in again.');
    throw new ApiError(401, 'Not authenticated');
  }

  let detail = '';
  if (res.status === 503) {
    state.dbOffline = true;
    try { detail = (await res.json()).detail; } catch {}
    updateDbBanner(detail || 'Database unreachable');
    throw new ApiError(503, detail || 'Database unreachable');
  }

  if (!res.ok) {
    try { detail = (await res.json()).detail; } catch {}
    throw new ApiError(res.status, detail || ('Request failed (' + res.status + ')'));
  }

  // success clears any previous DB-offline flag
  if (state.dbOffline) { state.dbOffline = false; updateDbBanner(null); }
  if (opts.raw) return res;
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

/** Surface an error to the user via toast, unless it was already handled (401/503). */
function showErr(e, prefix = 'Error') {
  if (e instanceof ApiError && (e.status === 401 || e.status === 503)) return;
  toast(e.detail || e.message || String(e), 'error', prefix);
}

/* ------------------------------------------------------------------ *
 * 3. DOM + component helpers
 * ------------------------------------------------------------------ */
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k in n && k !== 'list') { try { n[k] = v; } catch { n.setAttribute(k, v); } }
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.appendChild(typeof kid === 'string' || typeof kid === 'number' ? document.createTextNode(String(kid)) : kid);
  }
  return n;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
function fmtNum(n) { return (n == null || n === '') ? '—' : Number(n).toLocaleString('en-IN'); }
function sevClass(s) { s = (s || '').toLowerCase(); return ['high', 'medium', 'low'].includes(s) ? s : 'neutral'; }
function esc(s) { return (s == null ? '' : String(s)); }

function badge(text, kind) { return el('span', { class: `badge ${kind || 'neutral'}` }, text); }
function sevBadge(s) { return el('span', { class: `badge ${sevClass(s)}` }, (s || 'n/a').toUpperCase()); }

/** Photo <img> pointing at /api/photo/{id}; falls back to a placeholder box on error. */
function photoImg(voterId, size = 56, kind = 'photo') {
  const src = kind === 'photo' ? `/api/photo/${voterId}` : voterId; // kind!=photo => raw url
  const img = el('img', {
    class: 'photo', width: size, height: size, alt: '', loading: 'lazy',
    src: (kind === 'photo' && voterId == null) ? '' : src,
    style: `width:${size}px;height:${size}px`,
  });
  const swap = () => {
    const ph = el('span', { class: 'photo ph', style: `width:${size}px;height:${size}px`, title: 'No photo' }, '👤');
    if (img.parentNode) img.parentNode.replaceChild(ph, img);
  };
  if (kind === 'photo' && voterId == null) { const ph = el('span', { class: 'photo ph', style: `width:${size}px;height:${size}px` }, '👤'); return ph; }
  img.addEventListener('error', swap);
  return img;
}

function spinner(big) { return el('span', { class: 'spinner' + (big ? ' lg' : '') }); }
function loadingRow(text = 'Loading…') { return el('div', { class: 'loading-row' }, spinner(), text); }
function emptyState(icon, title, msg, actionBtn) {
  return el('div', { class: 'empty' },
    el('div', { class: 'big' }, icon),
    el('h3', {}, title),
    msg ? el('p', {}, msg) : null,
    actionBtn || null);
}
function statCard(label, value, kind, foot) {
  return el('div', { class: `stat ${kind || ''}` },
    el('div', { class: 'k' }, label),
    el('div', { class: 'v mono' }, value == null ? '—' : (typeof value === 'number' ? fmtNum(value) : value)),
    foot ? el('div', { class: 'foot' }, foot) : null);
}
function field(labelText, control) {
  return el('label', { class: 'field' }, el('span', { class: 'lbl' }, labelText), control);
}
function selectEl(options, value, onChange, attrs = {}) {
  const s = el('select', attrs);
  for (const o of options) {
    const opt = typeof o === 'object' ? o : { value: o, label: o };
    s.appendChild(el('option', { value: opt.value }, opt.label));
  }
  if (value != null) s.value = value;
  if (onChange) s.addEventListener('change', onChange);
  return s;
}
/** Trigger a browser download / open of a URL (reports, CSV, ZIP). */
function download(url) { window.open(url, '_blank'); }

/* map explore/voter row fields robustly (backend column names) */
function pick(obj, keys, dflt = '') { for (const k of keys) if (obj && obj[k] != null && obj[k] !== '') return obj[k]; return dflt; }

/* ------------------------------------------------------------------ *
 * 4. Auth gate
 * ------------------------------------------------------------------ */
function renderLogin(errMsg) {
  const appName = (state.config && state.config.app_name) || 'Electoral Roll';
  const cfgMsg = state.config && state.config.auth_configured === false ? state.config.message : '';
  clear(APP);
  const err = el('div', { class: 'login-err', style: 'display:none' });
  if (errMsg) { err.textContent = errMsg; err.style.display = 'block'; }

  const uname = el('input', { type: 'text', placeholder: 'Username', autocomplete: 'username', required: true, autofocus: true });
  const pass = el('input', { type: 'password', placeholder: 'Password', autocomplete: 'current-password', required: true });
  const btn = el('button', { class: 'btn primary block', type: 'submit' }, 'Sign in');

  const form = el('form', {
    onsubmit: async (e) => {
      e.preventDefault();
      err.style.display = 'none';
      btn.disabled = true; clear(btn).appendChild(spinner()); btn.appendChild(document.createTextNode(' Signing in…'));
      try {
        await api('/api/login', { method: 'POST', body: { username: uname.value, password: pass.value } });
        location.reload();
      } catch (ex) {
        err.textContent = ex.detail || 'Sign-in failed.'; err.style.display = 'block';
        btn.disabled = false; clear(btn).appendChild(document.createTextNode('Sign in'));
      }
    }
  },
    field('Username', uname), field('Password', pass), err, btn);

  APP.appendChild(el('div', { class: 'login-wrap' },
    el('div', { class: 'login-card' },
      el('div', { class: 'brand' }, el('span', { class: 'mark' }, '🛰️'), el('h1', {}, appName)),
      el('div', { class: 'sub' }, 'Fraud detection command center'),
      form,
      cfgMsg ? el('div', { class: 'login-note' }, cfgMsg) : null,
    )));
}

/* ------------------------------------------------------------------ *
 * 5. Shell + router
 * ------------------------------------------------------------------ */
const NAV = [
  { hash: '#/overview', label: 'Overview', ico: '⌂' },
  { hash: '#/suspects', label: 'Suspects', ico: '⚑' },
  { hash: '#/combined', label: 'Combined Model', ico: '🧬' },
  { hash: '#/review', label: 'Review Queue', ico: '☑' },
  { hash: '#/explore', label: 'Explore', ico: '⌕' },
  { hash: '#/reviewed', label: 'Reviewed', ico: '✓' },
  { hash: '#/ingest', label: 'Ingest', ico: '⬆' },
  { hash: '#/enrich', label: 'Enrichment', ico: '✦' },
  { hash: '#/reports', label: 'Reports', ico: '⎙' },
];

let dbBannerEl = null;
function updateDbBanner(msg) {
  const host = document.getElementById('db-banner-host');
  const pill = document.getElementById('db-pill');
  if (msg) {
    if (pill) { pill.className = 'status-pill bad'; clear(pill).append(el('span', { class: 'dot' }), 'DB offline'); pill.title = msg; }
    if (host) {
      clear(host);
      host.appendChild(el('div', { class: 'db-banner' }, el('span', {}, '⚠'), el('span', {}, 'Database unreachable — ' + msg + '. Some data cannot load.')));
    }
  } else {
    if (host) clear(host);
  }
}

function buildShell() {
  clear(APP);
  const appName = (state.config && state.config.app_name) || 'Electoral Roll';

  // ---- left rail ----
  const nav = el('nav', {});
  NAV.forEach(item => nav.appendChild(el('a', { href: item.hash, id: 'nav-' + item.hash.slice(2) },
    el('span', { class: 'ico' }, item.ico), el('span', {}, item.label))));
  const rail = el('aside', { class: 'rail', id: 'rail' },
    el('div', { class: 'brand' }, el('span', { class: 'mark' }, '🛰️'),
      el('div', { class: 'name' }, appName, el('small', {}, 'Command Center'))),
    nav,
    el('div', { class: 'rail-foot' }, state.me && state.me.user ? ('Signed in · ' + state.me.user) : ''));

  // ---- top bar ----
  const search = el('input', {
    type: 'search', placeholder: 'Search voters, or paste an EPIC…', 'aria-label': 'Global search',
    onkeydown: (e) => { if (e.key === 'Enter') globalSearch(e.target.value.trim()); }
  });
  const yearSel = selectEl(
    state.years.length ? state.years.map(y => ({ value: y, label: String(y) })) : [{ value: '', label: '—' }],
    state.year || (state.years[0] != null ? state.years[0] : ''),
    (e) => { state.year = e.target.value; LS.set('er_year', state.year); route(); },
    { class: 'year', id: 'year-select', 'aria-label': 'Active year' });
  if (!state.year && state.years[0] != null) { state.year = String(state.years[0]); LS.set('er_year', state.year); }

  const pill = el('span', { class: 'status-pill', id: 'db-pill', title: 'Checking…' }, el('span', { class: 'dot' }), 'Checking…');
  const signout = el('button', {
    class: 'btn sm ghost',
    onclick: async () => { try { await api('/api/logout', { method: 'POST' }); } catch {} location.reload(); }
  }, 'Sign out');

  const topbar = el('header', { class: 'topbar' },
    el('button', { class: 'btn sm ghost', style: 'display:none', id: 'rail-toggle', onclick: () => document.getElementById('rail').classList.toggle('open') }, '☰'),
    el('div', { class: 'search' }, el('span', { class: 'ico' }, '⌕'), search),
    el('div', { class: 'spacer' }),
    el('div', { class: 'yearsel' }, el('span', { class: 'lbl' }, 'Year'), yearSel),
    pill, signout);

  const main = el('main', { class: 'main' }, topbar,
    el('div', { class: 'content' },
      el('div', { id: 'db-banner-host' }),
      el('div', { id: 'view' })));

  APP.appendChild(el('div', { class: 'shell' }, rail, main));

  // show rail toggle on narrow screens
  if (window.matchMedia('(max-width: 720px)').matches) document.getElementById('rail-toggle').style.display = '';

  refreshHealth();
  route();
}

function setActiveNav() {
  const cur = (location.hash || '#/overview').split('?')[0];
  document.querySelectorAll('.rail nav a').forEach(a => a.classList.toggle('active', a.getAttribute('href') === cur));
}

async function refreshHealth() {
  try {
    const h = await api('/api/health');
    const pill = document.getElementById('db-pill');
    if (!pill) return;
    if (h.db_ready) { pill.className = 'status-pill ok'; clear(pill).append(el('span', { class: 'dot' }), 'DB online'); pill.title = h.message || 'Database reachable'; state.dbOffline = false; updateDbBanner(null); }
    else { pill.className = 'status-pill bad'; clear(pill).append(el('span', { class: 'dot' }), 'DB offline'); pill.title = h.message || 'offline'; updateDbBanner(h.message || 'not ready'); }
  } catch (e) { /* handled by api() for 503/401 */ }
}

function globalSearch(q) {
  if (!q) return;
  // EPIC heuristic: 3 letters + 7 digits (typical) OR alnum >=10 with letters+digits
  const epicish = /^[A-Za-z]{2,3}[0-9]{6,8}$/.test(q) || (/^[A-Za-z0-9]{9,}$/.test(q) && /[A-Za-z]/.test(q) && /[0-9]/.test(q));
  if (epicish) { location.hash = '#/explore?epic=' + encodeURIComponent(q.toUpperCase()); }
  else { location.hash = '#/explore?q=' + encodeURIComponent(q); }
}

/* ---- Hash router ---- */
function parseHash() {
  const raw = location.hash || '#/overview';
  const [path, qs] = raw.slice(1).split('?');
  const params = new URLSearchParams(qs || '');
  return { path: path || '/overview', params };
}
const ROUTES = {
  '/overview': viewOverview,
  '/suspects': viewSuspects,
  '/combined': viewCombined,
  '/review': viewReview,
  '/explore': viewExplore,
  '/reviewed': viewReviewed,
  '/ingest': viewIngest,
  '/enrich': viewEnrich,
  '/reports': viewReports,
};
function route() {
  const view = document.getElementById('view');
  if (!view) return;
  setActiveNav();
  const { path, params } = parseHash();
  const fn = ROUTES[path] || viewOverview;
  clear(view);
  try { fn(view, params); } catch (e) { console.error(e); showErr(e, 'Render error'); }
  window.scrollTo(0, 0);
}

function viewHead(title, sub, actions) {
  return el('div', { class: 'view-head' },
    el('div', {}, el('h2', {}, title), sub ? el('div', { class: 'sub' }, sub) : null),
    actions ? el('div', { class: 'actions' }, ...actions) : null);
}
function requireYear(view) {
  if (state.year) return true;
  view.appendChild(emptyState('📅', 'No year selected', 'No electoral-roll years are available yet. Ingest a PDF first to create data.'));
  return false;
}

/* ============================================================================
   6a. OVERVIEW
   ========================================================================== */
async function viewOverview(view) {
  view.appendChild(viewHead('Overview', 'Command center · year ' + (state.year || '—'), [
    el('button', { class: 'btn', onclick: () => runRulesQuick() }, '▶ Run rules'),
    el('button', { class: 'btn', onclick: () => buildSuspectsQuick() }, '⚑ Build suspects'),
    el('button', { class: 'btn', onclick: () => { location.hash = '#/review'; } }, '☑ Review queue'),
    el('button', { class: 'btn primary', onclick: () => { location.hash = '#/suspects'; } }, 'Go to suspects →'),
  ]));
  if (!requireYear(view)) return;

  const body = el('div', {});
  view.appendChild(body);
  body.appendChild(loadingRow('Loading overview…'));

  let d;
  try { d = await api('/api/overview?year=' + encodeURIComponent(state.year)); }
  catch (e) { clear(body); body.appendChild(emptyState('⚠', 'Could not load overview', e.detail || e.message)); return; }
  clear(body);

  const sev = d.by_severity || {}; const rev = d.reviewed || {};
  // stat tiles
  body.appendChild(el('div', { class: 'grid cols-4' },
    statCard('Voters scanned', d.voters_total, 'accent'),
    statCard('Total flags', d.flags_total, 'neutral'),
    statCard('High severity', sev.high || 0, 'high'),
    statCard('Medium severity', sev.medium || 0, 'medium'),
  ));
  body.appendChild(el('div', { class: 'grid cols-4 mt' },
    statCard('Low severity', sev.low || 0, 'low'),
    statCard('Open', rev.open || 0, 'medium', 'awaiting review'),
    statCard('Reviewed', (rev.confirmed || 0) + (rev.legitimate || 0) + (rev.needs_info || 0), 'low',
      `${rev.confirmed || 0} confirmed · ${rev.legitimate || 0} cleared`),
    el('div', { class: 'stat accent', style: 'display:flex;flex-direction:column;justify-content:center' },
      el('div', { class: 'k' }, 'Suspect clusters'),
      el('button', { class: 'btn primary', style: 'margin-top:8px', onclick: () => { location.hash = '#/suspects'; } }, 'Build / view suspects')),
  ));

  // flags by rule + top constituencies
  const twin = el('div', { class: 'grid cols-2 mt' });
  body.appendChild(twin);

  // Flags by rule
  const rulePanel = el('div', { class: 'panel' }, el('div', { class: 'panel-head' }, el('h3', {}, 'Flags by rule')));
  const rb = el('div', { class: 'panel-body' });
  rulePanel.appendChild(rb);
  const byRule = (d.by_rule || []).slice().sort((a, b) => (b.flags || 0) - (a.flags || 0));
  if (!byRule.length) rb.appendChild(el('div', { class: 'dim small' }, 'No flags yet. Run rules to populate.'));
  else {
    const max = Math.max(...byRule.map(r => r.flags || 0), 1);
    const rank = el('div', { class: 'rank' });
    byRule.forEach(r => {
      rank.appendChild(el('div', { class: 'row' },
        el('div', { class: 'lab' }, el('span', { class: 'dot ' + sevClass(r.severity) }), el('span', { class: 't mono' }, r.rule)),
        el('div', { class: 'n' }, fmtNum(r.flags))));
      rank.appendChild(el('div', { class: 'bar ' + (r.severity === 'high' ? 'high' : '') }, el('span', { style: `width:${Math.round((r.flags || 0) / max * 100)}%` })));
    });
    rb.appendChild(rank);
  }
  twin.appendChild(rulePanel);

  // Top constituencies
  const cPanel = el('div', { class: 'panel' }, el('div', { class: 'panel-head' }, el('h3', {}, 'Top constituencies')));
  const cb = el('div', { class: 'panel-body' });
  cPanel.appendChild(cb);
  const tc = d.top_constituencies || [];
  if (!tc.length) cb.appendChild(el('div', { class: 'dim small' }, 'No constituency flags yet.'));
  else {
    const max = Math.max(...tc.map(c => c.flags || 0), 1);
    const rank = el('div', { class: 'rank' });
    tc.forEach(c => {
      rank.appendChild(el('div', { class: 'row' },
        el('div', { class: 'lab' }, el('span', { class: 't' }, `${esc(c.constituency_name) || 'AC'} `, el('span', { class: 'mono dim' }, '#' + esc(c.constituency_no)))),
        el('div', { class: 'n' }, fmtNum(c.flags))));
      rank.appendChild(el('div', { class: 'bar' }, el('span', { style: `width:${Math.round((c.flags || 0) / max * 100)}%` })));
    });
    cb.appendChild(rank);
  }
  twin.appendChild(cPanel);
}

async function runRulesQuick() {
  if (!state.year) return toast('Select a year first', 'warn');
  toast('Running all rules for ' + state.year + '…', 'info');
  try {
    const r = await api('/api/rules/run', { method: 'POST', body: { year: Number(state.year), rules: null } });
    const total = Object.values(r.added || {}).reduce((a, b) => a + b, 0);
    toast(`Rules complete — ${fmtNum(total)} flags added.`, 'success', 'Done');
    if (parseHash().path === '/overview') route();
  } catch (e) { showErr(e, 'Run rules'); }
}
async function buildSuspectsQuick() {
  if (!state.year) return toast('Select a year first', 'warn');
  toast('Building suspect clusters for ' + state.year + '… this can take a while.', 'info');
  try {
    const r = await api('/api/suspects/build', { method: 'POST', body: { year: Number(state.year) } });
    toast(`Suspects built — ${fmtNum(r.total)} records.`, 'success', 'Done');
    location.hash = '#/suspects';
  } catch (e) { showErr(e, 'Build suspects'); }
}

/* ============================================================================
   6b. SUSPECTS — the centerpiece cluster view
   ========================================================================== */
const suspectsState = { severity: 'all', signal: 'all', min_matches: '', ac: '', q: '', limit: 20, offset: 0 };

async function viewSuspects(view) {
  view.appendChild(viewHead('Suspects', 'One voter · all their similar/duplicate matches', [
    el('button', { class: 'btn', onclick: () => rebuildSuspects(view) }, '↻ Rebuild'),
  ]));
  if (!requireYear(view)) return;

  const host = el('div', {});
  view.appendChild(host);
  host.appendChild(loadingRow('Checking suspect cache…'));

  let summary;
  try { summary = await api('/api/suspects/summary?year=' + encodeURIComponent(state.year)); }
  catch (e) { clear(host); host.appendChild(emptyState('⚠', 'Could not load', e.detail || e.message)); return; }

  clear(host);
  if (!summary.built) {
    host.appendChild(emptyState('⚑', 'No suspect clusters built yet',
      'Build the combined similarity model for ' + state.year + ' to surface voters that have multiple similar matches.',
      el('button', { class: 'btn primary', onclick: () => rebuildSuspects(view) }, '⚑ Build suspects')));
    return;
  }

  // summary strip
  const s = summary.summary || {};
  host.appendChild(el('div', { class: 'grid cols-4 mb' },
    statCard('Records', s.total, 'accent', 'built ' + (summary.built_at ? new Date(summary.built_at).toLocaleString() : '')),
    statCard('High', s.high || 0, 'high'),
    statCard('Medium', s.medium || 0, 'medium'),
    statCard('Low', s.low || 0, 'low'),
  ));

  // filter bar
  const acOptions = [{ value: '', label: 'All ACs' }].concat((summary.constituencies || []).map(a => ({ value: a, label: String(a) })));
  const sevSel = selectEl([{ value: 'all', label: 'All' }, 'high', 'medium', 'low'], suspectsState.severity, null, {});
  const sigSel = selectEl([{ value: 'all', label: 'All' }, 'cosine', 'fuzzy', 'logical', 'nomap'], suspectsState.signal, null, {});
  const minInput = el('input', { type: 'number', min: '0', placeholder: 'e.g. 4', value: suspectsState.min_matches, style: 'width:100px' });
  const acSel = selectEl(acOptions, suspectsState.ac, null, {});
  const qInput = el('input', { type: 'search', placeholder: 'name / epic / constituency', value: suspectsState.q });

  const apply = () => {
    suspectsState.severity = sevSel.value; suspectsState.signal = sigSel.value;
    suspectsState.min_matches = minInput.value; suspectsState.ac = acSel.value; suspectsState.q = qInput.value;
    suspectsState.offset = 0;
    loadSuspects(listHost, pagerHost);
  };
  [sevSel, sigSel, acSel].forEach(x => x.addEventListener('change', apply));
  qInput.addEventListener('keydown', e => { if (e.key === 'Enter') apply(); });
  minInput.addEventListener('keydown', e => { if (e.key === 'Enter') apply(); });

  host.appendChild(el('div', { class: 'filterbar' },
    field('Severity', sevSel),
    field('Signal', sigSel),
    field('Min similar matches', minInput),
    field('Constituency', acSel),
    el('label', { class: 'field grow' }, el('span', { class: 'lbl' }, 'Search'), qInput),
    el('button', { class: 'btn primary', onclick: apply }, 'Apply'),
  ));

  const listHost = el('div', { class: 'suspect-list' });
  const pagerHost = el('div', {});
  host.appendChild(listHost);
  host.appendChild(pagerHost);
  loadSuspects(listHost, pagerHost);
}

async function rebuildSuspects(view) {
  if (!state.year) return;
  toast('Building suspects for ' + state.year + '…', 'info');
  const btnHost = view.querySelector('.view-head .actions');
  try {
    await api('/api/suspects/build', { method: 'POST', body: { year: Number(state.year) } });
    toast('Suspects built.', 'success');
    route();
  } catch (e) { showErr(e, 'Build suspects'); }
}

async function loadSuspects(listHost, pagerHost) {
  clear(listHost); clear(pagerHost);
  for (let i = 0; i < 3; i++) listHost.appendChild(el('div', { class: 'panel skel skel-card' }));
  const p = new URLSearchParams({ year: state.year, limit: suspectsState.limit, offset: suspectsState.offset });
  if (suspectsState.severity !== 'all') p.set('severity', suspectsState.severity);
  if (suspectsState.signal !== 'all') p.set('signal', suspectsState.signal);
  if (suspectsState.min_matches) p.set('min_matches', suspectsState.min_matches);
  if (suspectsState.ac) p.set('ac', suspectsState.ac);
  if (suspectsState.q) p.set('q', suspectsState.q);

  let d;
  try { d = await api('/api/suspects?' + p.toString()); }
  catch (e) {
    clear(listHost);
    if (e.status === 409) { listHost.appendChild(emptyState('⚑', 'Not built', 'Build suspects for this year first.', el('button', { class: 'btn primary', onclick: () => buildSuspectsQuick() }, 'Build now'))); return; }
    listHost.appendChild(emptyState('⚠', 'Could not load suspects', e.detail || e.message)); return;
  }
  clear(listHost);
  if (!d.records || !d.records.length) {
    listHost.appendChild(emptyState('🔍', 'No matching suspects', 'Try lowering "Min similar matches" or clearing filters.'));
    return;
  }
  d.records.forEach(r => listHost.appendChild(suspectCard(r)));

  // pager
  const from = d.offset + 1, to = d.offset + d.records.length;
  pagerHost.appendChild(el('div', { class: 'pager' },
    el('button', { class: 'btn sm', disabled: d.offset <= 0, onclick: () => { suspectsState.offset = Math.max(0, d.offset - suspectsState.limit); loadSuspects(listHost, pagerHost); } }, '← Prev'),
    el('span', { class: 'info' }, `${fmtNum(from)}–${fmtNum(to)} of ${fmtNum(d.total)}`),
    el('button', { class: 'btn sm', disabled: to >= d.total, onclick: () => { suspectsState.offset = d.offset + suspectsState.limit; loadSuspects(listHost, pagerHost); } }, 'Next →'),
  ));
}

function suspectCard(r) {
  const matches = [].concat(r.cosine || [], r.fuzzy || []);
  const n = matches.length;

  // primary voter block
  const primary = el('div', { class: 'suspect-primary' },
    photoImg(r.voter_id, 68),
    el('div', { class: 'id' },
      el('div', { class: 'nm' }, esc(r.name) || esc(r.roll_name) || 'Unknown'),
      el('div', { class: 'meta' },
        el('span', { class: 'mono' }, esc(r.epic_no) || '—'),
        el('span', {}, `AC ${esc(r.constituency_no)} · Part ${esc(r.part_no)} · Sl ${esc(r.serial_no)}`),
        el('span', {}, `${r.age != null ? r.age + ' yrs' : '—'} · ${esc(r.gender) || '—'}`)),
      el('div', { class: 'tags' }, sevBadge(r.severity), r.tier_label ? badge(r.tier_label, 'accent') : null, r.no_mapping ? badge('no-map', 'neutral') : null),
      r.signals_summary ? el('div', { class: 'sig' }, r.signals_summary) : null),
  );

  // match strip
  const strip = el('div', { class: 'match-strip' });
  const compareHost = el('div', {});  // where an expanded comparison table renders
  matches.forEach((m, i) => strip.appendChild(matchTile(m, compareHost)));

  const matchesBlock = el('div', { class: 'matches' },
    el('div', { class: 'mh' },
      el('span', { class: 'badge accent' }, `${n} similar voter${n === 1 ? '' : 's'}`),
      el('span', { class: 'cnt' }, `best ${r.best_dup != null ? Number(r.best_dup).toFixed(3) : '—'}`)),
    n ? strip : el('div', { class: 'dim small' }, 'No cosine/fuzzy matches on this record.'),
  );

  return el('div', { class: 'suspect-card ' + sevClass(r.severity) },
    el('div', { class: 'suspect-top' }, primary, matchesBlock),
    compareHost);
}

function matchTile(m, compareHost) {
  const model = (m.model || '').toLowerCase();
  const scoreVal = m.metric != null ? m.metric : m.score;
  const tile = el('div', { class: 'match-tile' },
    el('div', { class: 'row' },
      photoImg(m.partner_id, 46),
      el('div', { style: 'min-width:0' },
        el('div', { class: 'nm' }, esc(m.partner_name) || 'Unknown'),
        el('div', { class: 'ep' }, esc(m.partner_epic) || '—'))),
    el('div', { class: 'foot' },
      el('span', { class: 'model-tag ' + (model === 'cosine' ? 'cosine' : 'fuzzy') }, model || 'match'),
      el('span', { class: 'score' }, scoreVal != null ? Number(scoreVal).toFixed(3) : '—')),
  );
  tile.addEventListener('click', () => {
    const isOpen = tile.classList.contains('open');
    // close siblings
    tile.parentNode.querySelectorAll('.match-tile.open').forEach(t => t.classList.remove('open'));
    clear(compareHost);
    if (isOpen) return;
    tile.classList.add('open');
    compareHost.appendChild(comparePanel(m));
  });
  return tile;
}

function comparePanel(m) {
  const wrap = el('div', { class: 'compare' });
  if (m.reason) wrap.appendChild(el('div', { class: 'reason' }, '💬 ', m.reason));
  const rows = m.comparison || [];
  if (!rows.length) { wrap.appendChild(el('div', { class: 'dim small' }, 'No per-attribute comparison available.')); return wrap; }
  const table = el('table', { class: 'cmp' },
    el('thead', {}, el('tr', {}, el('th', {}, 'Attribute'), el('th', {}, 'Suspect'), el('th', {}, 'Match'), el('th', {}, 'Status'), el('th', {}, 'Similarity'))));
  const tb = el('tbody', {});
  rows.forEach(c => {
    const st = (c.status || '').toLowerCase();
    tb.appendChild(el('tr', {},
      el('td', { class: 'dim' }, esc(c.attribute)),
      el('td', {}, esc(c.a)),
      el('td', {}, esc(c.b)),
      el('td', { class: 'st st-' + (st || 'none') }, esc(c.status) || '—'),
      el('td', { class: 'mono' }, c.similarity != null ? Number(c.similarity).toFixed(3) : '—')));
  });
  table.appendChild(tb);
  wrap.appendChild(el('div', { class: 'table-wrap' }, table));
  return wrap;
}

/* ============================================================================
   6c. REVIEW QUEUE
   ========================================================================== */
const reviewState = { rule: '', limit: 20, offset: 0 };

async function viewReview(view) {
  view.appendChild(viewHead('Review Queue', 'Adjudicate open fraud flags', []));
  if (!requireYear(view)) return;

  // controls: reviewer, rule filter, run rules, clear
  const rules = await loadRules();
  const ruleOpts = [{ value: '', label: 'All rules' }].concat(rules.map(r => ({ value: r.id, label: `${r.id} (${r.severity})` })));

  const reviewerInput = el('input', { type: 'text', placeholder: 'your name', value: state.reviewer, style: 'width:150px',
    onchange: (e) => { state.reviewer = e.target.value.trim(); LS.set('er_reviewer', state.reviewer); } });
  const ruleSel = selectEl(ruleOpts, reviewState.rule, (e) => { reviewState.rule = e.target.value; reviewState.offset = 0; loadFlags(listHost, pagerHost); });

  // run rules multiselect
  const runSel = el('select', { multiple: true, size: Math.min(6, Math.max(3, rules.length)), style: 'min-width:220px' });
  rules.forEach(r => runSel.appendChild(el('option', { value: r.id }, `${r.id} · ${r.severity}`)));

  view.appendChild(el('div', { class: 'filterbar' },
    field('Reviewer', reviewerInput),
    field('Filter rule', ruleSel),
    el('div', { style: 'flex:1' }),
    el('button', { class: 'btn danger', onclick: () => clearFlags() }, '🗑 Clear flags'),
  ));

  view.appendChild(el('details', { class: 'panel', style: 'margin-bottom:16px' },
    el('summary', { style: 'cursor:pointer;padding:12px 16px;font-weight:600' }, '▶ Run rules (select which, or none for all)'),
    el('div', { class: 'panel-body wrap-flex' },
      field('Rules', runSel),
      el('button', { class: 'btn primary', onclick: async () => {
        const sel = Array.from(runSel.selectedOptions).map(o => o.value);
        toast('Running ' + (sel.length ? sel.length + ' rule(s)' : 'all rules') + '…', 'info');
        try {
          const r = await api('/api/rules/run', { method: 'POST', body: { year: Number(state.year), rules: sel.length ? sel : null } });
          const total = Object.values(r.added || {}).reduce((a, b) => a + b, 0);
          toast(`Added ${fmtNum(total)} flags.`, 'success', 'Rules complete');
          reviewState.offset = 0; loadFlags(listHost, pagerHost);
        } catch (e) { showErr(e, 'Run rules'); }
      } }, 'Run selected'))));

  const listHost = el('div', {});
  const pagerHost = el('div', {});
  view.appendChild(listHost);
  view.appendChild(pagerHost);
  loadFlags(listHost, pagerHost);
}

async function loadRules() {
  if (state.rules) return state.rules;
  try { const d = await api('/api/rules'); state.rules = d.rules || []; }
  catch { state.rules = []; }
  return state.rules;
}

async function clearFlags() {
  if (!confirm('Clear ALL flags for year ' + state.year + '? This cannot be undone.')) return;
  try { await api('/api/flags/clear', { method: 'POST', body: { year: Number(state.year) } }); toast('Flags cleared.', 'success'); route(); }
  catch (e) { showErr(e, 'Clear flags'); }
}

async function loadFlags(listHost, pagerHost) {
  clear(listHost); clear(pagerHost);
  listHost.appendChild(loadingRow('Loading flags…'));
  const p = new URLSearchParams({ year: state.year, limit: reviewState.limit, offset: reviewState.offset });
  if (reviewState.rule) p.set('rule', reviewState.rule);
  let d;
  try { d = await api('/api/flags?' + p.toString()); }
  catch (e) { clear(listHost); listHost.appendChild(emptyState('⚠', 'Could not load flags', e.detail || e.message)); return; }
  clear(listHost);
  if (!d.flags || !d.flags.length) {
    listHost.appendChild(emptyState('✓', 'No open flags', 'Nothing to review here. Run rules to generate flags, or change the filter.'));
    return;
  }
  d.flags.forEach(f => listHost.appendChild(flagCard(f, listHost)));

  pagerHost.appendChild(el('div', { class: 'pager' },
    el('button', { class: 'btn sm', disabled: reviewState.offset <= 0, onclick: () => { reviewState.offset = Math.max(0, reviewState.offset - reviewState.limit); loadFlags(listHost, pagerHost); } }, '← Prev'),
    el('span', { class: 'info' }, `showing ${fmtNum(d.flags.length)} · offset ${fmtNum(reviewState.offset)}`),
    el('button', { class: 'btn sm', disabled: !d.has_more, onclick: () => { reviewState.offset += reviewState.limit; loadFlags(listHost, pagerHost); } }, 'Next →'),
  ));
}

function voterMini(name, epic, extra) {
  return el('div', { class: 'voter-mini' },
    photoImg(extra.voter_id, 54),
    el('div', { style: 'min-width:0' },
      el('div', { class: 'nm' }, esc(name) || 'Unknown'),
      el('div', { class: 'kv' }, el('span', { class: 'mono' }, esc(epic) || '—')),
      el('div', { class: 'kv' }, `AC ${esc(extra.const_)} · Part ${esc(extra.part)} · Sl ${esc(extra.serial)}`),
      el('div', { class: 'kv' }, `${extra.age != null ? extra.age + ' yrs' : '—'} · ${esc(extra.gender) || '—'} · House ${esc(extra.house) || '—'}`)));
}

function flagCard(f, listHost) {
  const card = el('div', { class: 'flag-card ' + sevClass(f.severity) });
  const isHouse = f.details && f.details.house_norm != null && f.related_voter_id == null;

  const head = el('div', { class: 'flag-head' },
    el('span', { class: 'dot ' + sevClass(f.severity) }),
    el('span', { class: 'rule' }, esc(f.rule)),
    sevBadge(f.severity),
    f.score != null ? badge('score ' + Number(f.score).toFixed(3), 'neutral') : null,
    el('span', { class: 'spacer' }),
    el('span', { class: 'small dim mono' }, '#' + esc(f.id)));
  card.appendChild(head);

  // voter pair (or single for house overload)
  const pair = el('div', { class: 'flag-pair' });
  pair.appendChild(voterMini(f.name_a, f.epic_a, { voter_id: f.voter_id, const_: f.const_a, part: f.part_a, serial: f.serial_a, age: f.age_a, gender: f.gender_a, house: f.house_a }));
  if (f.related_voter_id != null || f.name_b) {
    pair.appendChild(voterMini(f.name_b, f.epic_b, { voter_id: f.related_voter_id, const_: f.const_b, part: f.part_b, serial: f.serial_b, age: f.age_b, gender: f.gender_b, house: f.house_b }));
  }
  card.appendChild(pair);

  // house expand
  if (isHouse) {
    const houseHost = el('div', { style: 'padding:0 14px 14px' });
    card.appendChild(el('div', { style: 'padding:0 14px 14px' },
      el('button', { class: 'btn sm', onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = 'Loading…';
        try {
          const p = new URLSearchParams({ cn: f.details.constituency_no, house_norm: f.details.house_norm, year: state.year });
          const d = await api('/api/house?' + p.toString());
          clear(houseHost).appendChild(membersTable(d.members || []));
          e.target.style.display = 'none';
        } catch (ex) { showErr(ex, 'Expand house'); e.target.disabled = false; e.target.textContent = '🏠 Expand house'; }
      } }, '🏠 Expand house'), houseHost));
  }

  // details JSON collapsible
  if (f.details && Object.keys(f.details).length) {
    card.appendChild(el('details', { class: 'json-toggle' },
      el('summary', { style: 'cursor:pointer;color:var(--text-3);font-size:12px' }, 'details JSON'),
      el('pre', { class: 'json' }, JSON.stringify(f.details, null, 2))));
  }

  // review actions
  const notes = el('input', { class: 'notes', type: 'text', placeholder: 'notes (optional)' });
  const doReview = async (verdict, btn) => {
    if (!state.reviewer) return toast('Enter a reviewer name first (top of page).', 'warn');
    btn.disabled = true;
    try {
      await api(`/api/flags/${f.id}/review`, { method: 'POST', body: { verdict, reviewer: state.reviewer, notes: notes.value || undefined } });
      toast(`Flag #${f.id} → ${verdict}.`, 'success');
      card.style.transition = 'opacity .3s'; card.style.opacity = '0'; setTimeout(() => card.remove(), 300);
    } catch (e) { showErr(e, 'Review'); btn.disabled = false; }
  };
  card.appendChild(el('div', { class: 'flag-actions' },
    el('button', { class: 'btn danger sm', onclick: (e) => doReview('confirmed', e.currentTarget) }, '⚑ Confirmed'),
    el('button', { class: 'btn good sm', onclick: (e) => doReview('legitimate', e.currentTarget) }, '✓ Legitimate'),
    el('button', { class: 'btn warn sm', onclick: (e) => doReview('needs_info', e.currentTarget) }, '? Needs-info'),
    notes));
  return card;
}

function membersTable(rows) {
  if (!rows.length) return el('div', { class: 'dim small' }, 'No members returned.');
  const cols = Object.keys(rows[0]);
  const table = el('table', { class: 'data' }, el('thead', {}, el('tr', {}, ...cols.map(c => el('th', {}, c)))));
  const tb = el('tbody', {});
  rows.forEach(r => tb.appendChild(el('tr', {}, ...cols.map(c => el('td', { class: 'mono' }, esc(r[c]))))));
  table.appendChild(tb);
  return el('div', { class: 'table-wrap' }, table);
}

/* ============================================================================
   6d. REVIEWED HISTORY
   ========================================================================== */
const reviewedState = { verdict: '', rule: '', limit: 50 };
async function viewReviewed(view) {
  view.appendChild(viewHead('Reviewed', 'History of adjudicated flags', []));
  if (!requireYear(view)) return;
  const rules = await loadRules();
  const verdictSel = selectEl([{ value: '', label: 'All verdicts' }, { value: 'confirmed', label: 'Confirmed' }, { value: 'legitimate', label: 'Legitimate' }, { value: 'needs_info', label: 'Needs-info' }],
    reviewedState.verdict, (e) => { reviewedState.verdict = e.target.value; load(); });
  const ruleSel = selectEl([{ value: '', label: 'All rules' }].concat(rules.map(r => ({ value: r.id, label: r.id }))),
    reviewedState.rule, (e) => { reviewedState.rule = e.target.value; load(); });
  view.appendChild(el('div', { class: 'filterbar' }, field('Verdict', verdictSel), field('Rule', ruleSel)));
  const host = el('div', {});
  view.appendChild(host);
  async function load() {
    clear(host); host.appendChild(loadingRow());
    const p = new URLSearchParams({ year: state.year, limit: reviewedState.limit });
    if (reviewedState.verdict) p.set('verdict', reviewedState.verdict);
    if (reviewedState.rule) p.set('rule', reviewedState.rule);
    let d;
    try { d = await api('/api/reviewed?' + p.toString()); }
    catch (e) { clear(host); host.appendChild(emptyState('⚠', 'Could not load', e.detail || e.message)); return; }
    clear(host);
    const sm = d.summary || {};
    host.appendChild(el('div', { class: 'grid cols-3 mb' },
      statCard('Confirmed', sm.confirmed || 0, 'high'),
      statCard('Legitimate', sm.legitimate || 0, 'low'),
      statCard('Needs-info', sm.needs_info || 0, 'medium')));
    const flags = d.flags || [];
    if (!flags.length) { host.appendChild(emptyState('✓', 'Nothing reviewed yet', 'Adjudicated flags will appear here.')); return; }
    const cols = ['id', 'rule', 'severity', 'verdict', 'reviewer', 'notes', 'name_a', 'epic_a', 'name_b', 'epic_b', 'reviewed_at'];
    const table = el('table', { class: 'data' }, el('thead', {}, el('tr', {}, ...cols.map(c => el('th', {}, c)))));
    const tb = el('tbody', {});
    flags.forEach(f => {
      const tr = el('tr', {});
      cols.forEach(c => {
        let v = f[c];
        if (c === 'severity') return tr.appendChild(el('td', {}, sevBadge(v)));
        if (c === 'verdict') return tr.appendChild(el('td', {}, badge(v, v === 'confirmed' ? 'high' : v === 'legitimate' ? 'low' : 'medium')));
        tr.appendChild(el('td', { class: ['id', 'epic_a', 'epic_b'].includes(c) ? 'mono' : '' }, esc(v)));
      });
      // reopen action
      tr.appendChild(el('td', {}, el('button', { class: 'btn sm ghost', onclick: async () => {
        try { await api(`/api/flags/${f.id}/reopen`, { method: 'POST' }); toast('Flag #' + f.id + ' reopened.', 'success'); load(); } catch (e) { showErr(e, 'Reopen'); }
      } }, 'Reopen')));
      tb.appendChild(tr);
    });
    table.querySelector('thead tr').appendChild(el('th', {}, ''));
    table.appendChild(tb);
    host.appendChild(el('div', { class: 'table-wrap' }, table));
  }
  load();
}

/* ============================================================================
   6e. EXPLORE
   ========================================================================== */
const exploreState = { page: 1, sort: '', filters: {}, q: '' };

async function viewExplore(view, params) {
  view.appendChild(viewHead('Explore', 'Search & filter the full voter roll', [
    el('button', { class: 'btn', id: 'csv-btn', onclick: () => exportCsv() }, '⬇ Download CSV'),
  ]));
  if (!requireYear(view)) return;

  const host = el('div', {});
  view.appendChild(host);
  host.appendChild(loadingRow('Loading filter options…'));

  let opts;
  try { opts = await api('/api/explore/options?year=' + encodeURIComponent(state.year)); }
  catch (e) { clear(host); host.appendChild(emptyState('⚠', 'Could not load', e.detail || e.message)); return; }
  clear(host);

  const fo = opts.options || {};
  // filter controls
  const acSel = el('select', { multiple: true, size: 5 });
  (fo.acs || []).forEach(a => acSel.appendChild(el('option', { value: a }, String(a))));
  const partSel = el('select', { multiple: true, size: 5 });
  const genderSel = selectEl([{ value: '', label: 'Any' }].concat((fo.genders || []).map(g => ({ value: g, label: g }))), '', null);
  const relSel = selectEl([{ value: '', label: 'Any' }].concat((fo.relation_types || []).map(g => ({ value: g, label: g }))), '', null);
  const catSel = selectEl([{ value: '', label: 'Any' }].concat((fo.category_types || []).map(g => ({ value: g, label: g }))), '', null);
  const statusSel = selectEl([{ value: '', label: 'Any' }].concat((opts.statuses || []).map(g => ({ value: g, label: g }))), '', null);
  const sortSel = selectEl((opts.sorts || []).map(s => ({ value: s, label: s })), exploreState.sort || (opts.sorts || [])[0] || '', null);
  const ageMin = el('input', { type: 'number', placeholder: fo.age_min != null ? String(fo.age_min) : 'min', style: 'width:80px' });
  const ageMax = el('input', { type: 'number', placeholder: fo.age_max != null ? String(fo.age_max) : 'max', style: 'width:80px' });
  const hasMobile = el('input', { type: 'checkbox' });
  const hasPhoto = el('input', { type: 'checkbox' });
  const qInput = el('input', { type: 'search', placeholder: 'name / epic / relation…', value: params.get('q') || '' });

  // refresh parts when ACs change
  const refreshParts = async () => {
    const acs = Array.from(acSel.selectedOptions).map(o => o.value);
    clear(partSel);
    if (!acs.length) return;
    try {
      const pp = new URLSearchParams({ year: state.year });
      acs.forEach(a => pp.append('ac', a));
      const d = await api('/api/explore/parts?' + pp.toString());
      (d.parts || []).forEach(p => partSel.appendChild(el('option', { value: p }, String(p))));
    } catch (e) { /* ignore */ }
  };
  acSel.addEventListener('change', refreshParts);

  const buildParams = (forExport) => {
    const p = new URLSearchParams({ year: state.year });
    Array.from(acSel.selectedOptions).forEach(o => p.append('ac', o.value));
    Array.from(partSel.selectedOptions).forEach(o => p.append('part', o.value));
    if (genderSel.value) p.set('gender', genderSel.value);
    if (relSel.value) p.set('relation_type', relSel.value);
    if (catSel.value) p.set('category_type', catSel.value);
    if (statusSel.value) p.set('status', statusSel.value);
    if (ageMin.value) p.set('age_min', ageMin.value);
    if (ageMax.value) p.set('age_max', ageMax.value);
    if (hasMobile.checked) p.set('has_mobile', 'true');
    if (hasPhoto.checked) p.set('has_photo', 'true');
    if (qInput.value.trim()) p.set('q', qInput.value.trim());
    if (sortSel.value) p.set('sort', sortSel.value);
    if (!forExport) p.set('page', exploreState.page);
    return p;
  };
  exploreState._buildParams = buildParams;

  const side = el('div', { class: 'panel pad filter-side' },
    el('h3', { style: 'margin:0 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-2)' }, 'Filters'),
    field('Constituency (multi)', acSel),
    field('Part (multi)', partSel),
    field('Gender', genderSel),
    field('Relation type', relSel),
    field('Category', catSel),
    field('Status', statusSel),
    el('div', { class: 'wrap-flex' }, field('Age min', ageMin), field('Age max', ageMax)),
    el('label', { class: 'check' }, hasMobile, 'Has mobile'),
    el('label', { class: 'check' }, hasPhoto, 'Has photo'),
    field('Sort', sortSel),
    el('label', { class: 'field' }, el('span', { class: 'lbl' }, 'Search'), qInput),
    el('button', { class: 'btn primary block', onclick: () => { exploreState.page = 1; runSearch(); } }, 'Search'),
  );

  const resultHost = el('div', {});
  host.appendChild(el('div', { class: 'explore-grid' }, side, resultHost));

  async function runSearch() {
    clear(resultHost); resultHost.appendChild(loadingRow('Searching…'));
    let d;
    try { d = await api('/api/explore?' + buildParams().toString()); }
    catch (e) { clear(resultHost); resultHost.appendChild(emptyState('⚠', 'Search failed', e.detail || e.message)); return; }
    clear(resultHost);
    const rows = d.rows || [];
    if (!rows.length) { resultHost.appendChild(emptyState('🔍', 'No voters match', 'Adjust the filters and search again.')); return; }
    resultHost.appendChild(el('div', { class: 'small dim mb' }, `${fmtNum(d.total)} result${d.total === 1 ? '' : 's'} · page ${d.page}`));

    const cols = ['photo', 'name', 'epic_no', 'constituency_no', 'part_no', 'serial_no', 'age', 'gender', 'relation_name'];
    const table = el('table', { class: 'data' }, el('thead', {}, el('tr', {}, ...cols.map(c => el('th', {}, c === 'photo' ? '' : c)))));
    const tb = el('tbody', {});
    rows.forEach(r => {
      const vid = pick(r, ['voter_id', 'id']);
      const tr = el('tr', { class: 'clickable', onclick: () => openVoterDrawer(vid, r) },
        el('td', {}, photoImg(vid, 38)),
        el('td', {}, esc(pick(r, ['name', 'roll_name']))),
        el('td', { class: 'mono' }, esc(pick(r, ['epic_no', 'epic']))),
        el('td', { class: 'mono' }, esc(pick(r, ['constituency_no']))),
        el('td', { class: 'mono' }, esc(pick(r, ['part_no']))),
        el('td', { class: 'mono' }, esc(pick(r, ['serial_no']))),
        el('td', {}, esc(r.age)),
        el('td', {}, esc(r.gender)),
        el('td', {}, esc(pick(r, ['relation_name']))));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    resultHost.appendChild(el('div', { class: 'table-wrap' }, table));

    const pageSize = d.page_size || rows.length;
    const totalPages = Math.max(1, Math.ceil((d.total || 0) / pageSize));
    resultHost.appendChild(el('div', { class: 'pager' },
      el('button', { class: 'btn sm', disabled: d.page <= 1, onclick: () => { exploreState.page = d.page - 1; runSearch(); } }, '← Prev'),
      el('span', { class: 'info' }, `page ${d.page} / ${totalPages}`),
      el('button', { class: 'btn sm', disabled: d.page >= totalPages, onclick: () => { exploreState.page = d.page + 1; runSearch(); } }, 'Next →')));
  }
  exploreState._runSearch = runSearch;

  // preload from global search params
  const epic = params.get('epic');
  if (epic) { openPersonByEpic(epic); }
  if (params.get('q') || epic == null) runSearch();
}

function exportCsv() {
  if (!exploreState._buildParams) return;
  download('/api/explore/export.csv?' + exploreState._buildParams(true).toString());
}

/* Voter detail drawer: full fields + person (EPIC) rows + docs + flags */
async function openVoterDrawer(voterId, rowHint) {
  const scrim = el('div', { class: 'drawer-scrim', onclick: closeDrawer });
  const body = el('div', { class: 'db' }, loadingRow('Loading voter…'));
  const drawer = el('aside', { class: 'drawer', role: 'dialog', 'aria-label': 'Voter detail' },
    el('div', { class: 'dh' }, el('h3', {}, 'Voter detail'), el('button', { class: 'btn sm ghost', onclick: closeDrawer }, '✕ Close')),
    body);
  function closeDrawer() { scrim.remove(); drawer.remove(); document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') closeDrawer(); }
  document.addEventListener('keydown', onKey);
  document.body.append(scrim, drawer);

  let voter, epic;
  try {
    const d = await api('/api/voter/' + encodeURIComponent(voterId));
    voter = d.voter || {};
    epic = pick(voter, ['epic_no', 'epic']);
  } catch (e) { clear(body); body.appendChild(emptyState('⚠', 'Could not load voter', e.detail || e.message)); return; }

  clear(body);
  body.appendChild(el('div', { class: 'row-flex mb' }, photoImg(voterId, 84),
    el('div', {}, el('div', { style: 'font-weight:700;font-size:16px' }, esc(pick(voter, ['name', 'roll_name']))),
      el('div', { class: 'mono muted' }, esc(epic) || '—'))));

  // full fields
  const dl = el('dl', { class: 'kv-grid' });
  Object.entries(voter).forEach(([k, v]) => {
    if (v == null || v === '') return;
    dl.appendChild(el('dt', {}, k));
    dl.appendChild(el('dd', { class: /epic|serial|no$|_no|id/.test(k) ? 'mono' : '' }, typeof v === 'object' ? JSON.stringify(v) : String(v)));
  });
  body.appendChild(el('div', { class: 'panel pad mb' }, el('h3', { style: 'margin:0 0 10px;font-size:12px;text-transform:uppercase;color:var(--text-3)' }, 'All fields'), dl));

  // person profile (rows across years, docs, flags)
  if (epic) {
    const pHost = el('div', {});
    body.appendChild(pHost);
    pHost.appendChild(loadingRow('Loading person profile…'));
    try {
      const person = await api('/api/person/' + encodeURIComponent(epic));
      clear(pHost);
      renderPersonProfile(pHost, person);
    } catch (e) { clear(pHost); pHost.appendChild(el('div', { class: 'dim small' }, 'Person profile unavailable: ' + (e.detail || e.message))); }
  }
}

function renderPersonProfile(host, person) {
  const rows = person.rows || [];
  host.appendChild(el('div', { class: 'panel', style: 'margin-bottom:14px' },
    el('div', { class: 'panel-head' }, el('h3', {}, `Rows sharing EPIC (${rows.length})`)),
    el('div', { class: 'panel-body' }, rows.length ? membersTable(rows) : el('div', { class: 'dim small' }, 'Only this row.'))));

  // documents
  const docs = person.documents || [];
  if (docs.length) {
    const imgs = el('div', { class: 'doc-imgs' });
    docs.forEach(dc => {
      const url = `/api/epic-doc/${encodeURIComponent(person.epic_no)}/${encodeURIComponent(dc.doc_type)}`;
      imgs.appendChild(el('div', {}, photoImg(url, 120, 'url'), el('div', { class: 'small dim', style: 'text-align:center;margin-top:4px' }, dc.doc_type)));
    });
    host.appendChild(el('div', { class: 'panel', style: 'margin-bottom:14px' },
      el('div', { class: 'panel-head' }, el('h3', {}, 'Documents')),
      el('div', { class: 'panel-body' }, imgs)));
  }

  // flags on person
  const flags = person.flags || [];
  host.appendChild(el('div', { class: 'panel' },
    el('div', { class: 'panel-head' }, el('h3', {}, `Flags on this person (${flags.length})`)),
    el('div', { class: 'panel-body' }, flags.length ? membersTable(flags) : el('div', { class: 'dim small' }, 'No flags recorded.'))));
}

async function openPersonByEpic(epic) {
  // Open a drawer that shows the person profile directly (from global EPIC search)
  const scrim = el('div', { class: 'drawer-scrim', onclick: close });
  const body = el('div', { class: 'db' }, loadingRow('Looking up EPIC…'));
  const drawer = el('aside', { class: 'drawer', role: 'dialog' },
    el('div', { class: 'dh' }, el('h3', {}, 'EPIC ' + epic), el('button', { class: 'btn sm ghost', onclick: close }, '✕ Close')), body);
  function close() { scrim.remove(); drawer.remove(); }
  document.body.append(scrim, drawer);
  try {
    const person = await api('/api/person/' + encodeURIComponent(epic));
    clear(body);
    if (!person.rows || !person.rows.length) { body.appendChild(emptyState('🔍', 'No voter found', 'No rows share EPIC ' + epic + '.')); return; }
    renderPersonProfile(body, person);
  } catch (e) { clear(body); body.appendChild(emptyState('⚠', 'Lookup failed', e.detail || e.message)); }
}

/* ============================================================================
   6f. INGEST
   ========================================================================== */
async function viewIngest(view) {
  view.appendChild(viewHead('Ingest', 'Extract voter records from a roll PDF, then load into the database', []));
  let meta = {};
  try { meta = await api('/api/ingest/meta'); } catch (e) { /* non-fatal */ }

  const fileState = { file: null };
  const methodSel = selectEl((meta.methods || ['regex', 'llm']).map(m => ({ value: m, label: m })), 'regex', null);
  const includePhotos = el('input', { type: 'checkbox', checked: true });
  const trim = el('input', { type: 'checkbox' });
  const dropFirst = el('input', { type: 'number', min: '0', value: '0', style: 'width:70px' });
  const dropLast = el('input', { type: 'number', min: '0', value: '0', style: 'width:70px' });

  const dz = el('div', { class: 'dropzone', tabindex: '0' },
    el('div', { class: 'big' }, '⬆'),
    el('div', {}, 'Drag & drop a PDF here, or click to choose'),
    el('div', { class: 'fname' }));
  const fileInput = el('input', { type: 'file', accept: 'application/pdf,.pdf', style: 'display:none' });
  const setFile = (f) => { fileState.file = f; dz.querySelector('.fname').textContent = f ? f.name : ''; };
  dz.addEventListener('click', () => fileInput.click());
  dz.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') fileInput.click(); });
  fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
  ['dragover', 'dragenter'].forEach(ev => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag'); }));
  dz.addEventListener('drop', (e) => { const f = e.dataTransfer.files[0]; if (f) setFile(f); });

  const resultHost = el('div', { class: 'mt' });
  const extractBtn = el('button', { class: 'btn primary', onclick: () => doExtract() }, 'Extract');

  view.appendChild(el('div', { class: 'panel pad' },
    dz, fileInput,
    el('div', { class: 'wrap-flex mt' },
      field('Method', methodSel),
      el('label', { class: 'check' }, includePhotos, 'Include photos'),
      el('label', { class: 'check' }, trim, 'Trim pages'),
      field('Drop first', dropFirst),
      field('Drop last', dropLast),
      extractBtn),
    meta.ocr_provider ? el('div', { class: 'small dim mt' }, `OCR provider: ${meta.ocr_provider} · Mistral key ${meta.mistral_key_set ? 'set' : 'not set'} · batch ${meta.batch_threshold || '?'}`) : null,
  ));
  view.appendChild(resultHost);

  async function doExtract() {
    if (!fileState.file) return toast('Choose a PDF first.', 'warn');
    extractBtn.disabled = true; clear(extractBtn).append(spinner(), ' Extracting…');
    clear(resultHost).appendChild(el('div', { class: 'panel pad' }, el('div', { class: 'row-flex' }, spinner(), 'Extracting records — this can be slow for large rolls…'), el('div', { class: 'progress mt' }, el('span', {}))));
    const fd = new FormData();
    fd.append('file', fileState.file);
    fd.append('method', methodSel.value);
    fd.append('include_photos', includePhotos.checked);
    fd.append('trim', trim.checked);
    fd.append('drop_first', dropFirst.value || '0');
    fd.append('drop_last', dropLast.value || '0');
    let d;
    try { d = await api('/api/ingest/extract', { method: 'POST', body: fd }); }
    catch (e) { clear(resultHost).appendChild(emptyState('⚠', 'Extraction failed', e.detail || e.message)); extractBtn.disabled = false; clear(extractBtn).append('Extract'); return; }
    extractBtn.disabled = false; clear(extractBtn).append('Extract');
    renderExtractResult(resultHost, d);
  }
}

function renderExtractResult(host, d) {
  clear(host);
  const iss = d.issues || {};
  const missing = (iss.missing_serials || []).length;
  const incomplete = (iss.incomplete_rows || []).length;

  host.appendChild(el('div', { class: 'grid cols-4 mb' },
    statCard('Rows', d.rows, 'accent'),
    statCard('Photos', d.n_photos, 'low'),
    statCard('Missing serials', missing, missing ? 'high' : 'low'),
    statCard('Incomplete rows', incomplete, incomplete ? 'medium' : 'low'),
  ));

  host.appendChild(el('div', { class: 'integrity mb' },
    el('span', { class: 'chip' }, `serials ${esc(iss.min_serial)}–${esc(iss.expected_max_serial)}`),
    missing ? el('span', { class: 'chip warn' }, `${missing} missing`) : el('span', { class: 'chip ok' }, 'no gaps'),
    incomplete ? el('span', { class: 'chip warn' }, `${incomplete} incomplete`) : el('span', { class: 'chip ok' }, 'rows complete'),
    d.year_guess ? el('span', { class: 'chip' }, 'year guess ' + d.year_guess) : null,
  ));

  // actions: download zip + ingest to db
  const yearInput = el('input', { type: 'number', value: d.year_guess || state.year || '', style: 'width:110px' });
  host.appendChild(el('div', { class: 'panel pad mb wrap-flex' },
    el('a', { class: 'btn', href: '/api/ingest/download/' + encodeURIComponent(d.token), target: '_blank' }, '⬇ Download ZIP'),
    field('Ingest year', yearInput),
    el('button', { class: 'btn primary', onclick: async (e) => {
      const yr = Number(yearInput.value);
      if (!yr) return toast('Enter a year.', 'warn');
      e.currentTarget.disabled = true; clear(e.currentTarget).append(spinner(), ' Ingesting…');
      try {
        const r = await api('/api/ingest/to_db', { method: 'POST', body: { token: d.token, year: yr } });
        toast(`Ingested ${fmtNum(r.voters)} voters, ${fmtNum(r.photos)} photos for ${r.year}.`, 'success', 'Ingest complete');
        // refresh years
        try { const yy = await api('/api/years'); state.years = yy.years || []; const sel = document.getElementById('year-select'); if (sel) { clear(sel); state.years.forEach(y => sel.appendChild(el('option', { value: y }, String(y)))); sel.value = String(yr); state.year = String(yr); LS.set('er_year', state.year); } } catch {}
      } catch (ex) { showErr(ex, 'Ingest'); }
      e.currentTarget.disabled = false; clear(e.currentTarget).append('Ingest to database');
    } }, 'Ingest to database'),
  ));

  // preview table
  const preview = d.preview || [];
  if (preview.length) {
    const cols = d.columns && d.columns.length ? d.columns : Object.keys(preview[0]);
    const table = el('table', { class: 'data' }, el('thead', {}, el('tr', {}, ...cols.map(c => el('th', {}, c)))));
    const tb = el('tbody', {});
    preview.forEach(r => tb.appendChild(el('tr', {}, ...cols.map(c => el('td', {}, esc(r[c]))))));
    table.appendChild(tb);
    host.appendChild(el('div', { class: 'panel' }, el('div', { class: 'panel-head' }, el('h3', {}, `Preview (${preview.length} of ${fmtNum(d.rows)})`)), el('div', { style: 'padding:2px' }, el('div', { class: 'table-wrap' }, table))));
  }
}

/* ============================================================================
   6g. ENRICHMENT
   ========================================================================== */
async function viewEnrich(view) {
  view.appendChild(viewHead('Enrichment', 'ECINET enrichment of pending voters', []));
  if (!requireYear(view)) return;
  const host = el('div', {});
  view.appendChild(host);
  host.appendChild(loadingRow('Loading enrichment status…'));
  let d;
  try { d = await api('/api/enrich/summary?year=' + encodeURIComponent(state.year)); }
  catch (e) { clear(host); host.appendChild(emptyState('⚠', 'Could not load', e.detail || e.message)); return; }
  clear(host);

  const cfg = d.config || {};
  host.appendChild(el('div', { class: 'panel pad mb' },
    el('div', { class: 'wrap-flex' },
      badge(cfg.available ? 'Config available' : 'Not configured', cfg.available ? 'low' : 'high'),
      cfg.source ? badge('source: ' + cfg.source, 'neutral') : null,
      (cfg.acs && cfg.acs.length) ? badge(cfg.acs.length + ' ACs configured', 'accent') : null),
    cfg.message ? el('div', { class: 'small dim mt' }, cfg.message) : null));

  // config paste
  const cfgArea = el('textarea', { rows: 6, placeholder: 'Paste ECINET config JSON here…', style: 'width:100%;font-family:var(--mono);font-size:12px' });
  host.appendChild(el('div', { class: 'panel', style: 'margin-bottom:14px' },
    el('div', { class: 'panel-head' }, el('h3', {}, 'Configuration')),
    el('div', { class: 'panel-body' }, cfgArea,
      el('div', { class: 'mt' }, el('button', { class: 'btn primary', onclick: async (e) => {
        if (!cfgArea.value.trim()) return toast('Paste config JSON first.', 'warn');
        try { JSON.parse(cfgArea.value); } catch { return toast('Not valid JSON.', 'error'); }
        e.currentTarget.disabled = true;
        try { const r = await api('/api/enrich/config', { method: 'POST', body: { json: cfgArea.value } }); toast(r.message || 'Saved.', r.ok ? 'success' : 'warn'); route(); }
        catch (ex) { showErr(ex, 'Save config'); e.currentTarget.disabled = false; }
      } }, 'Save config')))));

  // pending summary table
  const pending = d.pending || [];
  host.appendChild(el('div', { class: 'panel', style: 'margin-bottom:14px' },
    el('div', { class: 'panel-head' }, el('h3', {}, `Pending summary (${pending.length})`)),
    el('div', { class: 'panel-body' }, pending.length ? membersTable(pending) : el('div', { class: 'dim small' }, 'Nothing pending enrichment.'))));

  // run form
  const acsInput = el('input', { type: 'text', placeholder: 'comma-separated ACs, blank = all', style: 'width:220px' });
  const perCap = el('input', { type: 'number', min: '1', value: '100', style: 'width:100px' });
  const incImages = el('input', { type: 'checkbox' });
  const incAadhaar = el('input', { type: 'checkbox' });
  host.appendChild(el('div', { class: 'panel', },
    el('div', { class: 'panel-head' }, el('h3', {}, 'Run enrichment')),
    el('div', { class: 'panel-body wrap-flex' },
      field('ACs', acsInput), field('Per-AC cap', perCap),
      el('label', { class: 'check' }, incImages, 'Include images'),
      el('label', { class: 'check' }, incAadhaar, 'Include Aadhaar'),
      el('button', { class: 'btn primary', onclick: async (e) => {
        const acs = acsInput.value.trim() ? acsInput.value.split(',').map(s => s.trim()).filter(Boolean) : null;
        e.currentTarget.disabled = true; clear(e.currentTarget).append(spinner(), ' Running…');
        try {
          const r = await api('/api/enrich/run', { method: 'POST', body: { year: Number(state.year), acs, per_ac_cap: Number(perCap.value) || null, include_images: incImages.checked, include_aadhaar: incAadhaar.checked } });
          toast('Enrichment complete.', 'success');
          const statHost = document.getElementById('enrich-stats');
          if (statHost) { clear(statHost); statHost.appendChild(el('pre', { class: 'json' }, JSON.stringify(r.stats || r, null, 2))); }
        } catch (ex) { showErr(ex, 'Enrichment'); }
        e.currentTarget.disabled = false; clear(e.currentTarget).append('Run enrichment');
      } }, 'Run enrichment')),
    el('div', { id: 'enrich-stats', style: 'padding:0 16px 16px' })));
}

/* ============================================================================
   6h. REPORTS
   ========================================================================== */
async function viewReports(view) {
  view.appendChild(viewHead('Reports', 'Generate & download fraud-flag PDF / ZIP reports for the current year', []));
  if (!requireYear(view)) return;
  const rules = await loadRules();

  const ruleSel = selectEl([{ value: '', label: 'All rules' }].concat(rules.map(r => ({ value: r.id, label: r.id }))), '', null);
  const acInput = el('input', { type: 'text', placeholder: 'AC (blank = all)', style: 'width:150px' });

  const url = (base) => {
    const p = new URLSearchParams({ year: state.year });
    if (ruleSel.value) p.set('rule', ruleSel.value);
    if (acInput.value.trim()) p.set('ac', acInput.value.trim());
    return base + '?' + p.toString();
  };

  view.appendChild(el('div', { class: 'panel pad mb wrap-flex' },
    field('Rule', ruleSel), field('Constituency', acInput)));

  view.appendChild(el('div', { class: 'grid cols-2' },
    reportCard('Fraud flags — PDF', 'All open flags for the selected rule/AC, formatted as a PDF report.',
      () => download(url('/api/reports/flags.pdf'))),
    reportCard('Fraud flags — ZIP', 'Per-constituency flag PDFs bundled into a ZIP archive.',
      () => download(url('/api/reports/flags.zip'))),
  ));
  view.appendChild(el('div', { class: 'small dim mt' },
    el('span', {}, 'Combined-model reports (comprehensive report + per-voter dossier, ≤50 voters/PDF) now live in the '),
    el('a', { href: '#/combined' }, 'Combined Model tab →'),
    el('span', {}, '  Downloads open in a new tab.')));
}

function reportCard(title, desc, onClick) {
  return el('div', { class: 'panel pad' },
    el('div', { style: 'font-weight:700;font-size:15px;margin-bottom:5px' }, title),
    el('div', { class: 'small dim', style: 'margin-bottom:12px' }, desc),
    el('button', { class: 'btn primary', onclick: onClick }, '⎙ Download'));
}

/* ============================================================================
   6i. COMBINED MODEL — dedicated tab: build once, then export the two
   ≤50-voter PDF facilities (comprehensive report + per-voter dossier).
   ========================================================================== */
async function viewCombined(view) {
  view.appendChild(viewHead('Combined Model',
    'Four doubt signals fused per voter — logical discrepancy · no category mapping · cosine_new · fuzzy_new. Build once, then export the ≤50-voter PDF reports below.',
    [el('button', { class: 'btn', onclick: () => rebuildSuspects(view) }, '↻ Rebuild model')]));
  if (!requireYear(view)) return;

  const host = el('div', {});
  view.appendChild(host);
  host.appendChild(loadingRow('Checking combined-model cache…'));

  let summary;
  try { summary = await api('/api/suspects/summary?year=' + encodeURIComponent(state.year)); }
  catch (e) { clear(host); host.appendChild(emptyState('⚠', 'Could not load', e.detail || e.message)); return; }

  clear(host);
  if (!summary.built) {
    host.appendChild(emptyState('🧬', 'Combined model not built yet',
      'Build the combined model for ' + state.year + ' to fuse the four signals per voter, then export the comprehensive report and per-voter dossiers.',
      el('button', { class: 'btn primary', onclick: () => rebuildSuspects(view) }, '🧬 Build combined model')));
    return;
  }

  // summary strip
  const s = summary.summary || {};
  host.appendChild(el('div', { class: 'grid cols-4 mb' },
    statCard('Flagged voters', s.total, 'accent', 'built ' + (summary.built_at ? new Date(summary.built_at).toLocaleString() : '')),
    statCard('High', s.high || 0, 'high'),
    statCard('Medium', s.medium || 0, 'medium'),
    statCard('Low', s.low || 0, 'low'),
  ));

  // shared scope controls
  const acOptions = [{ value: '', label: 'All constituencies' }].concat((summary.constituencies || []).map(a => ({ value: a, label: 'AC ' + a })));
  const acSel = selectEl(acOptions, '', null, {});
  const topInput = el('input', { type: 'number', min: '1', placeholder: 'all in scope', style: 'width:130px' });

  host.appendChild(el('div', { class: 'panel pad mb wrap-flex' },
    field('Constituency', acSel),
    field('Top-N voters (blank = all)', topInput),
    el('div', { class: 'small dim', style: 'align-self:center;max-width:340px' },
      'Priority order: fuzzy/cosine duplicates → logical discrepancy → no-mapping. Every export is split into PDFs of at most 50 voters (part 01 = strongest leads).')));

  const dl = (base, topKey) => {
    const p = new URLSearchParams({ year: state.year });
    if (acSel.value) p.set('ac', acSel.value);
    if (topInput.value) p.set(topKey, topInput.value);
    return base + '?' + p.toString();
  };

  host.appendChild(el('div', { class: 'grid cols-2' },
    reportCard('Facility 1 — Comprehensive report (ZIP)',
      'Every flagged voter with all findings, methods, reasons and the full duplicate-comparison logic, in priority order. Split into PDFs of ≤50 voters.',
      () => download(dl('/api/reports/combined_comprehensive.zip', 'top'))),
    reportCard('Facility 2 — Full dossier (ZIP)',
      'A complete case file per voter — every stored data point, every photo, and the EF form rendered large — plus the same full record for each cosine/fuzzy duplicate. Split into PDFs of ≤50 voters.',
      () => download(dl('/api/reports/combined_dossier.zip', 'count'))),
  ));

  host.appendChild(el('div', { class: 'small dim mt' },
    el('span', {}, 'Names come from the ECINET verified record. A flag is a lead, not a verdict. '),
    el('a', { href: '#/suspects' }, 'Browse the full suspect list →')));
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */
window.addEventListener('hashchange', route);

async function boot() {
  clear(APP).appendChild(el('div', { class: 'login-wrap' }, el('div', { class: 'row-flex' }, spinner(true), el('span', { style: 'color:var(--text-2)' }, 'Starting command center…'))));
  // config (no auth) — best effort
  try { state.config = await api('/api/config'); } catch { state.config = { app_name: 'Electoral Roll' }; }
  // auth check
  let me;
  try { me = await api('/api/me'); }
  catch (e) { if (e.status === 401) return; state.dbOffline = true; me = { authenticated: false }; }
  state.me = me;
  if (!me || !me.authenticated) { renderLogin(); return; }

  // load years
  try { const y = await api('/api/years'); state.years = y.years || []; } catch { state.years = []; }
  if (state.year && !state.years.map(String).includes(String(state.year))) state.year = state.years.length ? String(state.years[0]) : '';

  if (!location.hash) location.hash = '#/overview';
  buildShell();
}

boot();
