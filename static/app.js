'use strict';
/* PDF Edit — éditeur dans le navigateur.
 * Toutes les coordonnées du modèle sont en points PDF (1/72 de pouce), origine en haut à gauche.
 * À l'écran : pixels = points × zoom (variable CSS --z sur chaque page).
 */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const M_START = '', M_END = '';
const SESSION_KEY = 'pdfedit.session';

const S = {
  doc: null,
  fonts: {},
  blocks: {},
  objects: [],
  assets: {},
  zoom: 1.25,
  tool: 'select',
  selId: null,
  editingId: null,
  undo: [], redo: [], snap: '',
  broken: new Set(),
  defaults: { f: null, s: 12, c: '#000000' },
  shapeDefaults: {
    rect: { stroke: '#111827', fill: null, sw: 1.5, opacity: 1 },
    ellipse: { stroke: '#111827', fill: null, sw: 1.5, opacity: 1 },
    line: { stroke: '#111827', fill: null, sw: 1.5, opacity: 1 },
    highlight: { stroke: null, fill: '#fde047', sw: 0, opacity: 0.4 },
    whiteout: { stroke: null, fill: '#ffffff', sw: 0, opacity: 1 },
  },
  savedRange: null,
};
const els = new Map();      // id -> élément affiché
const patches = new Map();  // id -> cache masquant l'original
let pageEls = [];
let uid = 0;
const newId = (p) => `${p}${Date.now().toString(36)}${(uid++).toString(36)}`;

// ---------------------------------------------------------------------------
// Utilitaires
// ---------------------------------------------------------------------------

