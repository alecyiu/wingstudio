/* Wing Studio - small SVG chart kit (line, parity scatter, bars).
   Written here instead of vendoring a library so the UI works offline with no
   third-party code. Colours come from the CSS variables of styles.css.
   One y-axis per chart, always. */
(function () {
  'use strict';
  const AS = window.AS;
  const NS = 'http://www.w3.org/2000/svg';

  function s(tag, attrs, text) {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs) if (attrs[k] != null) el.setAttribute(k, attrs[k]);
    if (text != null) el.textContent = text;
    return el;
  }

  // "Nice" linear ticks covering [lo, hi].
  function linearTicks(lo, hi, count) {
    if (!(hi > lo)) { hi = lo + 1; }
    const raw = (hi - lo) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
    const ticks = [];
    for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-9; v += step) ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return { ticks, step };
  }

  function tickLabel(v, step) {
    const a = Math.abs(v);
    if (a >= 1e5 || (a > 0 && a < 1e-4)) return AS.fmtSci(v, 2);
    if (a >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 0 });
    const decimals = Math.max(0, Math.min(6, -Math.floor(Math.log10(step) + 1e-9)));
    return v.toFixed(decimals);
  }

  function extent(values) {
    let lo = Infinity, hi = -Infinity;
    for (const v of values) { if (v < lo) lo = v; if (v > hi) hi = v; }
    return [lo, hi];
  }

  function padded(lo, hi, frac) {
    if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
    if (lo === hi) { const d = Math.abs(lo) * 0.1 || 1; return [lo - d, hi + d]; }
    const d = (hi - lo) * frac;
    return [lo - d, hi + d];
  }

  /* Dots of several series on one spot: for each point, how many series drawn later have a point
     within tol px of it (over: that many dots lie on top of it) and how many series in all share
     the spot (others). pts[i] is the list of [px, py] of series i, in drawing order; the result has
     the same shape. Pixels, not values, decide: two dots less than a pixel apart hide each other
     just as two equal values do. */
  function coincident(pts, tol) {
    const near = (list, p) => list.some((q) => Math.abs(q[0] - p[0]) <= tol && Math.abs(q[1] - p[1]) <= tol);
    return pts.map((list, i) => list.map((p) => {
      let over = 0, others = 0;
      for (let j = 0; j < pts.length; j++) {
        if (j === i || !near(pts[j], p)) continue;
        others++;
        if (j > i) over++;
      }
      return { over, others };
    }));
  }
  const DOT_R = 2.5;                    // the dot of a series with `dots`
  const RING_STEP = 2.2, RING_W = 1.3;  // a ring every 2.2 px: just under a pixel of ground shows between two of them

  class Chart {
    constructor(container, kind) {
      this.el = container;
      this.kind = kind;
      this.opts = null;
      this.frame = 0;
      container.classList.add('chart');
      this.legend = AS.h('div', { class: 'chart-legend' });
      this.plot = AS.h('div', { class: 'chart-plot' });
      this.tip = AS.h('div', { class: 'chart-tip', hidden: true });
      AS.append(container, [this.legend, this.plot, this.tip]);
      // Redraw when the pane is resized or becomes visible again.
      this.observer = new ResizeObserver(() => this.schedule());
      this.observer.observe(this.plot);
      // a change of the window size redraws too, also where the observer is not served (a covered window)
      this.onResize = () => this.schedule();
      window.addEventListener('resize', this.onResize);
    }
    update(opts) { this.opts = opts; this.schedule(); }
    schedule() {
      if (this.frame) return;
      const run = () => { cancelAnimationFrame(this.frame); clearTimeout(this.timer); this.frame = 0; this.timer = 0; this.draw(); };
      this.frame = requestAnimationFrame(run);
      this.timer = setTimeout(run, 80);   // animation frames pause while the window is covered; see AS.invalidate
    }
    destroy() { this.observer.disconnect(); window.removeEventListener('resize', this.onResize); cancelAnimationFrame(this.frame); clearTimeout(this.timer); this.frame = 0; AS.clear(this.el); }

    draw() {
      const o = this.opts;
      const W = this.plot.clientWidth, H = this.plot.clientHeight;
      if (!o || W < 40 || H < 40) return;
      AS.clear(this.plot);
      this.tip.hidden = true;
      this.drawLegend(o);
      const svg = s('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': o.title || o.yLabel || 'chart' });
      this.plot.appendChild(svg);
      const box = { l: 56, r: 14, t: 8, b: o.xLabel ? 34 : 22 };
      box.w = W - box.l - box.r; box.h = H - box.t - box.b;
      if (this.kind === 'line') this.drawLine(svg, o, box);
      else if (this.kind === 'scatter') this.drawScatter(svg, o, box);
      else this.drawBars(svg, o, box);
    }

    drawLegend(o) {
      AS.clear(this.legend);
      const entries = o.legend || (this.kind === 'line' && o.series.length > 1
        ? o.series.filter((x) => !x.noLegend).map((x) => ({ name: x.name, color: x.color, dash: x.dash })) : []);
      for (const e of entries) {
        this.legend.appendChild(AS.h('span', { class: 'chart-key' },
          AS.h('i', { class: 'chart-swatch' + (e.dash ? ' dashed' : ''), style: { color: e.color } }), e.name));
      }
      this.legend.hidden = entries.length === 0;
    }

    empty(svg, box, text) {
      svg.appendChild(s('text', { x: box.l + box.w / 2, y: box.t + box.h / 2, class: 'chart-empty', 'text-anchor': 'middle' }, text || 'No data yet'));
    }

    axes(svg, box, x, y, o) {
      const g = s('g', { class: 'chart-axes' });
      for (const t of y.ticks) {
        const py = y.px(t.v);
        g.appendChild(s('line', { x1: box.l, x2: box.l + box.w, y1: py, y2: py, class: 'chart-grid' }));
        g.appendChild(s('text', { x: box.l - 6, y: py + 3.5, 'text-anchor': 'end', class: 'chart-tick' }, t.label));
      }
      for (const t of x.ticks) {
        const px = x.px(t.v);
        g.appendChild(s('line', { x1: px, x2: px, y1: box.t, y2: box.t + box.h, class: 'chart-grid' }));
        g.appendChild(s('line', { x1: px, x2: px, y1: box.t + box.h, y2: box.t + box.h + 4, class: 'chart-axis' }));
        g.appendChild(s('text', { x: px, y: box.t + box.h + 15, 'text-anchor': 'middle', class: 'chart-tick' }, t.label));
      }
      // the plot area is a closed frame, as in an engineering plot window
      g.appendChild(s('rect', { x: box.l, y: box.t, width: box.w, height: box.h, fill: 'none', class: 'chart-axis chart-frame' }));
      if (o.xLabel) g.appendChild(s('text', { x: box.l + box.w / 2, y: box.t + box.h + 30, 'text-anchor': 'middle', class: 'chart-label' }, o.xLabel));
      if (o.yLabel) {
        g.appendChild(s('text', { x: 12, y: box.t + box.h / 2, 'text-anchor': 'middle', class: 'chart-label',
          transform: `rotate(-90 12 ${box.t + box.h / 2})` }, o.yLabel));
      }
      svg.appendChild(g);
    }

    linearScale(lo, hi, pxLo, pxHi, count) {
      const { ticks, step } = linearTicks(lo, hi, count);
      return { px: (v) => pxLo + (v - lo) / (hi - lo) * (pxHi - pxLo), inv: (p) => lo + (p - pxLo) / (pxHi - pxLo) * (hi - lo),
        ticks: ticks.filter((v) => v >= lo - 1e-12 && v <= hi + 1e-12).map((v) => ({ v, label: tickLabel(v, step) })) };
    }

    logScale(lo, hi, pxLo, pxHi) {
      const a = Math.floor(Math.log10(lo)), b = Math.ceil(Math.log10(hi));
      const e0 = a === b ? a - 1 : a, e1 = b;
      const ticks = [];
      const every = Math.max(1, Math.ceil((e1 - e0) / 7));
      for (let e = e0; e <= e1; e += every) ticks.push({ v: Math.pow(10, e), label: '1e' + e });
      return { px: (v) => pxLo + (Math.log10(Math.max(v, 1e-300)) - e0) / (e1 - e0) * (pxHi - pxLo), ticks };
    }

    // ---------- line chart ----------
    drawLine(svg, o, box) {
      const series = o.series.map((x) => ({ ...x, points: (x.points || []).filter((p) => isFinite(p[0]) && isFinite(p[1]) && (!o.yLog || p[1] > 0)) }))
        .filter((x) => x.points.length);
      if (!series.length) { this.empty(svg, box, o.emptyText); return; }
      const xs = [], ys = [];
      for (const sr of series) for (const p of sr.points) { xs.push(p[0]); ys.push(p[1]); }
      for (const r of o.refs || []) if (AS.isNum(r.y) && (!o.yLog || r.y > 0)) ys.push(r.y);
      let [x0, x1] = extent(xs);
      if (o.xMin != null) x0 = o.xMin;
      if (o.xMax != null) x1 = Math.max(x1, o.xMax);
      if (x0 === x1) x1 = x0 + 1;
      const x = this.linearScale(x0, x1, box.l, box.l + box.w, Math.max(3, Math.floor(box.w / 80)));
      // xTicks, yTicks: [{ v, label }] given by the caller where the nice ticks do not fit the quantity
      // (an integer count would get 1.0, 1.5, 2.0; a share needs its unit in the label)
      if (o.xTicks) x.ticks = o.xTicks.filter((t) => t.v >= x0 && t.v <= x1);
      let y;
      if (o.yLog) {
        const [lo, hi] = extent(ys);
        y = this.logScale(lo, hi, box.t + box.h, box.t);
      } else {
        let [lo, hi] = padded(...extent(ys), 0.08);
        if (o.yMin != null) lo = o.yMin;
        if (o.yMax != null) hi = o.yMax;
        // yInvert: negative values at the top (pressure-coefficient convention).
        y = o.yInvert ? this.linearScale(lo, hi, box.t, box.t + box.h, Math.max(3, Math.floor(box.h / 34)))
          : this.linearScale(lo, hi, box.t + box.h, box.t, Math.max(3, Math.floor(box.h / 34)));
        if (o.yTicks) y.ticks = o.yTicks.filter((t) => t.v >= lo && t.v <= hi);
      }
      this.axes(svg, box, x, y, o);

      for (const r of o.refs || []) {
        if (!AS.isNum(r.y) || (o.yLog && r.y <= 0)) continue;
        const py = y.px(r.y);
        if (py < box.t - 1 || py > box.t + box.h + 1) continue;
        svg.appendChild(s('line', { x1: box.l, x2: box.l + box.w, y1: py, y2: py, class: 'chart-ref' }));
        if (r.label) svg.appendChild(s('text', { x: box.l + box.w - 4, y: py - 4, 'text-anchor': 'end', class: 'chart-ref-label' }, r.label));
      }
      const clipId = 'clip' + Math.random().toString(36).slice(2);
      const clip = s('clipPath', { id: clipId });
      clip.appendChild(s('rect', { x: box.l, y: box.t - 2, width: box.w, height: box.h + 4 }));
      svg.appendChild(clip);
      const marks = s('g', { 'clip-path': `url(#${clipId})` });
      svg.appendChild(marks);
      for (const sr of series) {
        let d = '';
        for (let i = 0; i < sr.points.length; i++) {
          d += (i ? 'L' : 'M') + x.px(sr.points[i][0]).toFixed(1) + ' ' + y.px(sr.points[i][1]).toFixed(1);
        }
        marks.appendChild(s('path', { d, fill: 'none', class: 'chart-line' + (sr.muted ? ' muted' : ''),
          style: `stroke:${sr.color}`, 'stroke-width': sr.width || 1.6, 'stroke-dasharray': sr.dash ? '5 4' : null }));
        if (sr.dots) for (const p of sr.points) marks.appendChild(s('circle', { cx: x.px(p[0]), cy: y.px(p[1]), r: DOT_R, style: `fill:${sr.color}` }));
      }
      // rings: where the dots of several series lie on one spot only the last one drawn shows. Each
      // series under it then gets an unfilled circle in its own colour around that dot, one step
      // larger per series above it, so every colour stays in sight. Nothing is moved: a ring is
      // centred on its own point. Not clipped, so a ring at the edge of the range is whole.
      const stacked = new Set();        // the points that share their spot with another series
      if (o.rings) {
        const dotted = series.filter((sr) => sr.dots);
        const at = coincident(dotted.map((sr) => sr.points.map((p) => [x.px(p[0]), y.px(p[1])])), 1);
        const rings = s('g', { class: 'chart-rings' });
        dotted.forEach((sr, i) => sr.points.forEach((p, k) => {
          const c = at[i][k], cx = x.px(p[0]), cy = y.px(p[1]);
          if (c.others) stacked.add(p);
          // a point outside the frame has no dot in sight, so it gets no ring either
          if (!c.over || cx < box.l - 0.5 || cx > box.l + box.w + 0.5 || cy < box.t - 0.5 || cy > box.t + box.h + 0.5) return;
          rings.appendChild(s('circle', { cx, cy, r: (DOT_R + RING_STEP * c.over).toFixed(1), fill: 'none', class: 'chart-ring', style: `stroke:${sr.color}`, 'stroke-width': RING_W }));
        }));
        svg.appendChild(rings);
      }
      for (const m of o.markers || []) {
        if (!AS.isNum(m.x) || !AS.isNum(m.y)) continue;
        marks.appendChild(s('circle', { cx: x.px(m.x), cy: y.px(m.y), r: 5, class: 'chart-marker', style: `fill:${m.color}` }));
      }

      // hover: crosshair plus one tooltip listing every series at the nearest x
      const cross = s('line', { y1: box.t, y2: box.t + box.h, class: 'chart-cross', visibility: 'hidden' });
      const dots = s('g', {});
      svg.appendChild(cross); svg.appendChild(dots);
      const hit = s('rect', { x: box.l, y: box.t, width: box.w, height: box.h, fill: 'transparent' });
      svg.appendChild(hit);
      const fmtX = o.fmtX || ((v) => AS.fmt(v));
      const fmtY = o.fmtY || ((v) => AS.fmt(v));
      hit.addEventListener('pointermove', (ev) => {
        const rect = svg.getBoundingClientRect();
        const px = ev.clientX - rect.left;
        const xv = x.inv(px);
        AS.clear(dots);
        let nearestX = null;
        const rows = [];
        for (const sr of series) {
          let best = sr.points[0];
          for (const p of sr.points) if (Math.abs(p[0] - xv) < Math.abs(best[0] - xv)) best = p;
          if (nearestX == null || Math.abs(best[0] - xv) < Math.abs(nearestX - xv)) nearestX = best[0];
          rows.push({ sr, p: best });
        }
        cross.setAttribute('x1', x.px(nearestX)); cross.setAttribute('x2', x.px(nearestX));
        cross.setAttribute('visibility', 'visible');
        AS.clear(this.tip);
        // tipHead: the caller's own head where the axis label is a sentence, not a name to put before the value
        this.tip.appendChild(AS.h('div', { class: 'chart-tip-head' }, o.tipHead ? o.tipHead(nearestX) : (o.xLabel ? o.xLabel + ' ' : '') + fmtX(nearestX)));
        for (const { sr, p } of rows) {
          // only series that have a point close to the crosshair are listed
          if (Math.abs(x.px(p[0]) - x.px(nearestX)) > 12) continue;
          // a spot shared by several series already shows them all as dot and rings: the larger
          // hover dot would cover the innermost ring
          if (!stacked.has(p)) dots.appendChild(s('circle', { cx: x.px(p[0]), cy: y.px(p[1]), r: 4, class: 'chart-dot', style: `fill:${sr.color}` }));
          this.tip.appendChild(AS.h('div', { class: 'chart-tip-row' },
            AS.h('i', { class: 'chart-swatch' + (sr.dash ? ' dashed' : ''), style: { color: sr.color } }),
            AS.h('span', {}, sr.name), AS.h('b', {}, fmtY(p[1]))));
        }
        this.placeTip(ev);
      });
      hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); AS.clear(dots); this.tip.hidden = true; });
    }

    placeTip(ev) {
      this.tip.hidden = false;
      const host = this.el.getBoundingClientRect();
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      let left = ev.clientX - host.left + 14, top = ev.clientY - host.top - th - 8;
      if (left + tw > host.width - 4) left = ev.clientX - host.left - tw - 14;
      if (top < 2) top = ev.clientY - host.top + 14;
      this.tip.style.left = Math.max(2, left) + 'px';
      this.tip.style.top = top + 'px';
    }

    // ---------- parity scatter: y = x line with a relative tolerance band ----------
    drawScatter(svg, o, box) {
      const pts = (o.points || []).filter((p) => AS.isNum(p.x) && AS.isNum(p.y));
      if (!pts.length) { this.empty(svg, box, o.emptyText || 'No finished runs with a reference yet'); return; }
      const all = [];
      for (const p of pts) all.push(p.x, p.y);
      const [lo, hi] = padded(...extent(all), 0.15);
      const x = this.linearScale(lo, hi, box.l, box.l + box.w, Math.max(3, Math.floor(box.w / 80)));
      const y = this.linearScale(lo, hi, box.t + box.h, box.t, Math.max(3, Math.floor(box.h / 34)));
      this.axes(svg, box, x, y, o);
      const clipId = 'clip' + Math.random().toString(36).slice(2);
      const clip = s('clipPath', { id: clipId });
      clip.appendChild(s('rect', { x: box.l, y: box.t, width: box.w, height: box.h }));
      svg.appendChild(clip);
      const g = s('g', { 'clip-path': `url(#${clipId})` });
      svg.appendChild(g);
      if (o.tol) {
        const a = [lo, hi];
        const band = `M${x.px(a[0])} ${y.px(a[0] * (1 - o.tol))}L${x.px(a[1])} ${y.px(a[1] * (1 - o.tol))}` +
          `L${x.px(a[1])} ${y.px(a[1] * (1 + o.tol))}L${x.px(a[0])} ${y.px(a[0] * (1 + o.tol))}Z`;
        g.appendChild(s('path', { d: band, class: 'chart-band' }));
      }
      g.appendChild(s('line', { x1: x.px(lo), y1: y.px(lo), x2: x.px(hi), y2: y.px(hi), class: 'chart-ref' }));
      for (const p of pts) {
        const c = s('circle', { cx: x.px(p.x), cy: y.px(p.y), r: 5, class: 'chart-point', style: `fill:${p.color}`, tabindex: 0 });
        const show = (ev) => {
          AS.clear(this.tip);
          this.tip.appendChild(AS.h('div', { class: 'chart-tip-head' }, p.label || ''));
          this.tip.appendChild(AS.h('div', { class: 'chart-tip-row' }, AS.h('span', {}, o.xLabel || 'x'), AS.h('b', {}, AS.fmt(p.x))));
          this.tip.appendChild(AS.h('div', { class: 'chart-tip-row' }, AS.h('span', {}, o.yLabel || 'y'), AS.h('b', {}, AS.fmt(p.y))));
          if (p.x) this.tip.appendChild(AS.h('div', { class: 'chart-tip-row' }, AS.h('span', {}, 'difference'), AS.h('b', {}, AS.fmtPct((p.y - p.x) / p.x * 100))));
          this.placeTip(ev.clientX != null ? ev : { clientX: c.getBoundingClientRect().left, clientY: c.getBoundingClientRect().top });
        };
        c.addEventListener('pointerenter', show);
        c.addEventListener('focus', show);
        c.addEventListener('pointerleave', () => { this.tip.hidden = true; });
        c.addEventListener('blur', () => { this.tip.hidden = true; });
        g.appendChild(c);
      }
    }

    // ---------- horizontal bars, one per category: label, bar, value ----------
    drawBars(svg, o) {
      const items = (o.items || []).filter((it) => AS.isNum(it.value));
      const W = svg.width.baseVal.value, H = svg.height.baseVal.value;
      if (!items.length) { this.empty(svg, { l: 0, t: 0, w: W, h: H }, o.emptyText || 'No runs yet'); return; }
      const fmt = o.fmt || AS.fmt;
      const labelW = Math.min(96, Math.max(...items.map((it) => String(it.label).length)) * 6.6 + 10), valueW = 54;
      const x0 = labelW, span = Math.max(10, W - labelW - valueW);
      const hi = Math.max(...items.map((it) => it.value), 0) || 1;
      const slot = Math.min(30, (H - 6) / items.length), bh = Math.min(15, slot * 0.6);
      svg.appendChild(s('line', { x1: x0, x2: x0, y1: 2, y2: 4 + slot * items.length, class: 'chart-axis' }));
      items.forEach((it, i) => {
        const cy = 4 + slot * (i + 0.5), w = Math.max(0, span * it.value / hi), r = Math.min(4, w, bh / 2);
        // rounded outer end only; the bar stays anchored to the baseline
        const d = `M${x0} ${cy - bh / 2}H${x0 + w - r}Q${x0 + w} ${cy - bh / 2} ${x0 + w} ${cy - bh / 2 + r}V${cy + bh / 2 - r}` +
          `Q${x0 + w} ${cy + bh / 2} ${x0 + w - r} ${cy + bh / 2}H${x0}Z`;
        const bar = s('path', { d, class: 'chart-bar', style: `fill:${it.color || 'var(--s1)'}`, tabindex: 0, role: 'img', 'aria-label': `${it.label}: ${fmt(it.value)}` });
        const show = (ev) => {
          AS.clear(this.tip);
          this.tip.appendChild(AS.h('div', { class: 'chart-tip-head' }, it.fullLabel || it.label));
          this.tip.appendChild(AS.h('div', { class: 'chart-tip-row' }, AS.h('span', {}, o.yLabel || 'value'), AS.h('b', {}, fmt(it.value))));
          if (it.note) this.tip.appendChild(AS.h('div', { class: 'chart-tip-row' }, AS.h('span', {}, it.note)));
          const rect = bar.getBoundingClientRect();
          this.placeTip(ev.clientX != null ? ev : { clientX: rect.left + rect.width / 2, clientY: rect.top });
        };
        bar.addEventListener('pointermove', show);
        bar.addEventListener('focus', show);
        bar.addEventListener('pointerleave', () => { this.tip.hidden = true; });
        bar.addEventListener('blur', () => { this.tip.hidden = true; });
        // a wide transparent strip makes the hover target bigger than a thin or zero-length bar
        const strip = s('rect', { x: 0, y: cy - slot / 2, width: W, height: slot, fill: 'transparent' });
        strip.addEventListener('pointermove', show);
        strip.addEventListener('pointerleave', () => { this.tip.hidden = true; });
        svg.appendChild(strip);
        svg.appendChild(bar);
        svg.appendChild(s('text', { x: x0 - 6, y: cy + 3.5, 'text-anchor': 'end', class: 'chart-cat' }, it.label));
        svg.appendChild(s('text', { x: x0 + w + 5, y: cy + 3.5, class: 'chart-value' }, fmt(it.value)));
      });
    }
  }

  AS.chart = {
    line: (el) => new Chart(el, 'line'),
    scatter: (el) => new Chart(el, 'scatter'),
    bars: (el) => new Chart(el, 'bars'),
    SERIES: ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)', 'var(--s6)', 'var(--s7)', 'var(--s8)'],
  };

  /* Fixed colour per residual field, so p is always the same colour. */
  const FIELD_SLOT = { p: 0, Ux: 1, Uy: 2, Uz: 2, nuTilda: 4, k: 4, omega: 6, epsilon: 6 };
  AS.chart.fieldColor = function (name, index) {
    const slot = FIELD_SLOT[name] != null ? FIELD_SLOT[name] : (index + 3) % 8;
    return AS.chart.SERIES[slot];
  };
})();
