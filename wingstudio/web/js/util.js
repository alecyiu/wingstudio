/* Wing Studio - shared helpers (DOM builder, formatters, small utilities).
   Everything hangs off one global namespace, window.AS, so the files can be
   plain <script> tags with no build step. */
(function () {
  'use strict';
  const AS = (window.AS = window.AS || {});

  /* h('div', {class: 'x', onclick: fn, dataset: {stage: 'mesh'}}, child, ...) -> element.
     Children may be strings, numbers, nodes, arrays, or null/false (skipped). */
  AS.h = function (tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected' || k === 'hidden') el[k] = v;
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    AS.append(el, kids);
    return el;
  };

  AS.append = function (el, kids) {
    for (const kid of kids) {
      if (kid == null || kid === false) continue;
      if (Array.isArray(kid)) AS.append(el, kid);
      else el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  };

  /* Line icons on a 16 px grid, drawn with the current text colour. AS.icon('check') -> <svg>.
     Shapes with class "f" are filled; everything else is a 1.6 px stroke (see .ic in styles.css). */
  const ICONS = {
    check: '<path d="M3.2 8.6l3.1 3.1 6.5-7.2"/>',
    x: '<path d="M4 4l8 8M12 4l-8 8"/>',
    pause: '<path d="M5.6 3.5v9M10.4 3.5v9"/>',
    play: '<path class="f" d="M5 3.2v9.6a.5.5 0 0 0 .77.42l7.3-4.8a.5.5 0 0 0 0-.84l-7.3-4.8A.5.5 0 0 0 5 3.2z"/>',
    stop: '<rect class="f" x="4" y="4" width="8" height="8" rx="1.2"/>',
    redo: '<path d="M2.6 8a5.4 5.4 0 1 0 1.7-3.93"/><path d="M2.4 2.6v3.3h3.3"/>',
    'chevrons-left': '<path d="M7.5 4l-4 4 4 4M12.5 4l-4 4 4 4"/>',
    'chevrons-right': '<path d="M3.5 4l4 4-4 4M8.5 4l4 4-4 4"/>',
    'chevron-up': '<path d="M4 10l4-4 4 4"/>',
    'chevron-down': '<path d="M4 6l4 4 4-4"/>',
    download: '<path d="M8 2.5v8M4.6 7.4L8 10.8l3.4-3.4M3 13.5h10"/>',
    image: '<rect x="2.5" y="3" width="11" height="10" rx="1.5"/><circle cx="6" cy="6.5" r="1"/><path d="M13.5 10.2l-2.9-2.9L4.2 13"/>',
    'arrow-right': '<path d="M3 8h10M9.2 4.2L13 8l-3.8 3.8"/>',
    'arrow-down': '<path d="M8 3v10M4.2 9.2L8 13l3.8-3.8"/>',
    alert: '<path d="M8 2.4l6.2 10.9H1.8z"/><path d="M8 6.4v3.2M8 11.5v.1"/>',
    'sort-up': '<path class="f" d="M8 5l3.4 5H4.6z"/>',
    'sort-down': '<path class="f" d="M8 11L4.6 6h6.8z"/>',
    /* citations (js/cite.js): an open book beside a cited value, and the mark of a link that leaves the studio */
    ref: '<path d="M8 4.6L6.2 3.4H2.4V12h3.8L8 13.2 9.8 12h3.8V3.4H9.8zM8 4.6v8.6"/>',
    external: '<path d="M9 3h4v4M13 3L7.6 8.4M11 9.6V13H3V5h3.4"/>',
    /* the product mark: a cambered section with its chord line */
    wing: '<path class="f" d="M14.7 10.3C11.9 7.6 8.7 5.3 5.6 5.3 3.1 5.3 1.4 6.6 1.4 8.4c0 1.5 1.4 2.3 3.4 2.3 3.1 0 6.5-.2 9.9-.4z"/>',
  };
  AS.icon = function (name, size) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const px = size || 14;
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', px); svg.setAttribute('height', px);
    svg.setAttribute('class', 'ic ic-' + name);
    svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    svg.innerHTML = ICONS[name] || '';
    return svg;
  };

  AS.clear = function (el) { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  AS.$ = (sel, root) => (root || document).querySelector(sel);
  AS.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  AS.laidOut = (el) => !!el && el.isConnected && el.getClientRects().length > 0;
  // the reason prefix of a plan value the engineer set by hand (planning.ENGINEER_MARK)
  AS.ENGINEER_MARK = 'Set by the engineer';
  AS.engineerSet = (section, name) => String(((section || {}).reasons || {})[name] || '').startsWith(AS.ENGINEER_MARK);

  /* Rebuild `el` only when the signature of its inputs changed. Rebuilding on
     every event would destroy text selections (needed for quote-to-chat),
     scroll positions and focus. */
  AS.region = function (el, signature, build) {
    const sig = typeof signature === 'string' ? signature : JSON.stringify(signature);
    if (el.__sig === sig) return false;
    el.__sig = sig;
    const active = document.activeElement;
    const key = active && active !== el && el.contains(active) ? focusKey(active) : null;
    AS.clear(el);
    AS.append(el, [build()]);
    if (key) refocus(el, key);
    return true;
  };

  /* Most controls sit in a region that their own click rebuilds, so the focused button is replaced
     and the keyboard would be left on <body>. The new control is found by a name that survives the
     rebuild: its id, then each of FOCUS_ATTRS (Pause and Resume differ in id but share a data-key).
     Never by position: when the set of buttons changes, an index can land on a different control. */
  const FOCUS_ATTRS = ['data-tab', 'data-preset', 'data-vtab', 'data-key'];
  function focusKey(node) {
    const names = node.id ? [['id', node.id]] : [];
    for (const a of FOCUS_ATTRS) if (node.hasAttribute(a)) names.push([a, node.getAttribute(a)]);
    return names.length ? names : null;
  }
  function refocus(el, names) {
    const lost = !document.activeElement || document.activeElement === document.body;
    if (!lost) return;
    for (const [attr, value] of names) {
      const found = AS.$$('[' + attr + ']', el).filter((n) => n.getAttribute(attr) === value);
      if (!found.length) continue;
      // one unambiguous, usable match or nothing: focus is never handed to a different control
      if (found.length === 1 && !found[0].disabled) found[0].focus({ preventScroll: true });
      return;
    }
  }

  // ---------- formatting ----------
  const DASH = '–';
  AS.isNum = (v) => typeof v === 'number' && isFinite(v);

  AS.fmt = function (v, digits) {
    if (!AS.isNum(v)) return v == null || v === '' ? DASH : String(v);
    if (digits != null) return v.toFixed(digits);
    const a = Math.abs(v);
    if (a === 0) return '0';
    if (a >= 1e5 || a < 1e-3) return AS.fmtSci(v);
    if (a >= 100) return v.toLocaleString('en-US', { maximumFractionDigits: 1 });
    return String(parseFloat(v.toPrecision(4)));
  };

  AS.fmtSci = function (v, sig) {
    if (!AS.isNum(v)) return DASH;
    if (v === 0) return '0';
    const [m, e] = v.toExponential((sig || 3) - 1).split('e');
    return parseFloat(m) + 'e' + parseInt(e, 10);
  };

  AS.fmtInt = (v) => (AS.isNum(v) ? Math.round(v).toLocaleString('en-US') : DASH);
  AS.fmtPct = (v, digits) => (AS.isNum(v) ? (v > 0 ? '+' : '') + v.toFixed(digits == null ? 1 : digits) + ' %' : DASH);

  AS.fmtDur = function (seconds) {
    if (!AS.isNum(seconds) || seconds < 0) return DASH;
    const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const two = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${two(m)}:${two(r)}` : `${two(m)}:${two(r)}`;
  };

  AS.fmtSeconds = function (s) {
    if (!AS.isNum(s)) return DASH;
    if (s < 10) return s.toFixed(1) + ' s';
    if (s < 90) return Math.round(s) + ' s';
    return AS.fmtDur(s);
  };

  AS.fmtTokens = function (n) {
    if (!AS.isNum(n)) return DASH;
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
    return String(Math.round(n));
  };

  /* Share (0 to 1) of the input tokens that were read from the prompt cache; tokens_cached is part
     of tokens_in. null when the counts are missing or there was no input, as for the scripted model. */
  AS.cachedShare = function (cached, input) {
    if (!AS.isNum(cached) || !AS.isNum(input) || input <= 0) return null;
    return Math.max(0, Math.min(1, cached / input));
  };

  AS.fmtCost = function (usd) {
    if (!AS.isNum(usd)) return DASH;
    if (usd === 0) return '$0.00';
    return '$' + (usd < 0.01 ? usd.toFixed(4) : usd < 1 ? usd.toFixed(3) : usd.toFixed(2));
  };

  AS.fmtClock = function (ts) {
    const d = new Date(ts);
    return isNaN(d) ? '' : d.toLocaleTimeString('en-GB', { hour12: false });
  };

  AS.fmtDate = function (ts) {
    const d = new Date(ts);
    if (isNaN(d)) return DASH;
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
      d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
  };

  /* Any plan or metric value -> short readable text. */
  AS.fmtValue = function (v) {
    if (v == null) return DASH;
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (typeof v === 'number') return AS.fmt(v);
    if (Array.isArray(v)) return v.map(AS.fmtValue).join(', ');
    if (typeof v === 'object') return Object.entries(v).map(([k, x]) => `${k} ${AS.fmtValue(x)}`).join(' · ');
    return String(v);
  };

  AS.humanKey = (k) => String(k).replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2');

  /* The title of a study as shown to the reader. A title is the start of the prompt; one that was
     taken as the first sentence stops at the decimal point of "Mach 0.15" and would state Mach 0.
     Where the prompt is at hand, the rest of that number is put back. */
  AS.studyTitle = function (study) {
    const title = study && typeof study.title === 'string' ? study.title : '';
    const prompt = study && typeof study.prompt === 'string' ? study.prompt.trim() : '';
    if (/\d$/.test(title) && prompt.startsWith(title)) {
      const rest = /^\.\d+/.exec(prompt.slice(title.length));
      if (rest) return title + rest[0];
    }
    return title;
  };

  // ---------- model names ----------
  AS.modelId = (m) => (typeof m === 'string' ? m : '');
  AS.modelLabel = function (m) {
    const id = AS.modelId(m);
    if (!id) return DASH;
    if (id === 'scripted') return 'Scripted';
    const tail = id.split('/').pop().replace(/^claude-/, '');
    const name = tail.split('-')[0];
    const version = tail.split('-').slice(1).join('.');
    return name.charAt(0).toUpperCase() + name.slice(1) + (version ? ' ' + version : '');
  };
  /* Fixed colour per model so a model keeps its colour in every chart. */
  AS.modelColor = function (m) {
    const id = AS.modelId(m).toLowerCase();
    if (id.includes('haiku')) return 'var(--s1)';
    if (id.includes('sonnet')) return 'var(--s2)';
    if (id.includes('opus')) return 'var(--s3)';
    if (id === 'scripted') return 'var(--s-neutral)';
    return 'var(--s7)';
  };

  AS.OBJECTIVES = { max_ld: 'Max L/D at design Cl', max_endurance: 'Max Cl^1.5/Cd (endurance)', min_cd: 'Min Cd at design Cl' };

  // ---------- misc ----------
  AS.debounce = function (fn, ms) {
    let t = null;
    return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
  };
  AS.clone = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));

  /* The reference catalogue: the id pattern of wingstudio/refs/catalogue.json, the key of an
     indexed passage (wingstudio/refs/docs/<id>.json) and the names of the kinds of source. */
  AS.REF = {
    ID: /^[a-z0-9]+(-[a-z0-9]+)*$/,
    PASSAGE: /^[a-z0-9-]+$/,
    KIND: new Map([['paper', 'paper'], ['book', 'book'], ['documentation', 'documentation'], ['project-measurement', 'measured by the studio'], ['project-rule', 'studio rule']]),
  };
  /* A catalogue entry's address, as a link only when the catalogue marks it verified and it is https. */
  AS.verifiedUrl = (e) => (e.url_verified === true && typeof e.url === 'string' && /^https:\/\//.test(e.url) ? e.url : '');
  AS.urlHost = (url) => { try { return new URL(url).hostname || 'original'; } catch (e) { return 'original'; } };
  AS.median = function (values) {
    const v = values.filter(AS.isNum).sort((a, b) => a - b);
    if (!v.length) return null;
    const mid = v.length >> 1;
    return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
  };
  AS.sum = (values) => values.filter(AS.isNum).reduce((a, b) => a + b, 0);
  AS.truthy = (v) => v === true || v === 1 || v === '1' || v === 'true';

  AS.store = {
    get(key, fallback) {
      try { const v = localStorage.getItem('as.' + key); return v == null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
    },
    set(key, value) { try { localStorage.setItem('as.' + key, JSON.stringify(value)); } catch (e) { /* private mode */ } },
  };

  AS.toast = function (message, kind) {
    const box = AS.$('#toasts');
    if (!box) return;
    const el = AS.h('div', { class: 'toast ' + (kind || 'info'), role: 'status', title: 'Click to dismiss', onclick: () => el.remove() }, message);
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3500);
  };

  /* NACA 4-digit outline (candidate thumbnails, the mock's geometry).
     Same equations as wingstudio/cfd/case.py (closed trailing edge). */
  AS.naca4 = function (code, n) {
    code = String(code).padStart(4, '0');
    const m = parseInt(code[0], 10) / 100, p = parseInt(code[1], 10) / 10, t = parseInt(code.slice(2), 10) / 100;
    const upper = [], lower = [];
    n = n || 60;
    for (let i = 0; i <= n; i++) {
      const x = 0.5 * (1 - Math.cos(Math.PI * i / n));
      const yt = 5 * t * (0.2969 * Math.sqrt(x) - 0.126 * x - 0.3516 * x * x + 0.2843 * x ** 3 - 0.1036 * x ** 4);
      let yc = 0, dyc = 0;
      if (m > 0 && p > 0) {
        if (x < p) { yc = m / (p * p) * (2 * p * x - x * x); dyc = 2 * m / (p * p) * (p - x); }
        else { yc = m / ((1 - p) ** 2) * (1 - 2 * p + 2 * p * x - x * x); dyc = 2 * m / ((1 - p) ** 2) * (p - x); }
      }
      const th = Math.atan(dyc);
      upper.push([x - yt * Math.sin(th), yc + yt * Math.cos(th)]);
      lower.push([x + yt * Math.sin(th), yc - yt * Math.cos(th)]);
    }
    return { upper, lower };
  };

  /* Accepts [[x,y],...], [{x, cp|y|z}, ...] or {x:[...], cp|y|z:[...]} and returns [[x,y],...]. */
  AS.toXY = function (v) {
    if (!v) return [];
    const yOf = (o) => (o.cp != null ? o.cp : o.y != null ? o.y : o.z != null ? o.z : o.value);
    if (Array.isArray(v)) {
      return v.map((p) => (Array.isArray(p) ? [Number(p[0]), Number(p[1])] : [Number(p.x), Number(yOf(p))]))
        .filter((p) => isFinite(p[0]) && isFinite(p[1]));
    }
    if (Array.isArray(v.x)) {
      const ys = v.cp || v.y || v.z || v.values || [];
      return v.x.map((x, i) => [Number(x), Number(ys[i])]).filter((p) => isFinite(p[0]) && isFinite(p[1]));
    }
    return [];
  };
})();