function esc(s) {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

async function api(url, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function toast(msg, error = false, ms = 3500) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = error ? 'error' : '';
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

function busy(msg) {
  $('#busy').hidden = !msg;
  if (msg) $('#busy-msg').textContent = msg;
}

const byId = (id) => S.objects.find((o) => o.id === id);
const isTextObj = (o) => o && (o.type === 'text' || (o.type === 'block' && o.state === 'edited'));
const isTextLike = (o) => o && (o.type === 'text' || (o.type === 'block' && o.state !== 'deleted'));
const contentOf = (id) => els.get(id)?.querySelector('.tcontent');

// ---------------------------------------------------------------------------
// Polices
// ---------------------------------------------------------------------------

function fontStack(f) {
  return `'F_${f}','FB_${f}'`;
}

async function ensureFonts(list) {
  const jobs = [];
  for (const f of list) {
    const known = S.fonts[f.id];
    S.fonts[f.id] = f;
    if (known && known._loaded) { f._loaded = true; continue; }
    f._loaded = true;
    const load = (name, fid) => {
      const face = new FontFace(name, `url(/api/font/${fid})`);
      return face.load().then((ff) => document.fonts.add(ff));
    };
    jobs.push(load('F_' + f.id, f.primary).catch(() => { S.broken.add(f.id); console.warn('Police non chargée', f.label); }));
    jobs.push(load('FB_' + f.id, f.fallback).catch(() => {}));
  }
  await Promise.all(jobs);
}

async function getVariant(family, weight, italic) {
  const n = norm(family);
  const hit = Object.values(S.fonts).find((f) => norm(f.family) === n && f.weight === weight && !!f.italic === !!italic);
  if (hit) return hit.id;
  const res = await api(`/api/doc/${S.doc.id}/font`, { family, weight, italic });
  await ensureFonts(res.fonts);
  refreshFamilyList();
  return res.id;
}

// ---------------------------------------------------------------------------
// Texte riche : paragraphes de "runs" {t, f, s, c, ls}
// ---------------------------------------------------------------------------

function runCSS(r) {
  let css = `font-family:${fontStack(r.f)};font-size:calc(var(--z)*${r.s}px);color:${r.c};`;
  if (r.ls) css += `letter-spacing:calc(var(--z)*${r.ls}px);`;
  return css;
}

function spanHTML(r) {
  return `<span data-f="${r.f}" data-s="${r.s}" data-c="${r.c}" data-ls="${r.ls || 0}" style="${runCSS(r)}">${esc(r.t)}</span>`;
}

const sameStyle = (a, b) => a.f === b.f && +a.s === +b.s && a.c === b.c && +(a.ls || 0) === +(b.ls || 0);

function mergeRuns(paras) {
  return paras.map((p) => {
    const out = [];
    for (const r of p) {
      if (!r.t) continue;
      const last = out[out.length - 1];
      if (last && sameStyle(last, r)) last.t += r.t;
      else out.push({ ...r });
    }
    return out;
  });
}

function parasToHTML(paras) {
  return mergeRuns(paras)
    .map((p) => `<div class="para">${p.length ? p.map(spanHTML).join('') : '<br>'}</div>`)
    .join('');
}

function readStyle(el) {
  return { f: el.dataset.f, s: +el.dataset.s, c: el.dataset.c, ls: +(el.dataset.ls || 0) };
}

function setRootStyle(c, st) {
  c.dataset.f = st.f; c.dataset.s = st.s; c.dataset.c = st.c; c.dataset.ls = st.ls || 0;
  c.style.fontFamily = fontStack(st.f);
  c.style.fontSize = `calc(var(--z)*${st.s}px)`;
  c.style.color = st.c;
}

function isLastInBlock(node, root) {
  let n = node;
  while (n && n !== root) {
    if (n.nextSibling) {
      let s = n.nextSibling;
      while (s && s.nodeType === 3 && !s.data.length) s = s.nextSibling;
      if (s) return false;
    }
    const p = n.parentNode;
    if (!p || p === root || p.tagName === 'DIV' || p.tagName === 'P') return true;
    n = p;
  }
  return true;
}

/** Lit le contenu éditable et le convertit en paragraphes de runs (tolère le HTML produit par le navigateur). */
function extract(c) {
  const def = readStyle(c);
  const paras = [];
  let cur = null, last = null;
  const newPara = () => { cur = []; paras.push(cur); };
  const addText = (t, st) => {
    const style = st || last || def;
    const parts = t.replace(/​/g, '').split('\n');
    parts.forEach((part, i) => {
      if (i > 0) newPara();
      if (!cur) newPara();
      if (part) cur.push({ t: part, ...style });
    });
    last = style;
  };
  const walk = (node, st) => {
    for (const ch of [...node.childNodes]) {
      if (ch.nodeType === 3) {
        if (ch.data) addText(ch.data, st);
      } else if (ch.nodeType === 1) {
        const tag = ch.tagName;
        if (tag === 'BR') {
          if (!cur) newPara();
          if (!isLastInBlock(ch, c)) newPara();
        } else if (tag === 'DIV' || tag === 'P') {
          newPara();
          walk(ch, st);
          cur = null;
        } else {
          walk(ch, ch.dataset && ch.dataset.f ? readStyle(ch) : st);
        }
      }
    }
  };
  walk(c, null);
  if (!paras.length) paras.push([]);
  return paras;
}

function needsNormalize(c) {
  for (const p of c.childNodes) {
    if (p.nodeType !== 1 || p.tagName !== 'DIV' || !p.classList.contains('para')) return true;
    for (const n of p.childNodes) {
      if (n.nodeType === 1 && n.tagName === 'SPAN' && n.dataset.f) {
        for (const t of n.childNodes) if (t.nodeType !== 3) return true;
      } else if (n.nodeType === 1 && n.tagName === 'BR') {
        if (p.childNodes.length > 1) return true;
      } else return true;
    }
  }
  return false;
}

function insertMarkers(c, withEnd = true) {
  const sel = getSelection();
  if (!sel.rangeCount) return false;
  const r = sel.getRangeAt(0);
  if (!c.contains(r.startContainer) || !c.contains(r.endContainer)) return false;
  if (withEnd && !r.collapsed) {
    const e = r.cloneRange(); e.collapse(false); e.insertNode(document.createTextNode(M_END));
  }
  const s = r.cloneRange(); s.collapse(true); s.insertNode(document.createTextNode(M_START));
  return true;
}

function restoreMarkers(c) {
  const find = (m) => {
    const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) { const i = n.data.indexOf(m); if (i >= 0) return [n, i]; }
    return null;
  };
  const e = find(M_END);
  if (e) e[0].deleteData(e[1], 1);
  const s = find(M_START);
  if (s) {
    s[0].deleteData(s[1], 1);
    if (e && e[0] === s[0] && e[1] > s[1]) e[1] -= 1;
  }
  if (!s) return;
  const sel = getSelection();
  const r = document.createRange();
  // Paragraphe devenu vide : on remet un <br> pour que le curseur reste visible
  const para = s[0].parentElement.closest('.para');
  if (para && !para.textContent.length) {
    para.innerHTML = '<br>';
    r.setStart(para, 0);
    r.collapse(true);
  } else {
    r.setStart(s[0], s[1]);
    if (e) r.setEnd(e[0], e[1]); else r.collapse(true);
  }
  sel.removeAllRanges();
  sel.addRange(r);
}

function rebuild(c, paras) {
  c.innerHTML = parasToHTML(paras);
}

function normalizeContent(c, keepCaret = true) {
  if (!needsNormalize(c)) return false;
  const had = keepCaret && insertMarkers(c);
  rebuild(c, extract(c));
  if (had) restoreMarkers(c);
  return true;
}

function plainText(o) {
  if (o.type === 'block' && o.state === 'orig') return S.blocks[o.id].paras.map((p) => p.map((r) => r.t).join('')).join('\n');
  const c = contentOf(o.id);
  return c ? c.textContent : '';
}

// ---------------------------------------------------------------------------
// Pages
// ---------------------------------------------------------------------------

function renderScale() {
  return Math.min(4, Math.max(1, Math.ceil(S.zoom * (window.devicePixelRatio || 1) * 2) / 2));
}
const pageURL = (n, layer, sc) => `/api/doc/${S.doc.id}/page/${n}.png?layer=${layer}&scale=${sc}`;

function buildPages() {
  const root = $('#pages');
  root.innerHTML = '';
  pageEls = S.doc.pages.map((pg, n) => {
    const p = document.createElement('div');
    p.className = 'page';
    p.dataset.n = n;
    p.innerHTML = `<div class="page-num">Page ${n + 1} / ${S.doc.pages.length}</div><img class="bg" draggable="false" alt=""><div class="layer patches"></div><div class="layer objs"></div><div class="layer overlay"></div>`;
    root.append(p);
    return p;
  });
  layoutPages();
}

function layoutPages() {
  const z = S.zoom, sc = renderScale();
  pageEls.forEach((p, n) => {
    const pg = S.doc.pages[n];
    p.style.width = pg.w * z + 'px';
    p.style.height = pg.h * z + 'px';
    p.style.setProperty('--z', z);
    const img = $('img.bg', p);
    const src = pageURL(n, 'full', sc);
    if (img.getAttribute('src') !== src) { img.loading = n > 2 ? 'lazy' : 'eager'; img.src = src; }
  });
  $('#zoom-val').textContent = Math.round(z * 100) + '%';
}

function setTool(t) {
  if (t === 'image') { $('#img-file').click(); return; }
  S.tool = t;
  $$('#tools .tool').forEach((b) => b.classList.toggle('active', b.dataset.tool === t));
  pageEls.forEach((p) => {
    p.classList.toggle('tool-text', t === 'text');
    p.classList.toggle('tool-shape', ['rect', 'ellipse', 'line', 'highlight', 'whiteout'].includes(t));
  });
  if (t !== 'select') { stopEditing(); select(null); }
  updateToolbar();
}

// ---------------------------------------------------------------------------
// Rendu des objets
// ---------------------------------------------------------------------------

function renderAll() {
  pageEls.forEach((p) => { $('.objs', p).innerHTML = ''; $('.patches', p).innerHTML = ''; $('.overlay', p).innerHTML = ''; });
  els.clear(); patches.clear();
  S.objects.forEach((o) => renderObj(o));
  renderSelection();
}

function setPatch(o, rect, kind) {
  const old = patches.get(o.id);
  if (old) old.remove();
  patches.delete(o.id);
  if (!rect) return;
  const z = S.zoom, pg = S.doc.pages[o.page];
  const d = document.createElement('div');
  d.className = 'patch';
  const pad = 1;
  const [x0, y0, x1, y1] = [rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad];
  Object.assign(d.style, { left: x0 * z + 'px', top: y0 * z + 'px', width: (x1 - x0) * z + 'px', height: (y1 - y0) * z + 'px' });
  if (kind.startsWith('#')) d.style.background = kind;
  else {
    d.style.backgroundImage = `url(${pageURL(o.page, kind, renderScale())})`;
    d.style.backgroundSize = `${pg.w * z}px ${pg.h * z}px`;
    d.style.backgroundPosition = `${-x0 * z}px ${-y0 * z}px`;
  }
  $('.patches', pageEls[o.page]).append(d);
  patches.set(o.id, d);
}

function placeBox(el, o) {
  const z = S.zoom, b = o.box, pg = S.doc.pages[o.page];
  el.style.top = b.y * z + 'px';
  el.style.transform = '';
  el.style.right = 'auto';
  if (b.w != null) {
    const left = b.anchor === 'left' ? b.x : b.anchor === 'right' ? b.x - b.w : b.x - b.w / 2;
    el.style.left = left * z + 'px';
    el.style.width = b.w * z + 'px';
    el.style.maxWidth = '';
  } else {
    el.style.width = 'max-content';
    el.style.maxWidth = b.maxW * z + 'px';
    if (b.anchor === 'right') { el.style.left = 'auto'; el.style.right = (pg.w - b.x) * z + 'px'; }
    else if (b.anchor === 'center') { el.style.left = b.x * z + 'px'; el.style.transform = 'translateX(-50%)'; }
    else el.style.left = b.x * z + 'px';
  }
  const c = el.firstChild;
  c.style.textAlign = o.align;
  c.style.lineHeight = `calc(var(--z)*${o.lh}px)`;
  c.style.wordSpacing = o.ws ? `calc(var(--z)*${o.ws}px)` : '';
  c.style.setProperty('--indent', `calc(var(--z)*${o.indent || 0}px)`);
}

function makeTBox(o) {
  const el = document.createElement('div');
  el.className = 'obj tbox';
  el.dataset.id = o.id;
  const c = document.createElement('div');
  c.className = 'tcontent';
  c.spellcheck = false;
  setRootStyle(c, o.def);
  c.innerHTML = o.html;
  el.append(c);
  placeBox(el, o);
  return el;
}

function rectStyle(el, r) {
  const z = S.zoom;
  const x0 = Math.min(r[0], r[2]), y0 = Math.min(r[1], r[3]);
  Object.assign(el.style, { left: x0 * z + 'px', top: y0 * z + 'px', width: Math.abs(r[2] - r[0]) * z + 'px', height: Math.abs(r[3] - r[1]) * z + 'px' });
}

function drawShape(svg, o) {
  const z = S.zoom, r = o.rect;
  const x0 = Math.min(r[0], r[2]), y0 = Math.min(r[1], r[3]);
  const w = Math.abs(r[2] - r[0]) * z, h = Math.abs(r[3] - r[1]) * z;
  rectStyle(svg, r);
  svg.setAttribute('width', Math.max(w, 1));
  svg.setAttribute('height', Math.max(h, 1));
  const sw = (o.stroke ? o.sw : 0) * z;
  const stroke = o.stroke || 'none', fill = o.fill || 'none';
  let inner;
  if (o.shape === 'line') {
    inner = `<line x1="${(r[0] - x0) * z}" y1="${(r[1] - y0) * z}" x2="${(r[2] - x0) * z}" y2="${(r[3] - y0) * z}" stroke="${o.stroke || '#000'}" stroke-width="${o.sw * z}" stroke-linecap="round"/>`;
    inner += `<line x1="${(r[0] - x0) * z}" y1="${(r[1] - y0) * z}" x2="${(r[2] - x0) * z}" y2="${(r[3] - y0) * z}" stroke="transparent" stroke-width="${Math.max(12, o.sw * z)}"/>`;
  } else if (o.shape === 'ellipse') {
    inner = `<ellipse cx="${w / 2}" cy="${h / 2}" rx="${Math.max(0, w / 2)}" ry="${Math.max(0, h / 2)}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}"/>`;
  } else {
    inner = `<rect x="0" y="0" width="${w}" height="${h}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}"/>`;
  }
  svg.innerHTML = inner;
  svg.style.opacity = o.opacity;
}

function renderObj(o) {
  const old = els.get(o.id);
  if (old) old.remove();
  els.delete(o.id);
  const z = S.zoom;
  let el = null;
  if (o.type === 'block') {
    const b = S.blocks[o.id];
    if (o.state === 'orig') {
      setPatch(o, null);
      el = document.createElement('div');
      el.className = 'hit';
      rectStyle(el, b.bbox);
    } else {
      setPatch(o, b.bbox, b.kind === 'ocr' && b.bg ? b.bg : 'notext');
      if (o.state === 'edited') el = makeTBox(o);
    }
  } else if (o.type === 'image') {
    if (o.state === 'orig') {
      setPatch(o, null);
      el = document.createElement('div');
      el.className = 'hit img';
      rectStyle(el, o.rect);
    } else {
      setPatch(o, o.orig, 'noimg');
      if (o.state === 'edited') {
        el = document.createElement('img');
        el.className = 'obj-img';
        el.draggable = false;
        el.src = `/api/doc/${S.doc.id}/image/${o.xref}.png`;
        rectStyle(el, o.rect);
      }
    }
  } else if (o.type === 'newimage') {
    el = document.createElement('img');
    el.className = 'obj-img';
    el.draggable = false;
    el.src = S.assets[o.asset];
    rectStyle(el, o.rect);
  } else if (o.type === 'text') {
    el = makeTBox(o);
  } else if (o.type === 'shape') {
    el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    el.classList.add('shape');
    drawShape(el, o);
  }
  if (!el) return;
  el.dataset.id = o.id;
  el.style.zIndex = S.objects.indexOf(o) + 1;
  if (o.id === S.editingId) el.style.zIndex = 99999;
  $('.objs', pageEls[o.page]).append(el);
  els.set(o.id, el);
}

function placeObj(o) {
  const el = els.get(o.id);
  if (!el) return renderObj(o);
  if (o.box) placeBox(el, o);
  else if (o.type === 'shape') drawShape(el, o);
  else if (o.rect) rectStyle(el, o.rect);
}

// ---------------------------------------------------------------------------
// Sélection & cadre
// ---------------------------------------------------------------------------

let selObserver = null;

function select(id) {
  if (S.selId !== id && S.editingId && S.editingId !== id) stopEditing();
  S.selId = id;
  renderSelection();
  updateToolbar();
}

function renderSelection() {
  pageEls.forEach((p) => ($('.overlay', p).innerHTML = ''));
  if (selObserver) { selObserver.disconnect(); selObserver = null; }
  const o = byId(S.selId);
  const el = o && els.get(o.id);
  if (!o || !el) return;
  const page = pageEls[o.page];
  const ov = $('.overlay', page);
  const pr = page.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const x = r.left - pr.left, y = r.top - pr.top, w = r.width, h = r.height;
  const frame = document.createElement('div');
  frame.className = 'selframe' + (S.editingId === o.id ? ' editing' : '');
  Object.assign(frame.style, { left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px' });
  ov.append(frame);
  const handle = (mode, hx, hy, cursor, cls = 'handle') => {
    const d = document.createElement('div');
    d.className = cls;
    d.dataset.handle = mode;
    d.style.left = hx + 'px';
    d.style.top = hy + 'px';
    d.style.cursor = cursor;
    ov.append(d);
    return d;
  };
  if (o.type === 'shape' && o.shape === 'line') {
    const z = S.zoom;
    handle('p1', o.rect[0] * z, o.rect[1] * z, 'crosshair');
    handle('p2', o.rect[2] * z, o.rect[3] * z, 'crosshair');
  } else if (o.box || o.type === 'block') {
    const g = handle('move', x - 30, y - 3, 'move', 'handle grip');
    g.title = 'Déplacer';
    g.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 3v18M3 12h18M12 3l-3 3m3-3l3 3M12 21l-3-3m3 3l3-3M3 12l3-3m-3 3l3 3M21 12l-3-3m3 3l-3 3"/></svg>';
    handle('w', x - 3, y + h / 2, 'ew-resize');
    handle('e', x + w + 3, y + h / 2, 'ew-resize');
    // bords haut/bas : déplacer
    for (const [ty, hh] of [[y - 8, 6], [y + h + 2, 6]]) {
      const d = document.createElement('div');
      d.className = 'edge';
      d.dataset.handle = 'move';
      Object.assign(d.style, { left: x + 'px', top: ty + 'px', width: w + 'px', height: hh + 'px' });
      ov.append(d);
    }
  } else {
    const pts = { nw: [x, y], n: [x + w / 2, y], ne: [x + w, y], e: [x + w, y + h / 2], se: [x + w, y + h], s: [x + w / 2, y + h], sw: [x, y + h], w: [x, y + h / 2] };
    const cur = { nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' };
    for (const [k, [hx, hy]] of Object.entries(pts)) handle(k, hx, hy, cur[k]);
  }
  if (o.box) {
    selObserver = new ResizeObserver(() => requestAnimationFrame(renderSelectionPosition));
    selObserver.observe(el);
  }
}

// Repositionnement léger (pendant la frappe)
function renderSelectionPosition() {
  const o = byId(S.selId);
  if (!o || !o.box) return;
  const el = els.get(o.id);
  const frame = $('.selframe', pageEls[o.page]);
  if (!el || !frame) return;
  const pr = pageEls[o.page].getBoundingClientRect(), r = el.getBoundingClientRect();
  const fr = frame.getBoundingClientRect();
  if (Math.abs(fr.width - r.width) > 0.5 || Math.abs(fr.height - r.height) > 0.5 || Math.abs(fr.left - r.left) > 0.5 || Math.abs(fr.top - r.top) > 0.5) {
    const keep = selObserver;
    selObserver = null;
    renderSelection();
    if (!selObserver) selObserver = keep;
  }
  void pr;
}

// ---------------------------------------------------------------------------
// Édition de texte
// ---------------------------------------------------------------------------

function blockDefaultStyle(b) {
  const first = b.paras.flat()[0];
  return first ? { f: first.f, s: first.s, c: first.c, ls: 0 } : { ...S.defaults, ls: 0 };
}

/** Transforme un bloc d'origine en zone de texte éditable, à la même position. */
function activateBlock(o) {
  const b = S.blocks[o.id];
  o.state = 'edited';
  o.box = { ...b.box, y: b.base - b.size };
  o.html = parasToHTML(b.paras);
  o.def = blockDefaultStyle(b);
  o.align = b.align;
  o.lh = b.lh || round(b.size * 1.2);
  o.indent = b.indent || 0;
  o.ws = b.ws || 0;
  o.pristine = true;
  renderObj(o);
  // Aligner la première ligne de base exactement sur celle du PDF
  const el = els.get(o.id);
  const y = firstBaseline(el);
  if (y != null) { o.box.y = b.base - y / S.zoom; placeBox(el, o); }
  o._initialHTML = el.firstChild.innerHTML;
}

/** Distance (px) entre le haut du rectangle d'un caractère et sa ligne de base, pour une police et une taille. */
const baseCache = new Map();
function baselineOffset(st) {
  const key = `${st.f}|${st.s}|${S.zoom}`;
  if (baseCache.has(key)) return baseCache.get(key);
  const d = document.createElement('div');
  d.style.cssText = 'position:absolute;left:-10000px;top:0;white-space:pre;';
  d.innerHTML = `<span style="font-family:${fontStack(st.f)};font-size:${st.s * S.zoom}px;font-kerning:none"><span style="display:inline-block;width:0;height:0;vertical-align:baseline"></span>H</span>`;
  document.body.append(d);
  const outer = d.firstChild;
  const r = document.createRange();
  r.setStart(outer.lastChild, 0); r.setEnd(outer.lastChild, 1);
  const off = outer.firstChild.getBoundingClientRect().top - r.getClientRects()[0].top;
  d.remove();
  if (document.fonts.status === 'loaded') baseCache.set(key, off);
  return off;
}

function firstBaseline(el) {
  const c = el.firstChild;
  const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) if (n.data.trim()) break;
  if (!n) return null;
  const i = n.data.search(/\S/);
  const r = document.createRange();
  r.setStart(n, i); r.setEnd(n, i + 1);
  const rect = r.getClientRects()[0];
  if (!rect) return null;
  const sp = n.parentElement.closest('[data-f]');
  const st = readStyle(sp && c.contains(sp) ? sp : c);
  return rect.top + baselineOffset(st) - el.getBoundingClientRect().top;
}

function startEditing(o, clientX, clientY) {
  if (S.editingId && S.editingId !== o.id) stopEditing();
  S.editingId = o.id;
  S.selId = o.id;
  const el = els.get(o.id);
  el.style.zIndex = 99999;
  const c = el.firstChild;
  c.contentEditable = 'true';
  if (o._initialHTML == null) o._initialHTML = c.innerHTML;
  if (clientX != null) {
    c.focus({ preventScroll: true });
    const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(clientX, clientY) : null;
    if (r && c.contains(r.startContainer)) {
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    }
  } else if (clientX === undefined) {
    c.focus({ preventScroll: true });
    const r = document.createRange(); r.selectNodeContents(c); r.collapse(false);
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
  }
  renderSelection();
  updateToolbar();
}

function syncHTML(o) {
  const c = contentOf(o.id);
  if (!c) return;
  normalizeContent(c, S.editingId === o.id);
  o.html = c.innerHTML;
  o.def = readStyle(c);
}

function stopEditing() {
  const id = S.editingId;
  if (!id) return;
  S.editingId = null;
  clearTimeout(inputTimer);
  const o = byId(id);
  const c = contentOf(id);
  if (!o || !c) return;
  c.contentEditable = 'false';
  syncHTML(o);
  const empty = !c.textContent.replace(/[\s​]/g, '').length;
  if (o.type === 'block' && o.pristine && c.innerHTML === o._initialHTML) {
    o.state = 'orig';
    for (const k of ['box', 'html', 'def', 'align', 'lh', 'indent', 'ws', 'pristine']) delete o[k];
  } else if (empty) {
    if (o.type === 'block') o.state = 'deleted';
    else S.objects.splice(S.objects.indexOf(o), 1);
    if (S.selId === id) S.selId = null;
  }
  delete o._initialHTML;
  renderObj(o);
  if (!S.objects.includes(o)) { const el = els.get(o.id); if (el) el.remove(); els.delete(o.id); }
  commit();
  renderSelection();
  updateToolbar();
}

let inputTimer = null;
function onContentInput(e) {
  const c = e.target.closest('.tcontent');
  if (!c || e.isComposing) return;
  normalizeContent(c);
  const o = byId(c.parentElement.dataset.id);
  if (o) o.pristine = o.pristine && true;
  clearTimeout(inputTimer);
  inputTimer = setTimeout(() => { if (o && S.editingId === o.id) { syncHTML(o); commit(); } }, 700);
}

// ---------------------------------------------------------------------------
// Styles de texte (barre d'outils)
// ---------------------------------------------------------------------------

function currentTextObj() {
  const o = byId(S.selId);
  if (o && o.type === 'block' && o.state === 'orig') activateBlock(o);
  return isTextObj(o) ? o : null;
}

function restoreSavedRange(c) {
  const sel = getSelection();
  if (S.savedRange && c.contains(S.savedRange.startContainer)) {
    sel.removeAllRanges();
    sel.addRange(S.savedRange);
  }
  return sel.rangeCount ? sel.getRangeAt(0) : null;
}

function fontsInRange(c, range) {
  const set = new Set();
  const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    if (!n.data.length || (range && !range.intersectsNode(n))) continue;
    const sp = n.parentElement.closest('[data-f]');
    set.add(sp ? sp.dataset.f : c.dataset.f);
  }
  if (!set.size) set.add(c.dataset.f);
  return [...set];
}

async function applyTextStyle(kind, value) {
  const o = currentTextObj();
  if (!o) {
    // Aucun texte sélectionné : style par défaut des nouveaux textes
    if (kind === 'size') S.defaults.s = value;
    if (kind === 'color') S.defaults.c = value;
    if (['family', 'bold', 'italic'].includes(kind)) {
      const df = S.fonts[S.defaults.f];
      const fam = kind === 'family' ? value : df.family;
      const wt = kind === 'bold' ? (df.weight >= 600 ? 400 : 700) : df.weight;
      const it = kind === 'italic' ? !df.italic : df.italic;
      S.defaults.f = await getVariant(fam, wt, it);
    }
    updateToolbar();
    return;
  }
  const c = contentOf(o.id);
  const editing = S.editingId === o.id;
  let range = editing ? restoreSavedRange(c) : null;
  const partial = !!(range && !range.collapsed && c.contains(range.commonAncestorContainer));
  const fids = fontsInRange(c, partial ? range : null);

  let fmap = null;
  if (['family', 'bold', 'italic'].includes(kind)) {
    fmap = {};
    const first = S.fonts[fids[0]];
    const targetW = first.weight >= 600 ? 400 : 700;
    const targetI = !first.italic;
    for (const f of fids) {
      const df = S.fonts[f];
      const fam = kind === 'family' ? value : df.family;
      const wt = kind === 'bold' ? targetW : df.weight;
      const it = kind === 'italic' ? targetI : !!df.italic;
      try { fmap[f] = await getVariant(fam, wt, it); } catch (err) { toast(err.message, true); return; }
    }
    const got = S.fonts[fmap[fids[0]]];
    if (kind === 'bold' && got && (got.weight >= 600) !== (targetW >= 600)) toast(`Pas de version ${targetW >= 600 ? 'grasse' : 'normale'} disponible pour « ${first.family} ».`);
    if (kind === 'italic' && got && !!got.italic !== targetI) toast(`Pas de version ${targetI ? 'italique' : 'droite'} disponible pour « ${first.family} ».`);
    if (editing) range = restoreSavedRange(c);
  }
  const patch = (r) => {
    if (fmap && fmap[r.f]) r.f = fmap[r.f];
    if (kind === 'size') r.s = value;
    if (kind === 'color') r.c = value;
    return r;
  };
  if (partial) {
    insertMarkers(c);
    const paras = extract(c);
    let inside = false;
    const out = paras.map((p) => {
      const np = [];
      for (const r of p) {
        let buf = '';
        const flush = () => { if (buf) { const nr = { ...r, t: buf }; np.push(inside ? patch(nr) : nr); buf = ''; } };
        for (const ch of r.t) {
          if (ch === M_START) { flush(); inside = true; buf = ch; }
          else if (ch === M_END) { flush(); inside = false; buf = ch; }
          else buf += ch;
        }
        flush();
      }
      return np;
    });
    rebuild(c, out);
    restoreMarkers(c);
  } else {
    const had = editing && insertMarkers(c, false);
    const oldSize = +c.dataset.s;
    const paras = extract(c).map((p) => p.map(patch));
    setRootStyle(c, patch(readStyle(c)));
    rebuild(c, paras);
    if (had) restoreMarkers(c);
    if (kind === 'size' && oldSize) {
      o.lh = round(o.lh * value / oldSize);
      placeBox(els.get(o.id), o);
    }
  }
  if (editing) S.savedRange = getSelection().rangeCount ? getSelection().getRangeAt(0) : null;
  o.pristine = false;
  syncHTML(o);
  commit();
  renderSelection();
  updateToolbar();
}

function setBlockProp(prop, value) {
  const o = currentTextObj();
  if (!o) return;
  o[prop] = value;
  o.pristine = false;
  placeBox(els.get(o.id), o);
  syncHTML(o);
  commit();
  renderSelection();
  updateToolbar();
}

// ---------------------------------------------------------------------------
// Barre d'outils contextuelle
// ---------------------------------------------------------------------------

function refreshFamilyList() {
  const sel = $('#f-family');
  const docFams = [...new Set(Object.values(S.fonts).map((f) => f.family))].sort((a, b) => a.localeCompare(b));
  const extra = S.doc.extraFamilies.filter((f) => !docFams.some((d) => norm(d) === norm(f)));
  sel.innerHTML = `<optgroup label="Polices du document">${docFams.map((f) => `<option>${esc(f)}</option>`).join('')}</optgroup>` +
    `<optgroup label="Autres polices">${extra.map((f) => `<option>${esc(f)}</option>`).join('')}</optgroup>`;
}

function styleAtCaret() {
  const o = byId(S.selId);
  if (!isTextLike(o)) return { ...S.defaults };
  if (o.type === 'block' && o.state === 'orig') return blockDefaultStyle(S.blocks[o.id]);
  const c = contentOf(o.id);
  if (!c) return o.def;
  let node = null;
  if (S.editingId === o.id) {
    const sel = getSelection();
    if (sel.rangeCount && c.contains(sel.anchorNode)) node = sel.anchorNode;
  }
  if (!node) node = c.querySelector('[data-f]') || c;
  const el = node.nodeType === 3 ? node.parentElement : node;
  const sp = el.closest('[data-f]');
  return readStyle(sp && c.contains(sp) ? sp : c);
}

function setSwatch(input, color) {
  const lab = input.parentElement;
  lab.classList.toggle('none', !color);
  if (color) { input.value = color; lab.querySelector('.swatch').style.background = color; }
}

function updateToolbar() {
  const o = byId(S.selId);
  const shapeTool = ['rect', 'ellipse', 'line', 'highlight', 'whiteout'].includes(S.tool);
  const showText = isTextLike(o) || (!o && S.tool === 'text');
  const showShape = (o && o.type === 'shape') || (!o && shapeTool);
  $('#ctx-text').hidden = !showText;
  $('#ctx-shape').hidden = !showShape;
  $('#ctx-hint').hidden = showText || showShape || !!o;
  $('#btn-delete').hidden = !o;
  if (showText) {
    const st = styleAtCaret();
    const f = S.fonts[st.f] || {};
    const sel = $('#f-family');
    if (f.family && ![...sel.options].some((op) => op.value === f.family)) refreshFamilyList();
    sel.value = f.family || '';
    $('#f-size').value = round(st.s, 1);
    setSwatch($('#f-color'), st.c);
    $('#f-bold').classList.toggle('on', (f.weight || 400) >= 600);
    $('#f-italic').classList.toggle('on', !!f.italic);
    const align = o ? (o.align || S.blocks[o.id]?.align || 'left') : 'left';
    $$('[data-align]').forEach((b) => b.classList.toggle('on', b.dataset.align === align));
    $$('[data-align]').forEach((b) => (b.disabled = !o));
    $('#f-lh').disabled = !o;
    const lh = o ? (o.lh || S.blocks[o.id]?.lh || round((S.blocks[o.id]?.size || 12) * 1.2)) : '';
    $('#f-lh').value = o ? round(lh, 1) : '';
  }
  if (showShape) {
    const sh = o && o.type === 'shape' ? o : S.shapeDefaults[S.tool];
    setSwatch($('#s-stroke'), sh.stroke);
    setSwatch($('#s-fill'), sh.fill);
    $('#s-stroke-none').classList.toggle('on', !sh.stroke);
    $('#s-fill-none').classList.toggle('on', !sh.fill);
    $('#s-width').value = sh.sw;
    $('#s-opacity').value = sh.opacity;
  }
  $('#btn-undo').disabled = !S.undo.length;
  $('#btn-redo').disabled = !S.redo.length;
}

function applyShapeProp(prop, value) {
  const o = byId(S.selId);
  const target = o && o.type === 'shape' ? o : S.shapeDefaults[S.tool];
  if (!target) return;
  target[prop] = value;
  if (prop === 'stroke' && value && !target.sw) target.sw = 1;
  if (o && o.type === 'shape') { drawShape(els.get(o.id), o); commit(); }
  updateToolbar();
}

// ---------------------------------------------------------------------------
// Historique (annuler / rétablir) et sauvegarde de session
// ---------------------------------------------------------------------------

function snapshot() {
  return JSON.stringify(S.objects, (k, v) => (k.startsWith('_') ? undefined : v));
}

function commit() {
  const s = snapshot();
  if (s === S.snap) return;
  S.undo.push(S.snap);
  if (S.undo.length > 200) S.undo.shift();
  S.snap = s;
  S.redo = [];
  updateToolbar();
  saveSession();
}

function restore(snap) {
  S.editingId = null;
  S.objects = JSON.parse(snap);
  S.snap = snap;
  if (!byId(S.selId)) S.selId = null;
  renderAll();
  updateToolbar();
  saveSession();
}

function undo() {
  if (S.editingId) stopEditing();
  if (!S.undo.length) return;
  S.redo.push(S.snap);
  restore(S.undo.pop());
}

function redo() {
  if (S.editingId) stopEditing();
  if (!S.redo.length) return;
  S.undo.push(S.snap);
  restore(S.redo.pop());
}

let sessionTimer = null;
function saveSession() {
  clearTimeout(sessionTimer);
  sessionTimer = setTimeout(() => {
    try {
      localStorage.setItem(SESSION_KEY, JSON.stringify({ id: S.doc.id, name: S.doc.name, objects: JSON.parse(S.snap || '[]'), assets: S.assets, defaults: S.defaults }));
    } catch (e) {
      try { localStorage.setItem(SESSION_KEY, JSON.stringify({ id: S.doc.id, name: S.doc.name, objects: JSON.parse(S.snap || '[]').filter((o) => o.type !== 'newimage'), assets: {}, defaults: S.defaults })); } catch (e2) { /* quota */ }
    }
  }, 800);
}

// ---------------------------------------------------------------------------
// Souris : clics, déplacements, redimensionnements, création
// ---------------------------------------------------------------------------

function pagePoint(page, e) {
  const r = page.getBoundingClientRect();
  return { x: (e.clientX - r.left) / S.zoom, y: (e.clientY - r.top) / S.zoom };
}

function onPagesPointerDown(e) {
  if (e.button !== 0) return;
  const page = e.target.closest('.page');
  if (!page) { stopEditing(); select(null); return; }
  const n = +page.dataset.n;

  const h = e.target.closest('[data-handle]');
  if (h) {
    const o = byId(S.selId);
    if (o) startDrag(e, o, h.dataset.handle);
    return;
  }
  if (S.tool !== 'select') { e.preventDefault(); createAt(e, page, n); return; }

  const objEl = e.target.closest('[data-id]');
  if (!objEl) { stopEditing(); select(null); return; }
  const o = byId(objEl.dataset.id);
  if (!o) return;

  if (o.type === 'block' && o.state === 'orig') {
    e.preventDefault();
    stopEditing();
    activateBlock(o);
    S.selId = o.id;
    startEditing(o, e.clientX, e.clientY);
  } else if (isTextObj(o)) {
    if (S.editingId === o.id) return; // le navigateur gère le curseur
    stopEditing();
    S.selId = o.id;
    // rendu éditable tout de suite : le clic place le curseur
    startEditing(o, null);
  } else {
    e.preventDefault();
    stopEditing();
    select(o.id);
    startDrag(e, o, 'move');
  }
}

function startDrag(e, o, mode) {
  e.preventDefault();
  e.stopPropagation();
  if (o.type === 'block' && o.state === 'orig') activateBlock(o);
  const z = S.zoom, sx = e.clientX, sy = e.clientY;
  const orig = JSON.parse(JSON.stringify({ box: o.box, rect: o.rect }));
  const el = els.get(o.id);
  const page = pageEls[o.page];
  const pr = page.getBoundingClientRect(), er = el.getBoundingClientRect();
  const left0 = (er.left - pr.left) / z, width0 = er.width / z;
  const ratio = o.rect ? Math.abs((orig.rect[2] - orig.rect[0]) / (orig.rect[3] - orig.rect[1] || 1)) : 1;
  let moved = false;
  const move = (ev) => {
    const dx = (ev.clientX - sx) / z, dy = (ev.clientY - sy) / z;
    if (!moved && Math.hypot(dx, dy) * z < 3) return;
    if (!moved) {
      moved = true;
      if (o.type === 'image' && o.state === 'orig') { o.state = 'edited'; renderObj(o); }
    }
    if (o.box) {
      const b = o.box;
      if (mode === 'move') { b.x = orig.box.x + dx; b.y = orig.box.y + dy; }
      else if (mode === 'e') { b.anchor = 'left'; b.x = left0; b.w = Math.max(10, width0 + dx); }
      else if (mode === 'w') { const w = Math.max(10, width0 - dx); b.anchor = 'left'; b.x = left0 + width0 - w; b.w = w; }
      placeBox(els.get(o.id), o);
    } else if (o.rect) {
      const r = [...orig.rect];
      if (mode === 'move') { r[0] += dx; r[2] += dx; r[1] += dy; r[3] += dy; }
      else if (mode === 'p1') { r[0] += dx; r[1] += dy; }
      else if (mode === 'p2') { r[2] += dx; r[3] += dy; }
      else {
        const x0 = Math.min(r[0], r[2]), x1 = Math.max(r[0], r[2]), y0 = Math.min(r[1], r[3]), y1 = Math.max(r[1], r[3]);
        let nx0 = x0, nx1 = x1, ny0 = y0, ny1 = y1;
        if (mode.includes('w')) nx0 = Math.min(x0 + dx, x1 - 2);
        if (mode.includes('e')) nx1 = Math.max(x1 + dx, x0 + 2);
        if (mode.includes('n')) ny0 = Math.min(y0 + dy, y1 - 2);
        if (mode.includes('s')) ny1 = Math.max(y1 + dy, y0 + 2);
        const keep = (o.type === 'image' || o.type === 'newimage') !== ev.shiftKey;
        if (keep && mode.length === 2) {
          const w = nx1 - nx0, h = w / ratio;
          if (mode.includes('n')) ny0 = ny1 - h; else ny1 = ny0 + h;
        }
        r.splice(0, 4, nx0, ny0, nx1, ny1);
      }
      o.rect = r;
      placeObj(o);
    }
    renderSelection();
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    if (moved) {
      if (o.type === 'block') o.pristine = false;
      commit();
      if (S.editingId === o.id) contentOf(o.id).focus({ preventScroll: true });
    }
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

function createAt(e, page, n) {
  const p = pagePoint(page, e);
  const pg = S.doc.pages[n];
  if (S.tool === 'text') {
    const st = { ...S.defaults, ls: 0 };
    const o = {
      id: newId('t'), type: 'text', page: n,
      box: { anchor: 'left', x: p.x, y: p.y - st.s * 0.7, w: null, maxW: Math.max(40, pg.w - p.x - 20) },
      html: '<div class="para"><br></div>', def: st, align: 'left', lh: round(st.s * 1.2), indent: 0,
    };
    S.objects.push(o);
    renderObj(o);
    setTool('select');
    S.selId = o.id;
    startEditing(o);
    return;
  }
  const kind = S.tool;
  const d = S.shapeDefaults[kind];
  const o = { id: newId('s'), type: 'shape', page: n, shape: kind, rect: [p.x, p.y, p.x, p.y], stroke: d.stroke, fill: d.fill, sw: d.sw, opacity: d.opacity };
  S.objects.push(o);
  renderObj(o);
  const z = S.zoom, sx = e.clientX, sy = e.clientY;
  let moved = false;
  const move = (ev) => {
    const dx = (ev.clientX - sx) / z, dy = (ev.clientY - sy) / z;
    if (Math.hypot(dx, dy) * z > 3) moved = true;
    let ex = p.x + dx, ey = p.y + dy;
    if (ev.shiftKey && kind === 'line') { if (Math.abs(dx) > Math.abs(dy)) ey = p.y; else ex = p.x; }
    o.rect = [p.x, p.y, ex, ey];
    drawShape(els.get(o.id), o);
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    if (!moved) {
      const def = { line: [120, 0], highlight: [120, 14] }[kind] || [120, 70];
      o.rect = [p.x, p.y, p.x + def[0], p.y + def[1]];
    } else if (kind !== 'line') {
      const r = o.rect;
      o.rect = [Math.min(r[0], r[2]), Math.min(r[1], r[3]), Math.max(r[0], r[2]), Math.max(r[1], r[3])];
    }
    drawShape(els.get(o.id), o);
    setTool('select');
    commit();
    select(o.id);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

function visiblePage() {
  const v = $('#viewer').getBoundingClientRect();
  let best = 0, bestArea = -1;
  pageEls.forEach((p, i) => {
    const r = p.getBoundingClientRect();
    const a = Math.max(0, Math.min(r.bottom, v.bottom) - Math.max(r.top, v.top));
    if (a > bestArea) { bestArea = a; best = i; }
  });
  return best;
}

async function addImageFile(file) {
  const url = await new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.onerror = rej; fr.readAsDataURL(file); });
  const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; });
  let src = url, w = img.naturalWidth, h = img.naturalHeight;
  const MAX = 2400;
  if (Math.max(w, h) > MAX || !/^data:image\/(png|jpeg)/.test(url)) {
    const k = Math.min(1, MAX / Math.max(w, h));
    const cv = document.createElement('canvas');
    cv.width = Math.round(w * k); cv.height = Math.round(h * k);
    cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
    src = /^data:image\/jpeg/.test(url) ? cv.toDataURL('image/jpeg', 0.9) : cv.toDataURL('image/png');
  }
  const n = visiblePage();
  const pg = S.doc.pages[n];
  const v = $('#viewer').getBoundingClientRect(), pr = pageEls[n].getBoundingClientRect();
  const cx = ((Math.max(v.top, pr.top) + Math.min(v.bottom, pr.bottom)) / 2 - pr.top) / S.zoom;
  let ww = Math.min(w * 0.75, pg.w * 0.4);
  let hh = ww * h / w;
  const asset = newId('a');
  S.assets[asset] = src;
  const x = (pg.w - ww) / 2, y = Math.max(10, Math.min(pg.h - hh - 10, cx - hh / 2));
  const o = { id: newId('i'), type: 'newimage', page: n, asset, rect: [x, y, x + ww, y + hh] };
  S.objects.push(o);
  renderObj(o);
  commit();
  select(o.id);
}

function deleteSelected() {
  const o = byId(S.selId);
  if (!o) return;
  if (S.editingId === o.id) { S.editingId = null; }
  if (o.type === 'block') { o.state = 'deleted'; for (const k of ['box', 'html', 'def', 'pristine']) delete o[k]; }
  else if (o.type === 'image') o.state = 'deleted';
  else S.objects.splice(S.objects.indexOf(o), 1);
  const el = els.get(o.id);
  if (el) el.remove();
  els.delete(o.id);
  if (S.objects.includes(o)) renderObj(o);
  S.selId = null;
  commit();
  renderSelection();
  updateToolbar();
}

function nudge(dx, dy) {
  const o = byId(S.selId);
  if (!o) return;
  if (o.type === 'block' && o.state === 'orig') activateBlock(o);
  if (o.box) { o.box.x += dx; o.box.y += dy; if (o.type === 'block') o.pristine = false; }
  else if (o.rect) {
    if (o.type === 'image' && o.state === 'orig') { o.state = 'edited'; renderObj(o); }
    o.rect = [o.rect[0] + dx, o.rect[1] + dy, o.rect[2] + dx, o.rect[3] + dy];
  } else return;
  placeObj(o);
  renderSelection();
  clearTimeout(nudge._t);
  nudge._t = setTimeout(commit, 400);
}

// ---------------------------------------------------------------------------
// Export : mesure exacte de la position de chaque mot affiché
// ---------------------------------------------------------------------------

function measureText(o) {
  const el = els.get(o.id);
  const c = el && el.firstChild;
  if (!c) return [];
  const z = S.zoom;
  const pr = pageEls[o.page].getBoundingClientRect();
  const chars = [];
  const range = document.createRange();
  const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const sp = n.parentElement.closest('[data-f]');
    const st = readStyle(sp && c.contains(sp) ? sp : c);
    for (let i = 0; i < n.data.length; i++) {
      const ch = n.data[i];
      if (ch === '​' || ch === M_START || ch === M_END) continue;
      if (/\s/.test(ch)) { chars.push({ space: true }); continue; }
      // caractères hors BMP (emoji…) : on garde la paire
      let len = 1;
      const code = n.data.charCodeAt(i);
      if (code >= 0xd800 && code <= 0xdbff && i + 1 < n.data.length) len = 2;
      range.setStart(n, i); range.setEnd(n, i + len);
      const rs = range.getClientRects();
      if (!rs.length) { i += len - 1; continue; }
      chars.push({ n, i, ch: n.data.substr(i, len), r: rs[rs.length - 1], st });
      i += len - 1;
    }
  }
  const lines = [];
  let prev = null, seg = null, gap = false;
  for (const ch of chars) {
    if (ch.space) { gap = true; continue; }
    const newLine = !prev || (ch.r.left < prev.r.right - 1 && ch.r.bottom > prev.r.bottom + 1);
    if (newLine) lines.push({ first: ch, segs: [] });
    const line = lines[lines.length - 1];
    const lsPx = Math.abs(ch.st.ls || 0) * z;
    const contiguous = prev && !newLine && !gap && Math.abs(ch.r.left - prev.r.right) <= 1 + lsPx;
    if (seg && contiguous && sameStyle(seg.st, ch.st)) {
      seg.t += ch.ch;
    } else {
      seg = { t: ch.ch, st: ch.st, x: (ch.r.left - pr.left) / z };
      line.segs.push(seg);
    }
    prev = ch;
    gap = false;
  }
  // Ligne de base de chaque ligne, à partir du premier caractère de la ligne
  for (const line of lines) line.y = (line.first.r.top + baselineOffset(line.first.st) - pr.top) / z;
  const out = [];
  for (const line of lines) {
    for (const s of line.segs) {
      out.push({ t: s.t, f: s.st.f, s: s.st.s, c: s.st.c, ls: s.st.ls || 0, x: round(s.x, 3), y: round(line.y, 3), fb: S.broken.has(s.st.f) });
    }
  }
  return out;
}

function buildOps() {
  const pages = S.doc.pages.map((_, n) => ({ n, removeBlocks: [], removeImages: [], items: [] }));
  for (const o of S.objects) {
    const P = pages[o.page];
    if (o.type === 'block') {
      if (o.state === 'orig') continue;
      P.removeBlocks.push(o.id);
      if (o.state === 'edited') P.items.push({ type: 'text', segments: measureText(o) });
    } else if (o.type === 'image') {
      if (o.state === 'orig') continue;
      P.removeImages.push({ xref: o.xref, k: o.k });
      if (o.state === 'edited') P.items.push({ type: 'image', xref: o.xref, rect: o.rect });
    } else if (o.type === 'text') {
      P.items.push({ type: 'text', segments: measureText(o) });
    } else if (o.type === 'newimage') {
      P.items.push({ type: 'image', src: S.assets[o.asset], rect: o.rect });
    } else if (o.type === 'shape') {
      P.items.push({ type: 'shape', shape: o.shape, rect: o.rect, stroke: o.stroke, fill: o.fill, sw: o.sw, opacity: o.opacity });
    }
  }
  return { pages: pages.filter((p) => p.removeBlocks.length || p.removeImages.length || p.items.length) };
}

async function download() {
  stopEditing();
  const keepSel = S.selId;
  select(null);
  busy('Création du PDF…');
  try {
    await document.fonts.ready;
    const ops = buildOps();
    const r = await fetch(`/api/doc/${S.doc.id}/export`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(ops) });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || 'Erreur serveur'); }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = S.doc.name.replace(/\.pdf$/i, '') + ' (modifié).pdf';
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast('PDF téléchargé ✔');
  } catch (err) {
    toast(err.message, true, 6000);
  } finally {
    busy(null);
    if (byId(keepSel)) select(keepSel);
  }
}

