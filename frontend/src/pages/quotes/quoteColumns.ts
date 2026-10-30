/*
  The line-item columns of one quote. Most quotes never touch this and keep
  the standard layout (saved as null). A quote that needs something unusual -
  a Lead time or Brand column, a renamed heading - saves its own list, and the
  form and the PDF both draw their table from it, in its order.

  Built-in columns hold the figures the quote is worked out from, so only UOM
  and Product No. can be removed; the rest can be renamed and moved. Added
  columns are plain text, one value per line, kept in the line's `extra`.
*/

export type BuiltIn = 'description' | 'product_no' | 'uom' | 'quantity' | 'unit_price' | 'line_total'
// label '' = the standard name. kind: an added column is text, unless it's part
// of a second price (FCA and DDP side by side): 'price' is typed per line,
// 'total' is worked out - qty x that price - and `of` names its price column.
export type Col = { key: string; label: string; kind?: 'price' | 'total'; of?: string }

export const DEFAULT_LABEL: Record<BuiltIn, string> = {
  description: 'Item description', product_no: 'Product No.', uom: 'U.O.M', quantity: 'QTY', unit_price: 'Unit price', line_total: 'Total',
}
export const REMOVABLE: string[] = ['product_no', 'uom']
// Room on an A4 page: past this the description column gets too narrow to read.
export const MAX_ADDED = 4

export const isBuiltIn = (key: string): key is BuiltIn => key in DEFAULT_LABEL
export const isAdded = (key: string) => !isBuiltIn(key)

export function defaultCols(nlng: boolean): Col[] {
  const keys: BuiltIn[] = ['description', ...(nlng ? ['product_no' as BuiltIn] : []), 'uom', 'quantity', 'unit_price', 'line_total']
  return keys.map(key => ({ key, label: '' }))
}

export const labelOf = (c: Col) => c.label.trim() || (isBuiltIn(c.key) ? DEFAULT_LABEL[c.key] : 'Column')

/** A key for a new column that isn't already used on this quote. */
export function newKey(cols: Col[]): string {
  let i = 1
  while (cols.some(c => c.key === 'x' + i)) i++
  return 'x' + i
}

/** The second price on a quote, if it has one: its price and total columns. */
export function secondPrice(cols: Col[]): { price: Col; total: Col } | null {
  const total = cols.find(c => c.kind === 'total')
  const price = total && cols.find(c => c.key === total.of && c.kind === 'price')
  return price && total ? { price, total } : null
}

/** The pair "Add a second price" puts in: a price typed per line and its total. */
export function newPricePair(cols: Col[]): Col[] {
  const p = newKey(cols)
  const t = newKey([...cols, { key: p, label: '' }])
  return [{ key: p, label: 'DDP unit price', kind: 'price' }, { key: t, label: 'DDP total', kind: 'total', of: p }]
}

/** A number typed or pasted into a price cell: "1,500.00", "$700". */
export const priceOf = (v: string | undefined) => parseFloat(String(v ?? '').replace(/[^\d.-]/g, '')) || 0

/** A saved layout, checked: unknown shapes are dropped, the built-ins that
    can't be removed are put back if missing, so a bad save never hides a price. */
export function readCols(raw: unknown, nlng: boolean): Col[] | null {
  if (!Array.isArray(raw) || !raw.length) return null
  const seen = new Set<string>()
  const cols: Col[] = []
  for (const c of raw) {
    const key = String((c as Col)?.key || '')
    if (!key || seen.has(key) || !(isBuiltIn(key) || /^x\d+$/.test(key))) continue
    seen.add(key)
    const col: Col = { key, label: String((c as Col)?.label || '').slice(0, 40) }
    const kind = (c as Col)?.kind
    if (isAdded(key) && (kind === 'price' || kind === 'total')) col.kind = kind
    if (col.kind === 'total') col.of = String((c as Col)?.of || '')
    cols.push(col)
  }
  // Half a pair is just a text column.
  const pair = secondPrice(cols)
  for (const c of cols) if (c.kind && c !== pair?.price && c !== pair?.total) { delete c.kind; delete c.of }
  for (const d of defaultCols(nlng)) if (!REMOVABLE.includes(d.key) && !seen.has(d.key)) cols.push(d)
  return cols
}
