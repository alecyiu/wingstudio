/* Wing Studio - minimal, safe markdown renderer for agent text and the
   final report. Builds DOM nodes directly (never innerHTML), so model output
   cannot inject markup. Supports headings, paragraphs, lists, tables, block
   quotes, code blocks, bold, italic, inline code and links. */
(function () {
  'use strict';
  const AS = window.AS;
  const h = AS.h;

  function inline(text) {
    const out = [];
    const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*\s][^*]*)\*|\[([^\]]+)\]\((https?:[^)\s]+|\/[^)\s]*)\))/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[2] != null) out.push(h('strong', {}, m[2]));
      else if (m[3] != null) out.push(h('code', {}, m[3]));
      else if (m[4] != null) out.push(h('em', {}, m[4]));
      else out.push(h('a', { href: m[6], target: '_blank', rel: 'noopener' }, m[5]));
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  // A cell may contain an escaped pipe (\|), as in |Cm|: split on unescaped pipes only.
  const cells = (l) => l.trim().replace(/^\|/, '').replace(/([^\\])\|$/, '$1').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));

  AS.md = function (source) {
    const root = h('div', { class: 'md' });
    const lines = String(source || '').replace(/\r/g, '').split('\n');
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if (/^```/.test(line)) {
        const code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        root.appendChild(h('pre', {}, h('code', {}, code.join('\n'))));
      } else if ((m = /^(#{1,4})\s+(.*)$/.exec(line))) {
        root.appendChild(h('h' + Math.min(4, m[1].length + 1), {}, inline(m[2])));
        i++;
      } else if (isRow(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
        const head = cells(line);
        i += 2;
        const body = [];
        while (i < lines.length && isRow(lines[i])) body.push(cells(lines[i++]));
        root.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
          h('thead', {}, h('tr', {}, head.map((c) => h('th', {}, inline(c))))),
          h('tbody', {}, body.map((r) => h('tr', {}, r.map((c) => h('td', {}, inline(c)))))))));
      } else if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const list = h(ordered ? 'ol' : 'ul', {});
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          list.appendChild(h('li', {}, inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ''))));
          i++;
        }
        root.appendChild(list);
      } else if (/^>\s?/.test(line)) {
        const quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ''));
        root.appendChild(h('blockquote', {}, inline(quote.join(' '))));
      } else {
        const para = [];
        while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*([-*]|\d+\.)\s+|>\s?)/.test(lines[i]) && !isRow(lines[i])) para.push(lines[i++]);
        if (!para.length) para.push(lines[i++]);
        root.appendChild(h('p', {}, inline(para.join(' '))));
      }
    }
    return root;
  };
})();