// ---------------------------------------------------------------------------
// Zoom
// ---------------------------------------------------------------------------

function setZoom(z, keepCenter = true) {
  z = Math.max(0.3, Math.min(4, z));
  const v = $('#viewer');
  const ry = (v.scrollTop + v.clientHeight / 2) / Math.max(1, v.scrollHeight);
  const rx = (v.scrollLeft + v.clientWidth / 2) / Math.max(1, v.scrollWidth);
  stopEditing();
  S.zoom = z;
  layoutPages();
  renderAll();
  if (keepCenter) {
    v.scrollTop = ry * v.scrollHeight - v.clientHeight / 2;
    v.scrollLeft = rx * v.scrollWidth - v.clientWidth / 2;
  }
}

function fitWidth() {
  const maxW = Math.max(...S.doc.pages.map((p) => p.w));
  return Math.min(1.5, ($("#viewer").clientWidth - 72) / maxW);
}

// ---------------------------------------------------------------------------
// OCR des pages scannées
// ---------------------------------------------------------------------------

async function runOCR() {
  for (let n = 0; n < S.doc.pages.length; n++) {
    const pg = S.doc.pages[n];
    if (!pg.scanned || pg.ocrDone) continue;
    const banner = document.createElement('div');
    banner.className = 'page-banner';
    banner.textContent = 'Reconnaissance du texte de cette page scannée…';
    pageEls[n].append(banner);
    try {
      const res = await api(`/api/doc/${S.doc.id}/ocr/${n}`, {});
      await ensureFonts(res.fonts);
      refreshFamilyList();
      pg.ocrDone = true;
      for (const b of res.blocks) {
        S.blocks[b.id] = b;
        if (!byId(b.id)) { const o = { id: b.id, type: 'block', page: n, state: 'orig' }; S.objects.push(o); renderObj(o); }
      }
      S.snap = snapshot();
      banner.textContent = `Texte reconnu : ${res.blocks.length} lignes modifiables`;
      setTimeout(() => banner.remove(), 2500);
    } catch (err) {
      banner.textContent = 'OCR impossible : ' + err.message;
      setTimeout(() => banner.remove(), 5000);
    }
  }
}

