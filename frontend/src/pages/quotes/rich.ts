/*
  The rich-text rules for quote descriptions and notes, copied from
  web/script.js (_ceClean and friends).

  The PDF understands exactly four things: bold, italic, underline and colour,
  plus line breaks. A paste from Word or Outlook arrives carrying font names,
  point sizes and nested tables, and each shape broke the PDF a new way. So
  everything is cleaned on the way in and again on the way out, and only
  markup the PDF can render survives.
*/

export const esc = (s: string) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** Decode entities first, or a saved "&amp;" becomes "&amp;amp;" on every save. */
export function decode(s: string): string {
  const d = document.createElement('div')
  d.innerHTML = String(s)
  return d.textContent || ''
}

/**
  A colour ReportLab can parse, or null for "no colour - follow the page".

  Only real colours are kept: reds, blues, greens - the ones someone chose to
  make a line stand out. Neutral ones are dropped: black, the near-blacks
  every email and Word file writes for ordinary text (Gmail rgb(34,34,34),
  Outlook #212121, this app's own #0f1a2e when a row is copied to another),
  greys, and white. Kept, they froze ordinary text dark - unreadable in dark
  mode - or white, invisible in light mode and on the printed PDF. Without a
  colour, text is light in dark mode, dark in light mode and black on paper.
*/
function colour(el: Element): string | null {
  const raw = String(el.getAttribute('color') || (el as HTMLElement).style?.color || '').trim()
  if (!raw) return null
  let hex: string | null = null
  const rgb = raw.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i)
  if (rgb) hex = '#' + [1, 2, 3].map(i => ('0' + (parseInt(rgb[i], 10) & 255).toString(16)).slice(-2)).join('')
  else if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw)) hex = raw.toLowerCase()
  // Word writes the common ones by name: style="color:red".
  else hex = NAMED[raw.toLowerCase()] ?? null
  return hex && !isNeutral(hex) ? hex : null
}

/** Black, white or any grey in between: the three channels nearly equal. */
export function isNeutral(hex: string): boolean {
  let h = hex.replace('#', '')
  if (h.length === 3) h = h.split('').map(c => c + c).join('')
  const [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16))
  return Math.max(r, g, b) - Math.min(r, g, b) < 40
}

const NAMED: Record<string, string> = {
  red: '#ff0000', darkred: '#8b0000', maroon: '#800000', blue: '#0000ff', navy: '#000080', darkblue: '#00008b',
  green: '#008000', darkgreen: '#006400', orange: '#ffa500', purple: '#800080', teal: '#008080',
}

const BLOCK = new Set(['DIV', 'P', 'LI', 'TR', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6'])

export function clean(html: string | null | undefined): string {
  if (html == null || html === '') return ''
  const src = String(html)
  if (!src.includes('<')) return esc(decode(src))
  const doc = new DOMParser().parseFromString('<div id="r">' + src + '</div>', 'text/html')
  const root = doc.getElementById('r')
  if (!root) return esc(src.replace(/<[^>]*>/g, ''))
  const out: string[] = []
  // Lines as the screen shows them. A block (each line typed with Enter, a
  // pasted paragraph) starts AND ends a line - wherever it sits, even inside
  // bold text or as the last thing in its parent. Only breaking after blocks
  // that weren't last lost the line breaks around bold headings, and a saved
  // note ran together on the PDF (SPM-00114-CNL). A <br> closing a block is the
  // browser's placeholder for an empty line, so it isn't counted twice.
  let open = false        // the current line has something on it
  let pending = false     // a line ended; break before whatever comes next
  const flush = () => { if (pending) { out.push('<br>'); pending = false } }
  const endLine = () => { if (open) { pending = true; open = false } }

  const walk = (node: Node) => {
    const kids = node.childNodes
    for (let i = 0; i < kids.length; i++) {
      const n = kids[i]
      if (n.nodeType === 3) {
        const v = n.nodeValue || ''
        // Whitespace between blocks (Word's newlines between paragraphs) isn't text.
        if (!/[^ \t\r\n]/.test(v)) { if (open) out.push(esc(v)); continue }
        flush(); out.push(esc(v)); open = true
        continue
      }
      if (n.nodeType !== 1) continue
      const el = n as HTMLElement
      const tag = el.tagName.toUpperCase()
      if (tag === 'BR') { flush(); out.push('<br>'); open = false; continue }
      if (tag === 'SCRIPT' || tag === 'STYLE') continue
      const block = BLOCK.has(tag)
      if (block) endLine()
      const st = el.style || ({} as CSSStyleDeclaration)
      const weight = String(st.fontWeight || '')
      const wraps: [string, string][] = []
      if (tag === 'B' || tag === 'STRONG' || weight === 'bold' || parseInt(weight, 10) >= 600) wraps.push(['<b>', '</b>'])
      if (tag === 'I' || tag === 'EM' || String(st.fontStyle || '') === 'italic') wraps.push(['<i>', '</i>'])
      if (tag === 'U' || /underline/i.test(String(st.textDecoration || st.textDecorationLine || ''))) wraps.push(['<u>', '</u>'])
      const col = colour(el)
      if (col) wraps.push([`<font color="${col}">`, '</font>'])
      // A line break waiting goes before the bold or colour, not just inside it.
      if (wraps.length && /[^ \t\r\n]/.test(el.textContent || '')) flush()
      wraps.forEach(w => out.push(w[0]))
      walk(el)
      for (let k = wraps.length - 1; k >= 0; k--) out.push(wraps[k][1])
      // A block ends a line, or "<p>one</p><p>two</p>" becomes "onetwo".
      if (block) endLine()
      // Table cells sit on one line but need separating, or a pasted row reads "2IN300#".
      else if ((tag === 'TD' || tag === 'TH') && open && i < kids.length - 1) out.push(' ')
    }
  }
  walk(root)

  let s = out.join('')
  // An empty bold/coloured wrapper is what pressing Enter with bold on leaves behind.
  for (let pass = 0; pass < 3; pass++) {
    s = s.replace(/<(b|i|u)>((?:\s|<br>)*)<\/\1>/g, '$2').replace(/<font [^>]*>((?:\s|<br>)*)<\/font>/g, '$1')
  }
  return s.replace(/(?:\s*<br>\s*){3,}/g, '<br><br>').replace(/^(?:\s*<br>)+|(?:<br>\s*)+$/g, '').trim()
}

/** Notes saved before they were rich text hold real newlines; keep their shape. */
export function notesToHtml(val: string | null | undefined): string {
  let src = String(val == null ? '' : val)
  if (!src.includes('<')) src = esc(decode(src)).replace(/\r?\n/g, '<br>')
  return clean(src)
}

/** What a paste becomes: cleaned HTML, or plain text with its line breaks. */
export function pasteHtml(e: ClipboardEvent | React.ClipboardEvent): string {
  const html = e.clipboardData?.getData('text/html')
  return html ? clean(html) : esc(e.clipboardData?.getData('text/plain') || '').replace(/\r?\n/g, '<br>')
}
