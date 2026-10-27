import { clean, esc } from './rich'

/*
  Turning a pasted table into quote lines.

  A table copied from an email, Word, Excel or a web page arrives as HTML with
  its rows and cells, so it is read here directly - instant, and free. Each
  column is matched to a quote column by its heading (Material -> product no,
  Quantity requested -> qty...), or by what's in it when the heading row
  wasn't copied. A column the quote has no place for is kept, as a line added
  to each item's description ("Manufacturer: FLEXITALLIC"). Totals are dropped:
  the quote works those out itself.

  Text copied out of a PDF has lost its table; the server rebuilds it (with
  Claude) and hands back headings and rows, which then go through the same
  matching. Either way the person sees a preview and can re-match any column
  before anything is added.
*/

export type Field = 'item_no' | 'product_no' | 'description' | 'uom' | 'quantity' | 'unit_price' | 'extra' | 'skip'

export const FIELD_LABEL: Record<Field, string> = {
  product_no: 'Product no.', description: 'Description', uom: 'UOM', quantity: 'Qty', unit_price: 'Unit price',
  extra: 'Add to description', item_no: 'Item no. (skip)', skip: 'Skip',
}
// Each of these can hold one column; extra and skip can hold any number.
const SINGLE: Field[] = ['item_no', 'product_no', 'description', 'uom', 'quantity', 'unit_price']

export type Table = {
  headers: string[]          // as copied, or "Column 1"... when no heading row came with it
  cells: string[][]          // each cell as cleaned HTML (keeps line breaks, bold, colour)
  hadHeader: boolean
}

export type Item = { product_no: string; description: string; uom: string; quantity: number; unit_price: number }

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9$#/ ]+/g, ' ').replace(/\s+/g, ' ').trim()
const plain = (html: string) => {
  const d = document.createElement('div')
  d.innerHTML = html.replace(/<br\s*\/?>/gi, '\n')
  return (d.textContent || '').replace(/[ \t]+/g, ' ').trim()
}

/** The quote column a heading means, or null if it's none of them. Order matters:
    "unit price" must win over "unit", "material description" over "material". */