// ---------------------------------------------------------------------------
// Chargement
// ---------------------------------------------------------------------------

async function loadDoc(p, session) {
  S.doc = p;
  S.fonts = {};
  S.blocks = {};
  S.objects = [];
  S.assets = {};
  S.undo = []; S.redo = [];
  S.selId = null; S.editingId = null;
  busy('Chargement des polices…');
  await ensureFonts(p.fonts);
  const count = {};
  p.pages.forEach((pg, n) => {
    pg.images.forEach((im) => {
      if (im.editable) S.objects.push({ id: im.id, type: 'image', page: n, xref: im.xref, k: im.k, orig: im.rect, rect: [...im.rect], state: 'orig' });
    });
  });
  p.pages.forEach((pg) => pg.blocks.forEach((b) => {
    S.blocks[b.id] = b;
    S.objects.push({ id: b.id, type: 'block', page: b.page, state: 'orig' });
    b.paras.flat().forEach((r) => (count[r.f] = (count[r.f] || 0) + r.t.length));
  }));
  const top = Object.entries(count).sort((a, b) => b[1] - a[1])[0];
  S.defaults = { f: top ? top[0] : p.fonts[0]?.id, s: 12, c: '#000000' };
  if (!S.defaults.f) S.defaults.f = await getVariant('Arial', 400, false);
  if (session) {
    S.objects = session.objects;
    S.assets = session.assets || {};
    if (session.defaults && S.fonts[session.defaults.f]) S.defaults = session.defaults;
  }
  S.snap = snapshot();
  $('#start').hidden = true;
  $('#editor').hidden = false;
  $('#docname').textContent = p.name;
  $('#docname').title = p.name;
  document.title = p.name + ' — PDF Edit';
  refreshFamilyList();
  S.zoom = fitWidth();
  buildPages();
  renderAll();
  updateToolbar();
  busy(null);
  saveSession();
  runOCR();
}

