import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError, send } from '../../lib/api'
import { FIELD_LABEL, type Field, type Item, SINGLE, type Table, fieldForHeader, fromHtml, fromRebuilt, fromTabs, guessFields, headKey, toItems } from './pasteTable'
import { type Col, labelOf } from './quoteColumns'

/*
  "Paste items": copy a table from an email, Word, Excel or a PDF, paste it
  here, check how its columns were matched, and add the rows to the quote.
  Nothing reaches the quote until the person presses Add or Replace.
*/

const CHOICES: Field[] = ['product_no', 'description', 'uom', 'quantity', 'unit_price', 'extra', 'column', 'skip']

/**
  The guesses, checked against the quote's own columns. A heading with the
  same name as one of them ("DDP Unit Price (USD)" and "DDP unit price") goes
  into it; one named like a worked-out total is skipped, as the quote works
  that out itself. A quote with no second price yet takes a second price
  column as one, so a pasted FCA + DDP table lands as FCA + DDP.
*/
function matchQuote(t: Table, guessed: Field[], targets: Col[], canPrice2: boolean): Field[] {
  const fields = [...guessed]
  t.headers.forEach((h, i) => {
    if (!t.hadHeader) return
    const hit = targets.find(c => headKey(labelOf(c)) === headKey(h))
    if (hit) fields[i] = hit.kind === 'total' ? 'skip' : `col:${hit.key}`
  })
  if (canPrice2 && t.hadHeader && fields.includes('unit_price') && !fields.includes('price2')) {
    const i = t.headers.findIndex((h, j) => fields[j] === 'extra' && fieldForHeader(h) === 'unit_price')
    if (i >= 0) fields[i] = 'price2'
  }
  return fields
}

const plainText = (html: string) => {
  const d = document.createElement('div')
  d.innerHTML = html.replace(/<br\s*\/?>/gi, '\n')
  return d.textContent || ''
}