export function fieldForHeader(h: string): Field | null {
  const s = norm(h)
  if (!s) return null
  if (/\b(qty|quantity|quantities|qnty|quan)\b/.test(s)) return 'quantity'
  if (/\b(price|rate|cost)\b|\$ ?\/ ?each|per unit/.test(s)) return /\b(total|amount|extended|ext)\b/.test(s) && !/\bunit\b/.test(s) ? 'skip' : 'unit_price'
  if (/\b(total|amount|extended|ext|value|sum)\b/.test(s)) return 'skip'
  if (/\b(description|desc|specification|spec|details|particulars|item description|material description)\b/.test(s)) return 'description'
  if (/\b(material|part|product|catalogue|catalog|cat|stock|mesc|sap|code|ref|reference|sku|article|mfr)\b/.test(s)) return 'product_no'
  if (/^(uom|unit|units|u ?m|unit of measure|unit of measurement|um|measure)$/.test(s)) return 'uom'
  if (/^(item|item no|item #|s ?n|sn|s no|serial|line|line no|line item|#|no|pos|position|sr|sr no)$/.test(s)) return 'item_no'
  return null
}

const UOMS = new Set(['EA', 'EACH', 'PCS', 'PC', 'PCE', 'SET', 'SETS', 'NOS', 'NO', 'M', 'MTR', 'FT', 'KG', 'LOT', 'ROLL', 'BOX', 'PR', 'PAIR', 'L', 'LTR', 'DRUM', 'BAG', 'PKT', 'PACK', 'UNIT', 'UNITS', 'LM', 'MM'])
const NUM = /^[\s$£€₦]*-?\d[\d,]*(\.\d+)?\s*$/

/** With no heading row, guess each column from what's in it. */
function guessByContent(cols: string[][]): Field[] {
  const stats = cols.map(c => {
    const vals = c.map(v => v.trim()).filter(Boolean)
    const n = vals.length || 1
    const numeric = vals.filter(v => NUM.test(v)).length / n
    const decimals = vals.filter(v => /\.\d/.test(v) || /[$£€₦]/.test(v)).length / n
    const uom = vals.filter(v => UOMS.has(v.toUpperCase().replace(/\.$/, ''))).length / n
    const avgLen = vals.reduce((a, v) => a + v.length, 0) / n
    const lines = vals.reduce((a, v) => a + v.split('\n').length, 0) / n
    const maxDigits = Math.max(0, ...vals.map(v => v.replace(/\D/g, '').length))
    const seq = vals.every((v, i) => v === String(i + 1))
    return { numeric, decimals, uom, avgLen, lines, maxDigits, seq, n: vals.length }
  })
  const fields: Field[] = cols.map(() => 'extra')
  const take = (f: Field, i: number) => { if (i >= 0 && fields[i] === 'extra') fields[i] = f }
  // The description: the wordiest column.
  take('description', stats.reduce((b, s, i) => (s.avgLen * s.lines > (stats[b]?.avgLen ?? -1) * (stats[b]?.lines ?? 1) ? i : b), 0))
  stats.forEach((s, i) => { if (s.seq && s.n > 1) take('item_no', i) })
  stats.forEach((s, i) => { if (s.uom >= 0.6) take('uom', i) })
  // Money has decimals or a currency sign; a quantity is a short whole number;
  // a product code is a longer number or a code with letters.
  const free = (i: number) => fields[i] === 'extra'
  const price = stats.findIndex((s, i) => free(i) && s.numeric >= 0.8 && s.decimals >= 0.5)
  take('unit_price', price)
  const qty = stats.findIndex((s, i) => free(i) && s.numeric >= 0.8 && s.maxDigits <= 4)
  take('quantity', qty)
  const code = stats.findIndex((s, i) => free(i) && s.avgLen > 0 && s.avgLen <= 30 && s.lines <= 1.2)
  take('product_no', code)
  return fields
}

/** Match each column to a quote column: by heading, else by content. */
export function guessFields(t: Table): Field[] {
  let fields: Field[]
  if (t.hadHeader) {
    fields = t.headers.map(h => fieldForHeader(h) ?? 'extra')
    // A column named something unknown that holds the long text is the description.
    if (!fields.includes('description')) {
      const cols = t.headers.map((_, i) => t.cells.map(r => plain(r[i] || '')))
      const byContent = guessByContent(cols)
      const d = byContent.indexOf('description')
      if (d >= 0 && fields[d] === 'extra') fields[d] = 'description'
    }
  } else {
    fields = guessByContent(t.headers.map((_, i) => t.cells.map(r => plain(r[i] || ''))))
  }
  // One column per quote column: a second "Qty" becomes a description line.
  const seen = new Set<Field>()
  return fields.map(f => {
    if (!SINGLE.includes(f)) return f
    if (seen.has(f)) return 'extra'
    seen.add(f)
    return f
  })
}

/** Is this row the heading row? Short cells, and at least one recognised heading. */
function looksLikeHeader(row: string[]): boolean {
  const texts = row.map(plain)
  const known = texts.filter(t => fieldForHeader(t) && fieldForHeader(t) !== 'skip' || /\b(total|amount)\b/i.test(t)).length
  return known >= 1 && texts.every(t => t.length <= 40) && !texts.some(t => NUM.test(t) && t.length > 0 && !/^#?$/.test(t))
}

function finish(rows: string[][]): Table | null {
  rows = rows.filter(r => r.some(c => plain(c)))
  if (!rows.length) return null
  const width = Math.max(...rows.map(r => r.length))
  rows = rows.map(r => [...r, ...Array(width - r.length).fill('')])
  // Drop columns that are empty all the way down (spacer cells from Word).
  const keep = Array.from({ length: width }, (_, i) => rows.some(r => plain(r[i])))
  rows = rows.map(r => r.filter((_, i) => keep[i]))
  const hadHeader = looksLikeHeader(rows[0])
  const headers = hadHeader ? rows[0].map(plain) : rows[0].map((_, i) => `Column ${i + 1}`)
  const cells = hadHeader ? rows.slice(1) : rows
  return cells.length ? { headers, cells, hadHeader } : null
}

/** A table from clipboard HTML: the one with the most rows. Null if there's no table. */
export function fromHtml(html: string): Table | null {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const tables = [...doc.querySelectorAll('table')]
  if (!tables.length) return null
  // Copying part of an Outlook table can nest one inside another; the innermost
  // table with real rows is the one meant.
  const best = tables
    .map(t => ({ t, rows: [...t.rows].filter(r => r.closest('table') === t) }))
    .sort((a, b) => b.rows.length - a.rows.length)[0]
  const rows = best.rows.map(tr => [...tr.cells].map(td => clean(td.innerHTML)))
  return finish(rows)
}

/** Tab-separated text (Excel or Google Sheets copied as plain text). */
export function fromTabs(text: string): Table | null {
  const lines = text.replace(/\r/g, '').split('\n').filter(l => l.trim())
  if (lines.length < 1 || !lines.some(l => l.includes('\t'))) return null
  return finish(lines.map(l => l.split('\t').map(c => esc(c.trim()).replace(/\\n/g, '<br>'))))
}

/** A table the server rebuilt from PDF text. */
export function fromRebuilt(headers: string[], rows: string[][]): Table | null {
  const t = finish([headers.map(h => esc(h)), ...rows.map(r => r.map(c => esc(c).replace(/\r?\n/g, '<br>')))])
  if (!t) return null
  // The server always sends headings, even made-up ones.
  return t.hadHeader ? t : { headers: headers.map(String), cells: rows.map(r => r.map(c => esc(c).replace(/\r?\n/g, '<br>'))), hadHeader: true }
}

// The quote form offers only these units; anything else pasted would be saved
// as one thing and shown as another. Common spellings map onto them; a unit
// with no match becomes EA and keeps its name in the description.
export const QUOTE_UOMS = ['EA', 'PC', 'KIT', 'ROLL', 'BOX', 'MTR', 'GL', 'SET', 'PACK']
const UOM_ALIASES: Record<string, string> = {
  EACH: 'EA', UNIT: 'EA', UNITS: 'EA', EA: 'EA',
  PC: 'PC', PCS: 'PC', PCE: 'PC', PCES: 'PC', PIECE: 'PC', PIECES: 'PC', NO: 'PC', NOS: 'PC', NR: 'PC', NUMBER: 'PC',
  KIT: 'KIT', KITS: 'KIT', ROLL: 'ROLL', ROLLS: 'ROLL', RL: 'ROLL', BOX: 'BOX', BOXES: 'BOX', BX: 'BOX',
  M: 'MTR', MTR: 'MTR', MTRS: 'MTR', METER: 'MTR', METERS: 'MTR', METRE: 'MTR', METRES: 'MTR', LM: 'MTR',
  GL: 'GL', GAL: 'GL', GALLON: 'GL', GALLONS: 'GL',
  SET: 'SET', SETS: 'SET', PACK: 'PACK', PACKS: 'PACK', PK: 'PACK', PKT: 'PACK', PKTS: 'PACK', PACKET: 'PACK', PACKETS: 'PACK',
}
export function quoteUom(raw: string): { uom: string; note: string } {
  const u = raw.trim().toUpperCase().replace(/[.\s]+$/, '')
  if (!u) return { uom: 'EA', note: '' }
  const hit = UOM_ALIASES[u]
  return hit ? { uom: hit, note: '' } : { uom: 'EA', note: raw.trim() }
}

export function num(s: string): number {
  const m = plain(s).replace(/,/g, '').match(/-?\d+(\.\d+)?/)
  return m ? parseFloat(m[0]) : 0
}

/** The quote lines, for the rows the person kept. */
export function toItems(t: Table, fields: Field[], keep: boolean[], productInDescription = false): Item[] {
  // Only NLNG quotes have a Product no. column; elsewhere the number would be
  // stored but never seen or printed, so it opens the description instead.
  const pnLabel = t.hadHeader && fields.indexOf('product_no') >= 0 ? t.headers[fields.indexOf('product_no')] : 'Product no.'
  return t.cells.filter((_, i) => keep[i]).map(row => {
    const at = (f: Field) => fields.indexOf(f)
    const cell = (f: Field) => (at(f) >= 0 ? row[at(f)] || '' : '')
    // Columns the quote has no place for become extra lines in the description.
    const extras = fields.map((f, i) => (f === 'extra' && plain(row[i] || '') ? `${esc(t.hadHeader ? t.headers[i] : 'Note')}: ${esc(plain(row[i]).replace(/\n+/g, ' '))}` : ''))
      .filter(Boolean)
    const pn = plain(cell('product_no')).replace(/\n+/g, ' ')
    const lead = productInDescription && pn ? [`${esc(pnLabel.toUpperCase())}: ${esc(pn)}`] : []
    const unit = quoteUom(plain(cell('uom')))
    const unitNote = unit.note ? [`Unit: ${esc(unit.note)}`] : []
    const desc = [...lead, cell('description'), ...extras, ...unitNote].filter(Boolean).join('<br>')
    const qty = num(cell('quantity'))
    return {
      product_no: pn,
      description: desc,
      uom: unit.uom,
      quantity: qty > 0 ? qty : 1,
      unit_price: num(cell('unit_price')),
    }
  })
}