async function uploadFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') { toast('Ce fichier n’est pas un PDF.', true); return; }
  if (S.doc && S.undo.length && !confirm('Ouvrir un autre PDF ? Les modifications non téléchargées du document actuel seront perdues.')) return;
  busy('Analyse du PDF…');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Erreur');
    await loadDoc(j);
  } catch (err) {
    busy(null);
    toast(err.message, true, 6000);
  }
}

async function resumeSession() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); } catch (e) { s = null; }
  if (!s) return;
  busy('Réouverture…');
  try {
    const p = await api(`/api/doc/${s.id}`);
    await loadDoc(p, s);
  } catch (err) {
    busy(null);
    localStorage.removeItem(SESSION_KEY);
    $('#resume').hidden = true;
    toast('Session précédente introuvable, ouvre à nouveau le PDF.', true);
  }
}

// ---------------------------------------------------------------------------
// Branchements
// ---------------------------------------------------------------------------

function wire() {
  const drop = $('#drop');
  $('#file').addEventListener('change', (e) => uploadFile(e.target.files[0]));
  ['dragenter', 'dragover'].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); if (t === 'drop' || e.target === document.documentElement) drop.classList.remove('over'); }));
  document.addEventListener('drop', (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (!f) return;
    if (/^image\//.test(f.type) && S.doc) addImageFile(f);
    else uploadFile(f);
  });

  try {
    const s = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null');
    if (s && s.id) { $('#resume').hidden = false; $('#resume').textContent = `Reprendre « ${s.name} »`; }
  } catch (e) { /* rien */ }
  $('#resume').addEventListener('click', resumeSession);

  $('#btn-new').addEventListener('click', () => $('#file').click());
  $$('#tools .tool').forEach((b) => b.addEventListener('click', () => setTool(b.dataset.tool)));
  $('#img-file').addEventListener('change', (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) addImageFile(f); });
  $('#btn-undo').addEventListener('click', undo);
  $('#btn-redo').addEventListener('click', redo);
  $('#zoom-in').addEventListener('click', () => setZoom(S.zoom * 1.2));
  $('#zoom-out').addEventListener('click', () => setZoom(S.zoom / 1.2));
  $('#zoom-val').addEventListener('click', () => setZoom(fitWidth()));
  $('#btn-download').addEventListener('click', download);
  $('#btn-delete').addEventListener('click', deleteSelected);

  // Les boutons de la barre ne doivent pas voler le focus du texte en cours d'édition
  $$('.bar .btn').forEach((b) => b.addEventListener('mousedown', (e) => e.preventDefault()));

  $('#f-family').addEventListener('change', (e) => applyTextStyle('family', e.target.value));
  $('#f-size').addEventListener('change', (e) => { const v = parseFloat(e.target.value); if (v > 0) applyTextStyle('size', v); });
  $('#f-color').addEventListener('input', (e) => { e.target.parentElement.querySelector('.swatch').style.background = e.target.value; });
  $('#f-color').addEventListener('change', (e) => applyTextStyle('color', e.target.value));
  $('#f-bold').addEventListener('click', () => applyTextStyle('bold'));
  $('#f-italic').addEventListener('click', () => applyTextStyle('italic'));
  $$('[data-align]').forEach((b) => b.addEventListener('click', () => setBlockProp('align', b.dataset.align)));
  $('#f-lh').addEventListener('change', (e) => { const v = parseFloat(e.target.value); if (v > 0) setBlockProp('lh', v); });

  $('#s-stroke').addEventListener('input', (e) => applyShapeProp('stroke', e.target.value));
  $('#s-fill').addEventListener('input', (e) => applyShapeProp('fill', e.target.value));
  $('#s-stroke-none').addEventListener('click', () => applyShapeProp('stroke', null));
  $('#s-fill-none').addEventListener('click', () => applyShapeProp('fill', null));
  $('#s-width').addEventListener('change', (e) => applyShapeProp('sw', Math.max(0.25, parseFloat(e.target.value) || 1)));
  $('#s-opacity').addEventListener('input', (e) => applyShapeProp('opacity', parseFloat(e.target.value)));

  const pages = $('#pages');
  pages.addEventListener('pointerdown', onPagesPointerDown);
  $('#viewer').addEventListener('pointerdown', (e) => { if (e.target.id === 'viewer' || e.target.id === 'pages') { stopEditing(); select(null); } });
  pages.addEventListener('input', onContentInput);
  pages.addEventListener('compositionend', onContentInput);
  pages.addEventListener('paste', (e) => {
    if (!e.target.closest('.tcontent')) return;
    e.preventDefault();
    const t = (e.clipboardData.getData('text/plain') || '').replace(/\r\n?/g, '\n');
    document.execCommand('insertText', false, t);
  });
  pages.addEventListener('dragstart', (e) => e.preventDefault());

  document.addEventListener('selectionchange', () => {
    if (!S.editingId) return;
    const c = contentOf(S.editingId);
    const sel = getSelection();
    if (c && sel.rangeCount && c.contains(sel.anchorNode)) {
      S.savedRange = sel.getRangeAt(0).cloneRange();
      updateToolbar();
    }
  });

  $('#viewer').addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    clearTimeout(setZoom._t);
    const target = S.zoom * Math.exp(-e.deltaY * 0.01);
    setZoom._t = setTimeout(() => setZoom(target), 60);
  }, { passive: false });

  document.addEventListener('keydown', (e) => {
    if ($('#editor').hidden) return;
    const mod = e.metaKey || e.ctrlKey;
    const inField = e.target.matches('input, select, textarea');
    const k = e.key.toLowerCase();
    if (mod && k === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if (mod && k === 'y') { e.preventDefault(); redo(); return; }
    if (mod && k === 's') { e.preventDefault(); download(); return; }
    if (S.editingId) {
      if (mod && k === 'b') { e.preventDefault(); applyTextStyle('bold'); }
      else if (mod && k === 'i') { e.preventDefault(); applyTextStyle('italic'); }
      else if (mod && k === 'u') { e.preventDefault(); }
      else if (e.key === 'Escape') { e.preventDefault(); stopEditing(); }
      return;
    }
    if (inField) return;
    if (e.key === 'Escape') { if (S.tool !== 'select') setTool('select'); else select(null); return; }
    if ((e.key === 'Delete' || e.key === 'Backspace') && S.selId) { e.preventDefault(); deleteSelected(); return; }
    if (e.key === 'Enter' && isTextLike(byId(S.selId))) { e.preventDefault(); const o = byId(S.selId); if (o.state === 'orig') activateBlock(o); startEditing(o); return; }
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[e.key] && S.selId) { e.preventDefault(); const m = e.shiftKey ? 10 : 1; nudge(arrows[e.key][0] * m, arrows[e.key][1] * m); return; }
    if (!mod && k === 'v') setTool('select');
    if (!mod && k === 't') setTool('text');
  });

  window.addEventListener('beforeunload', (e) => { if (S.doc && S.undo.length) { e.preventDefault(); e.returnValue = ''; } });
  window.addEventListener('resize', () => { if (S.selId) renderSelection(); });
}

wire();
