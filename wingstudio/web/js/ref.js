/* Wing Studio - the reference viewer (ref.html#<id>, or ref.html#<id>:<passage key>): the studio's
   reference catalogue on the left and one entry on the right, with the document kept for it rendered
   below and the passages the studio cites highlighted. The citation pop-up (js/cite.js) links here.
   The page is static. It reads GET /api/refs and GET /api/refs/doc/<id>, which also answer in the
   mock and the recorded tutorial (they are read-only). Everything is built with AS.h and text nodes; the
   id in the address is only ever used as a key of the catalogue.
   Two kinds of text never mix: what a source says (the document, word for word) and what the studio
   says about it (the summary, the notes on a passage, how the studio differs). */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h, $ = AS.$;

  const { ID, PASSAGE, KIND } = AS.REF;
  const GROUPS = [['Papers and books', ['paper', 'book']], ['Documentation', ['documentation']], ['This project', ['project-measurement', 'project-rule']]];
  /* Stage names as the studio shows them (AS.STAGE_DEFS in store.js, which this page does not load). */
  const STAGES = new Map([['case', 'Case'], ['geometry', 'Geometry'], ['mesh', 'Mesh'], ['physics', 'Boundary conditions and physics'], ['numerics', 'Solver setup'],
    ['solve', 'Solve'], ['post', 'Post-process'], ['validate', 'Validate against XFoil']]);
  const TITLE = 'References · Wing Studio';
  const OPEN_LIST = 6;     // a longer list of passages starts folded, so the document is not pushed off the screen
  const NO_PASSAGE = 'The passage named in the address is not in this document.';
  const NO_DOCUMENT = 'No document is kept for this reference, so the passage named in the address cannot be shown.';

  const root = $('#ref-root'), note = $('#ref-note');
  let data = null;         // the GET /api/refs body, once loaded
  let byId = new Map();    // a Map, not an object: an id such as "constructor" matches the id pattern
  let listBody = null, docBody = null;
  const docs = new Map();  // id -> { state: 'loading' | 'ok' | 'failed', markdown }, kept for the life of the page
  let shown = null;        // the entry on screen
  let paper = null;        // the panel its document is drawn in, when it has one

  const spinner = (label) => h('span', { class: 'working' }, h('i', { class: 'spinner', 'aria-hidden': 'true' }), label);
  const isProject = (e) => String(e.kind).startsWith('project-');
  const texts = (list) => (Array.isArray(list) ? list : []).filter((x) => x && typeof x.text === 'string' && x.text);
  const passagesOf = (e) => texts(e.passages).filter((p) => typeof p.key === 'string' && PASSAGE.test(p.key));

  async function fetchJson(url) {
    const response = await window.fetch(url, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(url + ' answered ' + response.status);
    const body = await response.json();           // a body that is not JSON rejects here
    if (!body || !Array.isArray(body.entries)) throw new Error(url + ' is not a reference catalogue');
    return body;
  }
  async function load() {
    const body = await fetchJson('/api/refs');
    // the server answers 200 with no entries and an error text when its catalogue file is unusable
    if (body.error && !body.entries.length) throw new Error(String(body.error));
    return body;
  }
  /* The document kept for an entry. The id is a key of the catalogue that matched the id
     pattern, so it is safe in a path. */
  async function fetchDoc(id) {
    const response = await window.fetch('/api/refs/doc/' + id, { headers: { Accept: 'application/json' } });
    const body = response.ok ? await response.json() : null;
    if (!body || typeof body.markdown !== 'string' || !body.markdown) throw new Error('no document');
    return body.markdown;
  }

  /* What the address names. id: '' for none, null for text that is no entry; name: the text before
     the first colon, where an id stands; passage: a key or ''; named: something stands after the
     colon, also when it is no well-formed key. */
  function wanted() {
    let raw = location.hash.replace(/^#/, '');
    if (!raw) return { id: '', raw: '', name: '', passage: '', named: false };
    try { raw = decodeURIComponent(raw); } catch (e) { /* shown as typed */ }
    const cut = raw.indexOf(':'), name = cut < 0 ? raw : raw.slice(0, cut), key = cut < 0 ? '' : raw.slice(cut + 1);
    return { id: ID.test(name) && byId.has(name) ? name : null, raw, name, passage: PASSAGE.test(key) ? key : '', named: key !== '' };
  }

  function say(kind, kids) {
    AS.clear(note);
    note.className = 'note ref-note' + (kind ? ' ' + kind : '');
    note.hidden = !kids;
    if (kids) AS.append(note, kids);
  }

  function frame() {
    AS.clear(root);
    listBody = h('div', { class: 'ref-scroll' });
    docBody = h('div', { class: 'ref-scroll' });
    AS.append(root, [
      h('nav', { class: 'ref-list', 'aria-label': 'All references' }, h('div', { class: 'props-head' }, h('span', { class: 'props-title' }, 'Catalogue')), listBody),
      h('article', { class: 'ref-doc' }, h('div',{ class: 'props-head' }, h('span', { class: 'props-title' }, 'Reference')), docBody)]);
  }

  function drawList() {
    AS.clear(listBody);
    const entries = data.entries.filter((e) => e && byId.get(e.id) === e);
    const known = GROUPS.reduce((all, g) => all.concat(g[1]), []);
    // an entry of a kind this page does not know is still listed
    const groups = GROUPS.concat([['Other', entries.map((e) => e.kind).filter((k) => !known.includes(k))]]);
    for (const [label, kinds] of groups) {
      const rows = entries.filter((e) => kinds.includes(e.kind));
      if (!rows.length) continue;
      AS.append(listBody, [h('div', { class: 'ref-group' }, label),
        rows.map((e) => h('a', { class: 'ref-item', href: '#' + encodeURIComponent(e.id), dataset: { ref: e.id } },
          h('span', { class: 'ref-item-title' }, e.title || e.id),
          e.doc === true ? h('span', { class: 'cite-tag ref-item-doc', title: 'The document is kept with the studio and opens here' }, 'document') : null))]);
    }
  }
  /* Marked in place: rebuilding the list would lose how far it was scrolled. */
  function markCurrent(id) {
    for (const item of AS.$$('.ref-item', listBody)) {
      if (item.dataset.ref === id) item.setAttribute('aria-current', 'true'); else item.removeAttribute('aria-current');
    }
  }

  /* 'mesh.first_cell_height' -> 'Mesh · first cell height' */
  function settingName(key) {
    const dot = String(key).indexOf('.');
    const stage = dot < 0 ? '' : key.slice(0, dot), name = dot < 0 ? key : key.slice(dot + 1);
    return (stage ? (STAGES.get(stage) || AS.humanKey(stage)) + ' · ' : '') + AS.humanKey(name);
  }
  const settingNames = (list) => (Array.isArray(list) ? list : []).filter((k) => typeof k === 'string').map(settingName).join(', ');

  // ---------- the document: Markdown to DOM, with the cited passages marked ----------
  /* Written for the documents under wingstudio/refs/docs: headings, paragraphs, lists, tables, fenced
     blocks, block quotes, and formulas left in the notation of the source (nothing here typesets
     LaTeX). Text only ever goes into text nodes. Every piece of text keeps its offset in the
     source, so a passage, which is an exact substring of the source, is marked wherever its
     characters come out, also across a table cell or a code span.
     `have` names the lines of the header block (Source, Retrieved, ...) the entry already shows.
     A link inside the document is never followed, whatever its host: it stays text with its address
     in the tooltip. The entry's verified Original address is the only link out of this page. */
  const META = /^(Source|Retrieved|Publisher|Licence|Scope): (.*)$/;
  const FENCE = /^\s*```/, HEAD = /^(#{1,6})[ \t]+(.*?)\s*$/, ITEM = /^(\s*)([-*]|\d+\.)[ \t]+(.*)$/, QUOTE = /^\s*>\s?/, RULE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
  const isRow = (t) => /^\s*\|.*\|\s*$/.test(t);
  const isSeparator = (t) => /^\s*\|?[\s:|-]+\|?\s*$/.test(t) && t.includes('-');
  const INLINE_TAGS = { sup: 'sup', sub: 'sub', b: 'strong', i: 'em' };
  /* Inline markup that no listing carries: a tag, a link, an escaped bracket, a formula in $$. */
  const SPLIT_OFF = /<(sup|sub|b|i)>|\]\(https?:\/\/|\\[\[\]]|\$\$[^$\n]+\$\$/i;
  function renderDoc(markdown, passages, have) {
    const src = String(markdown).replace(/\r\n?/g, '\n');
    const ranges = [];
    for (const p of passages) {
      const start = src.indexOf(p.text);
      if (start >= 0) ranges.push({ start, end: start + p.text.length, key: p.key });
    }
    const out = h('div', { class: 'ref-md' });

    /* [from, to) of the source as text, cut at the edges of the passages that cover it */
    function piece(from, to) {
      const nodes = [];
      let at = from;
      while (at < to) {
        let next = to;
        const keys = [];
        for (const r of ranges) {
          if (r.start <= at && at < r.end) { keys.push(r.key); next = Math.min(next, r.end); }
          else if (r.start > at) next = Math.min(next, r.start);
        }
        const text = src.slice(at, next);
        nodes.push(keys.length ? h('mark', { class: 'ref-hl', dataset: { passage: keys.join(' ') } }, text) : text);
        at = next;
      }
      return nodes;
    }
    /* Code, a formula ($$...$$ inside a line, or $...$), bold, a link, the four harmless tags the
       sources use, a bracket escaped with a backslash, emphasis. */
    function inline(from, to) {
      const re = /`(?<code>[^`\n]+)`|\$\$[ \t]*(?<shown>[^$\s][^$\n]*?)[ \t]*\$\$|\$(?!\s)(?<math>[^$\n]+?)(?<!\s)\$|\*\*(?<bold>[^*\n]+)\*\*|\[(?<label>[^\]\n]+)\]\((?<address>https?:\/\/[^)\s]+)\)|<(?<tag>sup|sub|b|i)>(?<inner>[^<\n]*)<\/\k<tag>>|\\(?<bracket>[\[\]])|(?<![\w*])\*(?!\s)(?<em>[^*\n]+?)(?<!\s)\*(?![\w*])/gi;
      const text = src.slice(from, to), nodes = [];
      const math = (a, b) => h('span', { class: 'ref-math', title: 'A formula, in the notation of the source' }, piece(a, b));
      let last = 0, m;
      while ((m = re.exec(text))) {
        if (m.index > last) nodes.push(piece(from + last, from + m.index));
        const at = from + m.index, g = m.groups;
        if (g.code != null) nodes.push(h('code', {}, piece(at + 1, at + 1 + g.code.length)));
        else if (g.shown != null) { const a = at + m[0].indexOf(g.shown, 2); nodes.push(math(a, a + g.shown.length)); }
        else if (g.math != null) nodes.push(math(at + 1, at + 1 + g.math.length));
        else if (g.bold != null) nodes.push(h('strong', {}, piece(at + 2, at + 2 + g.bold.length)));
        // never a live link, also not to the site of the Original address: any path there may be someone else's page
        else if (g.label != null) nodes.push(h('span', { class: 'ref-link-off', title: g.address }, piece(at + 1, at + 1 + g.label.length)));
        else if (g.tag != null) nodes.push(h(INLINE_TAGS[g.tag.toLowerCase()], {}, piece(at + g.tag.length + 2, at + g.tag.length + 2 + g.inner.length)));
        else if (g.bracket != null) nodes.push(piece(at + 1, at + 2));
        else nodes.push(h('em', {}, piece(at + 1, at + 1 + g.em.length)));
        last = m.index + m[0].length;
      }
      if (last < text.length) nodes.push(piece(from + last, to));
      return nodes;
    }

    const lines = [];
    let offset = 0;
    for (const text of src.split('\n')) { lines.push({ text, at: offset }); offset += text.length + 1; }
    const end = (l) => l.at + l.text.length;
    /* a line without the white space around it */
    function trimmed(l) {
      const lead = l.text.length - l.text.trimStart().length;
      return [l.at + lead, l.at + l.text.trimEnd().length];
    }
    /* the cells of a table row: between unescaped pipes, without the white space around them */
    function cells(l) {
      const t = l.text, cuts = [], found = [];
      for (let k = 0; k < t.length; k++) if (t[k] === '|' && t[k - 1] !== '\\') cuts.push(k);
      for (let k = 0; k + 1 < cuts.length; k++) {
        let a = cuts[k] + 1, b = cuts[k + 1];
        while (a < b && /\s/.test(t[a])) a++;
        while (b > a && /\s/.test(t[b - 1])) b--;
        found.push([l.at + a, l.at + b]);
      }
      return found;
    }
    const startsBlock = (i) => {
      const t = lines[i].text;
      return FENCE.test(t) || HEAD.test(t) || ITEM.test(t) || QUOTE.test(t) || RULE.test(t) || t.trim().startsWith('$$') || t.trim() === '[...]' ||
        (isRow(t) && i + 1 < lines.length && isSeparator(lines[i + 1].text));
    };
    /* The rest of a line that the source page split off into a block of its own: a fence without a
       language around one line that still carries inline markup (SPLIT_OFF). Drawn as a listing it
       shows its markup raw and cuts the sentence in two, so it is joined back onto the line above.
       The index of that line when the fence at line i is one, else -1. */
    function splitOff(i) {
      return i + 2 < lines.length && /^\s*```\s*$/.test(lines[i].text) && !FENCE.test(lines[i + 1].text) && FENCE.test(lines[i + 2].text) &&
        SPLIT_OFF.test(lines[i + 1].text) ? i + 1 : -1;
    }

    let i = 0, body = false;     // body: something after the title has been drawn, so the header block is over
    while (i < lines.length) {
      const l = lines[i], t = l.text;
      let m;
      if (!t.trim()) { i++; continue; }
      if (FENCE.test(t)) {
        const rest = splitOff(i), above = out.lastElementChild;
        if (rest >= 0 && above && above.tagName === 'P' && !above.classList.contains('ref-gap')) {
          // the rest of the paragraph above (a list takes its own in the loop below)
          AS.append(above, ['\n', inline(...trimmed(lines[rest]))]);
          i = rest + 2;
          continue;
        }
        let j = i + 1;
        while (j < lines.length && !FENCE.test(lines[j].text)) j++;
        // one piece from the first line to the last, line breaks included
        out.appendChild(h('pre', {}, h('code', {}, j > i + 1 ? piece(lines[i + 1].at, end(lines[j - 1])) : null)));
        i = j + 1; body = true;
      } else if ((m = HEAD.exec(t))) {
        const from = l.at + t.indexOf(m[2], m[1].length);
        // the page has its own h2: the title of the document is an h3, its sections follow
        out.appendChild(h('h' + Math.min(6, m[1].length + 2), {}, inline(from, from + m[2].length)));
        i++; body = body || m[1].length > 1;
      } else if (!body && META.test(t)) {
        // the header block written when the document was fetched: one line per fact, shown as a small grid
        const rows = [];
        for (; i < lines.length && (m = META.exec(lines[i].text)); i++) {
          if (have.includes(m[1])) continue;
          const from = lines[i].at + m[1].length + 2;
          rows.push(h('div', {}, h('dt', {}, m[1]), h('dd', {}, piece(from, end(lines[i])))));
        }
        if (rows.length) out.appendChild(h('dl', { class: 'ref-md-meta' }, rows));
        body = true;
      } else if (t.trim().startsWith('$$')) {
        // a displayed formula, on one line or closed on a later one
        let j = i;
        const from = l.at + t.indexOf('$$') + 2;
        while (j < lines.length - 1 && !(lines[j].text.trimEnd().endsWith('$$') && end(lines[j]) - 2 >= from)) j++;
        const closed = lines[j].text.trimEnd().endsWith('$$') && lines[j].at + lines[j].text.lastIndexOf('$$') >= from;
        out.appendChild(h('div', { class: 'ref-math', title: 'A formula, in the notation of the source' }, piece(from, closed ? lines[j].at + lines[j].text.lastIndexOf('$$') : end(lines[j]))));
        i = j + 1; body = true;
      } else if (t.trim() === '[...]') {
        out.appendChild(h('p', { class: 'ref-gap', title: 'Text of the source that is not part of this excerpt' }, piece(...trimmed(l))));
        i++; body = true;
      } else if (RULE.test(t)) {
        out.appendChild(h('hr', {}));
        i++; body = true;
      } else if (isRow(t) && i + 1 < lines.length && isSeparator(lines[i + 1].text)) {
        const head = cells(l), rows = [];
        for (i += 2; i < lines.length && isRow(lines[i].text); i++) rows.push(cells(lines[i]));
        out.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
          h('thead', {}, h('tr', {}, head.map((c) => h('th', {}, inline(c[0], c[1]))))),
          h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, inline(c[0], c[1])))))))));
        body = true;
      } else if (ITEM.test(t)) {
        // a deeper indent opens a list inside the last item; a list that does not start at 1 keeps its number
        const stack = [];
        for (; i < lines.length; i++) {
          m = ITEM.exec(lines[i].text);
          let top = stack[stack.length - 1];
          if (!m) {
            // an indented line that follows an item belongs to it
            if (top && /^\s+\S/.test(lines[i].text) && !startsBlock(i)) { AS.append(top.last, ['\n', inline(...trimmed(lines[i]))]); continue; }
            // so does the rest of its line that the source split off into a fence; the list then goes on
            let k = i;
            while (k < lines.length && !lines[k].text.trim()) k++;
            const rest = top && k < lines.length ? splitOff(k) : -1;
            if (rest < 0) break;
            AS.append(top.last, ['\n', inline(...trimmed(lines[rest]))]);
            for (k = rest + 2; k < lines.length && !lines[k].text.trim();) k++;
            if (k < lines.length && ITEM.test(lines[k].text)) { i = k - 1; continue; }
            i = rest + 2;
            break;
          }
          const indent = m[1].replace(/\t/g, '    ').length, number = parseInt(m[2], 10);
          while (stack.length > 1 && indent < top.indent) { stack.pop(); top = stack[stack.length - 1]; }
          if (!top || indent > top.indent) {
            const el = h(isFinite(number) ? 'ol' : 'ul', { start: isFinite(number) && number !== 1 ? String(number) : null });
            (top ? top.last : out).appendChild(el);
            top = { indent, el, last: null };
            stack.push(top);
          }
          top.last = h('li', {}, inline(end(lines[i]) - m[3].length, end(lines[i])));
          top.el.appendChild(top.last);
        }
        body = true;
      } else if (QUOTE.test(t)) {
        const quote = h('blockquote', {});
        let para = null;
        for (; i < lines.length && QUOTE.test(lines[i].text); i++) {
          const from = lines[i].at + QUOTE.exec(lines[i].text)[0].length;
          if (!lines[i].text.slice(from - lines[i].at).trim()) { para = null; continue; }
          if (!para) { para = h('p', {}); quote.appendChild(para); } else para.appendChild(document.createTextNode('\n'));
          AS.append(para, inline(from, end(lines[i])));
        }
        out.appendChild(quote);
        body = true;
      } else {
        const para = h('p', {});
        do {
          if (para.firstChild) para.appendChild(document.createTextNode('\n'));
          AS.append(para, inline(...trimmed(lines[i])));
          i++;
        } while (i < lines.length && lines[i].text.trim() && !startsBlock(i));
        out.appendChild(para);
        body = true;
      }
    }
    return out;
  }

  /* The passage the address names: marked as the current one and, when asked, brought into view. */
  function markPassage(key, scroll) {
    let first = null;
    for (const m of AS.$$('mark.ref-hl', docBody)) {
      const on = !!key && String(m.dataset.passage).split(' ').includes(key);
      m.classList.toggle('current', on);
      m.removeAttribute('aria-current');
      if (on && !first) first = m;
    }
    for (const item of AS.$$('.ref-passage', docBody)) {
      if (key && item.dataset.passage === key) item.setAttribute('aria-current', 'true'); else item.removeAttribute('aria-current');
    }
    if (!first) return false;
    first.setAttribute('aria-current', 'true');
    if (scroll) first.scrollIntoView({ block: 'center' });
    return true;
  }

  /* A passage the address names that cannot be shown gets one plain line. Without it a stale link
     opens the entry at its top with nothing marked and nothing said. */
  function tellPassage() {
    const want = wanted();
    if (!shown || want.id !== shown.id || !want.named) return;
    if (shown.doc !== true) { say('warn', [NO_DOCUMENT]); return; }
    const got = docs.get(shown.id);
    // nothing is known while the document loads; one that could not be loaded says so in its own panel
    if (got && got.state === 'ok' && !$('mark.ref-hl.current', docBody)) say('warn', [NO_PASSAGE]);
  }

  function drawPaper() {
    if (!paper || !shown) return;
    const e = shown, got = docs.get(e.id);
    AS.clear(paper);
    paper.appendChild(h('div', { class: 'props-head' }, h('span', { class: 'props-title' }, 'Document'),
      h('span', { class: 'ref-paper-note' }, 'The text of the source, word for word. Passages the studio cites are highlighted.')));
    if (got && got.state === 'ok') {
      const passages = passagesOf(e);
      const have = [e.retrieved ? 'Retrieved' : '', e.licence ? 'Licence' : '', e.scope ? 'Scope' : ''].filter(Boolean);
      const node = renderDoc(got.markdown, passages, have);
      const found = new Set();         // the keys of the passages that are marked in the text
      // the studio's note on a passage is one hover away from the passage itself
      for (const m of AS.$$('mark.ref-hl', node)) {
        const keys = String(m.dataset.passage).split(' ');
        const p = passages.find((x) => x.key === keys[0]);
        if (p) m.title = 'Cited for ' + settingNames(p.settings);
        for (const key of keys) found.add(key);
      }
      // A listed passage whose text is not in this copy (the index and the document out of step) has
      // nothing to go to: its line says so and is no link.
      for (const item of AS.$$('.ref-passage', docBody)) {
        const go = found.has(item.dataset.passage) ? null : $('a.ref-passage-go', item);
        if (!go) continue;
        go.replaceWith(h('span', { class: 'ref-passage-go' }, go.textContent),
          h('span', { class: 'cite-tag ref-passage-missing', title: 'The text of this passage was not found in the copy of the document kept here.' }, 'not in this copy'), ' ');
      }
      paper.appendChild(node);
      markPassage(wanted().passage, true);
      tellPassage();
    } else if (got && got.state === 'failed') {
      paper.appendChild(h('div', { class: 'ref-paper-state' }, h('span', {}, 'The document could not be loaded.'),
        h('button', { type: 'button', class: 'btn small', onclick: () => { docs.delete(e.id); drawPaper(); } }, 'Retry')));
    } else {
      paper.appendChild(h('div', { class: 'ref-paper-state' }, spinner('Loading the document')));
      if (!got) {
        docs.set(e.id, { state: 'loading' });
        const done = () => { if (shown === e) drawPaper(); };      // another entry may be on screen by now
        fetchDoc(e.id).then((markdown) => { docs.set(e.id, { state: 'ok', markdown }); }, () => { docs.set(e.id, { state: 'failed' }); }).then(done);
      }
    }
  }

  function scopeText(scope) {
    if (scope === 'full') return 'Full document';
    if (scope === 'excerpts') return 'Excerpts relevant to the studio. [...] marks text that is left out.';
    return typeof scope === 'string' ? scope : '';
  }
  function drawDoc(e) {
    AS.clear(docBody);
    paper = null;
    if (!e) {
      docBody.appendChild(h('div', { class: 'ref-doc-body' }, h('div', { class: 'placeholder' }, 'Choose a reference from the list.')));
      return;
    }
    // an entry without its own date was not checked: the date of the catalogue as a whole must not stand in for it
    const url = AS.verifiedUrl(e), checked = typeof e.checked === 'string' ? e.checked : '';
    const defaults = data.defaults && typeof data.defaults === 'object' ? data.defaults : {};
    const citedFor = Object.keys(defaults).filter((k) => Array.isArray(defaults[k]) && defaults[k].includes(e.id));
    const hasDoc = e.doc === true, passages = passagesOf(e), differs = texts(e.differs);
    const row = (label, value, text) => (value ? h('div', {}, h('dt', {}, label), h('dd', { class: text ? 'ref-text' : null }, value)) : null);
    if (hasDoc) paper = h('section', { class: 'ref-paper', 'aria-label': 'Document' });
    docBody.appendChild(h('div', { class: 'ref-doc-body' },
      h('h2', {}, e.title || e.id, h('span', { class: 'cite-tag' }, KIND.get(e.kind) || String(e.kind || 'source'))),
      isProject(e) ? h('p', { class: 'muted' }, 'Measured or decided in this project; not a published result.') : null,
      h('dl', { class: 'kv' },
        // a project entry names its evidence as file and section: plain text in the file face, never a link
        row('Source', e.source, !isProject(e)),
        row('Where in the source', e.locator, true),
        row('Retrieved', hasDoc && typeof e.retrieved === 'string' ? e.retrieved : ''),
        row('Licence', hasDoc && typeof e.licence === 'string' ? e.licence : '', true),
        row('Scope', hasDoc ? scopeText(e.scope) : '', true),
        row('Address', url
          ? [h('a', { class: 'cite-ext', href: url, target: '_blank', rel: 'noopener noreferrer', title: 'The original, in a new tab' }, AS.icon('external', 12), 'Original'), h('span', { class: 'ref-host' }, AS.urlHost(url))]
          : 'No verified public address is recorded for this entry.', true),
        row('Id', e.id),
        row('Last checked', checked || 'Not checked', !checked)),
      h('h3', {}, 'Studio summary'),
      h('p', { class: 'ref-says' }, e.says || 'No summary is recorded for this entry.'),
      differs.length ? [h('h3', {}, 'How the studio differs'),
        h('ul', { class: 'ref-differs' }, differs.map((d) => h('li', {}, h('p', {}, d.text), settingNames(d.settings) ? h('div', { class: 'ref-for' }, settingNames(d.settings)) : null)))] : null,
      h('h3', {}, 'Cited by default for'),
      citedFor.length ? h('ul', { class: 'ref-settings' }, citedFor.map((k) => h('li', {}, settingName(k))))
        : h('p', { class: 'muted' }, 'No setting cites this reference by default.'),
      hasDoc && passages.length ? h('details', { class: 'ref-cited', open: passages.length <= OPEN_LIST },
        h('summary', {}, `Passages the studio cites (${passages.length})`),
        h('p', { class: 'muted small' }, 'Each line names a section of the document, the settings it is cited for and, in the studio\'s words, why. The passage itself is highlighted in the document.'),
        h('ol', { class: 'ref-passages' }, passages.map((p) => h('li', { class: 'ref-passage', dataset: { passage: p.key } },
          h('a', { class: 'ref-passage-go', href: '#' + encodeURIComponent(e.id) + ':' + p.key }, p.heading || p.key),
          settingNames(p.settings) ? h('span', { class: 'ref-for' }, settingNames(p.settings)) : null,
          typeof p.why === 'string' && p.why ? h('p', { class: 'ref-passage-why' }, p.why) : null)))) : null,
      paper));
    drawPaper();
  }

  function draw() {
    if (!data) return;
    const want = wanted(), entry = want.id ? byId.get(want.id) : null;
    // the notice names the id alone: what follows the colon is a passage key and no part of it
    say(want.id === null ? 'warn' : '', want.id === null ? ['No reference with the id ', h('span', { class: 'mono' }, (want.name || want.raw).slice(0, 80)), '.'] : null);
    markCurrent(want.id || '');
    root.classList.toggle('has-doc', !!entry);       // stacked on a narrow window, the entry comes before the list
    document.title = entry ? (entry.title || entry.id) + ' · ' + TITLE : TITLE;
    const narrow = window.matchMedia('(max-width: 760px)').matches;
    // another passage of the entry on screen: nothing is rebuilt, the view moves
    if (entry && entry === shown && markPassage(want.passage, true)) return;
    const same = entry && entry === shown;
    shown = entry || null;
    if (!same) drawDoc(entry);
    docBody.scrollTop = 0;
    if (narrow) window.scrollTo(0, 0);
    else { const on = $('.ref-item[aria-current]', listBody); if (on) on.scrollIntoView({ block: 'nearest' }); }
    // the document may be on the page already (kept from an earlier visit): its passage comes after the reset above
    if (!same) markPassage(want.passage, true);
    tellPassage();
  }

  async function start() {
    say('', null);
    frame();
    docBody.appendChild(h('div', { class: 'ref-doc-body' }, spinner('Loading the references')));
    try {
      data = await load();
    } catch (e) {
      data = null;
      AS.clear(docBody);
      say('error', [h('span', {}, 'The reference catalogue could not be loaded.'),
        h('button', { type: 'button', class: 'btn small', onclick: start }, 'Retry')]);
      return;
    }
    byId = new Map();
    for (const e of data.entries) if (e && typeof e.id === 'string' && ID.test(e.id) && !byId.has(e.id)) byId.set(e.id, e);
    const kept = Array.from(byId.values()).filter((e) => e.doc === true).length;
    $('#ref-count').textContent = byId.size + ' entries' + (kept ? ', ' + kept + ' with a document' : '') + (data.checked ? ', checked ' + data.checked : '');
    shown = null;
    drawList();
    draw();
  }

  $('#ref-mark').appendChild(AS.icon('wing', 16));
  // another entry or passage is another fragment: no reload, the list keeps its place
  window.addEventListener('hashchange', draw);
  start();
})();