export default function PasteItems({ lineCount, showsProductNo, targets, canPrice2, onAdd, onFill, onClose }: {
  lineCount: number                    // filled lines already on the quote
  showsProductNo: boolean              // only NLNG quotes have a Product no. column
  targets: Col[]                       // the columns this quote added, which a pasted column can go into
  canPrice2: boolean                   // no second price yet, and room for one
  onAdd: (items: Item[], replace: boolean) => void
  // Put the pasted columns into the lines already there, top to bottom.
  onFill: (items: Item[], cols: Field[][]) => void   // cols: what each row has a value for
  onClose: () => void
}) {
  const hasLines = lineCount > 0
  const [table, setTable] = useState<Table | null>(null)
  const [fields, setFields] = useState<Field[]>([])
  const [keep, setKeep] = useState<boolean[]>([])
  const [reading, setReading] = useState(false)
  const [err, setErr] = useState('')
  const zone = useRef<HTMLDivElement>(null)

  useEffect(() => { zone.current?.focus() }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !reading) { e.stopPropagation(); onClose() } }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [reading, onClose])

  const use = (t: Table) => {
    setTable(t); setFields(matchQuote(t, guessFields(t), targets, canPrice2)); setKeep(t.cells.map(() => true)); setErr('')
  }

  // A paste, or a table dragged onto the box: both carry the same data.
  const onPaste = (e: React.ClipboardEvent) => { e.preventDefault(); if (!reading) read(e.clipboardData) }
  const onDrop = (e: React.DragEvent) => { e.preventDefault(); if (!reading) read(e.dataTransfer) }

  const read = async (data: DataTransfer) => {
    setErr('')
    const html = data.getData('text/html')
    const text = data.getData('text/plain')
    // A real table (email, Word, Excel, web) is read here; tab-separated text too.
    const t = (html && fromHtml(html)) || (text && fromTabs(text))
    if (t) { use(t); return }
    // A table came over but with nothing under its headings: say so, rather
    // than send the headings off to be "rebuilt" into nothing.
    if (/<table/i.test(html) || (text.includes('\t') && text.trim().split('\n').length < 2)) {
      setErr('Only the headings were copied. Copy the item rows too, then paste again.'); return
    }
    if (!text.trim()) { setErr('Nothing to read in that paste. Copy the table and try again.'); return }
    // No structure left - text from a PDF. Have the server rebuild the table.
    setReading(true)
    try {
      const r = await send<{ headers: string[]; rows: string[][] }>('POST', '/api/quotations/parse-paste', { text })
      const rebuilt = fromRebuilt(r.headers, r.rows)
      if (rebuilt) use(rebuilt)
      else setErr('No table could be found in what was pasted.')
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : 'The table could not be read. Try again.')
    } finally { setReading(false) }
  }

  const items = useMemo(() => (table ? toItems(table, fields, keep, !showsProductNo) : []), [table, fields, keep, showsProductNo])
  const count = items.length
  const noDesc = table && !fields.includes('description')
  // What a fill would change: every quote column the paste was matched to,
  // and per row only the cells that have something in them.
  const fillCols = fields.filter(f => !['item_no', 'skip'].includes(f))
  const hasValue = useMemo(() => (table ? table.cells.filter((_, i) => keep[i])
    .map(r => fillCols.filter(f => fields.some((x, j) => x === f && plainText(r[j] || '').trim()))) : []),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [table, fields, keep])
  const fills = Math.min(count, lineCount)

  const setField = (i: number, f: Field) => setFields(fs => fs.map((x, j) => {
    if (j === i) return f
    // Only one column can be the description, the qty, and so on.
    return x === f && SINGLE(f) ? 'extra' : x
  }))

  return createPortal(
    <div className="cell-overlay" onMouseDown={e => { if (e.target === e.currentTarget && !reading) onClose() }}>
      <div className={`cell-box paste-box${table ? ' wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="pi-hd">
        <div className="cell-hd">
          <h2 id="pi-hd">Paste items</h2>
          <button type="button" className="cell-close" onClick={onClose} disabled={reading} aria-label="Close">×</button>
        </div>

        {!table ? (
          <>
            <p className="pi-help">Copy the item table from an email, Word, Excel or a PDF, then paste it below. You don't need every column: the ones the quote uses are matched, and any others are added to each item's description.</p>
            <div ref={zone} className={`pi-zone${reading ? ' busy' : ''}`} contentEditable={!reading} suppressContentEditableWarning
                 role="textbox" aria-label="Paste the table here" tabIndex={0} onPaste={onPaste}
                 onDrop={onDrop} onDragOver={e => e.preventDefault()}
                 onKeyDown={e => { if (!(e.ctrlKey || e.metaKey) && e.key.length === 1) e.preventDefault() }}>
              {reading ? <span className="pi-reading"><span className="orbs" aria-hidden="true"><i /><i /><i /></span>Reading the table…</span>
                : <span className="pi-hint">Click here and press <kbd>Ctrl</kbd>+<kbd>V</kbd></span>}
            </div>
            {err && <p className="err-text" role="alert">{err}</p>}
          </>
        ) : (
          <>
            <p className="pi-help">
              {table.cells.length} row{table.cells.length === 1 ? '' : 's'} found. Check what each column goes into - change any that's wrong - and untick rows you don't want.
            </p>
            {noDesc && (hasLines
              ? <p className="pi-note">No Description column, so these can only fill the {lineCount} line{lineCount === 1 ? '' : 's'} already on the quote, from the top. To add new lines, set a column as the Description.</p>
              : <p className="err-text">No column is set as the Description - pick one above the item text.</p>)}
            <div className="pi-scroll">
              <table className="pi-table">
                <thead>
                  <tr>
                    <th className="pi-keep">
                      <input type="checkbox" aria-label="Keep all rows" checked={keep.every(Boolean)}
                             onChange={e => setKeep(keep.map(() => e.target.checked))} />
                    </th>
                    {table.headers.map((h, i) => (
                      <th key={i}>
                        <select value={fields[i]} onChange={e => setField(i, e.target.value as Field)}
                                className={`pi-pick f-${fields[i]}`} aria-label={`What "${h}" is`}>
                          {CHOICES.map(c => <option key={c} value={c}>{FIELD_LABEL[c as keyof typeof FIELD_LABEL]}</option>)}
                          {canPrice2 && <option value="price2">{FIELD_LABEL.price2}</option>}
                          {fields[i] === 'item_no' && <option value="item_no">{FIELD_LABEL.item_no}</option>}
                          {targets.some(c => c.kind !== 'total') && (
                            <optgroup label="This quote's columns">
                              {targets.filter(c => c.kind !== 'total').map(c => <option key={c.key} value={`col:${c.key}`}>{labelOf(c)}</option>)}
                            </optgroup>
                          )}
                        </select>
                        <span className="pi-src">{h}</span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.cells.map((r, ri) => (
                    <tr key={ri} className={keep[ri] ? '' : 'off'}>
                      <td className="pi-keep">
                        <input type="checkbox" aria-label={`Keep row ${ri + 1}`} checked={keep[ri]}
                               onChange={e => setKeep(k => k.map((x, j) => (j === ri ? e.target.checked : x)))} />
                      </td>
                      {r.map((c, ci) => (
                        <td key={ci} className={`f-${fields[ci]}`}><div className="pi-cell">{plainText(c)}</div></td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="cell-btns pi-btns">
              <button type="button" className="btn ghost" onClick={() => setTable(null)}>Paste again</button>
              <span className="sp" />
              {hasLines && (
                <button type="button" className={`btn${noDesc ? '' : ' ghost'}`} disabled={!fills || !fillCols.length}
                        title="Put these columns into the lines already on the quote, top to bottom"
                        onClick={() => onFill(items, hasValue)}>
                  Fill {fills} existing line{fills === 1 ? '' : 's'}
                </button>
              )}
              {hasLines && !noDesc && (
                <button type="button" className="btn ghost" disabled={!count}
                        onClick={() => onAdd(items, true)}>Replace all lines</button>
              )}
              {!(hasLines && noDesc) && (
                <button type="button" className="btn" disabled={!count || !!noDesc} onClick={() => onAdd(items, false)}>
                  {hasLines ? `Add ${count} after existing lines` : `Add ${count} item${count === 1 ? '' : 's'}`}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  )
}
