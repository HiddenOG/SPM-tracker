import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getBlob, getJson, readDataUrl, saveBlob, send } from '../../lib/api'
import { currentUser } from '../../lib/session'
import { todayLocal } from '../../lib/orders'
import { clean, esc, notesToHtml } from './rich'
import { FormatBar, Rich } from './RichEditor'
import PasteItems from './PasteItems'
import type { Field, Item } from './pasteTable'
import ColumnHead, { type Action } from './ColumnHead'
import ConfirmBox from '../../lib/ConfirmBox'
import { type Col, DEFAULT_LABEL, MAX_ADDED, REMOVABLE, defaultCols, isAdded, labelOf, newKey, newPricePair, priceOf, readCols, secondPrice } from './quoteColumns'
import { type Company, COMPANY_NAME, downloadQuotePdf, readCompany, sym } from './QuotesPage'
import './quotes.css'

/*
  One quotation, new or saved: a port of the form half of the quotes module
  in web/script.js. Every figure is worked out the same way, and the old
  safety rules hold:
    - a failed save never clears the form - it may be the only copy;
    - a failed or empty price import never empties the line items;
    - a PDF is built from the saved record, so it is always saved first, and
      no PDF is made if that save fails.
*/

// extra: the values in this quote's added columns, by column key.
type Row = { _k: number; product_no: string; description: string; uom: string; quantity: number; unit_price: number; tax_rate: number; line_total: number; extra: Record<string, string> }

const CLIENTS: [string, string][] = [['chevron', 'Chevron Nigeria Limited'], ['nlng', 'NLNG'], ['seplat', 'SEPLAT'], ['exxon', 'ExxonMobil'], ['total', 'TotalEnergies'], ['others', 'Others']]
const CLIENT_DEFAULT: Record<string, { company: string; dest: string; country: string }> = {
  chevron: { company: 'Chevron Nigeria Limited', dest: 'Your Chevron Warri Warehouse', country: 'United States / United Kingdom' },
  nlng: { company: 'Nigeria LNG Limited', dest: 'NLNG Plant, Bonny Island', country: 'United Kingdom' },
  seplat: { company: 'Seplat Petroleum Development Company', dest: '', country: '' },
  exxon: { company: 'ExxonMobil Nigeria Limited', dest: '', country: 'United States' },
  total: { company: 'TotalEnergies EP Nigeria Limited', dest: '', country: 'France / United Kingdom' },
  others: { company: '', dest: '', country: '' },
}
const UOMS = ['EA', 'PC', 'KIT', 'ROLL', 'BOX', 'MTR', 'GL', 'SET', 'PACK']
const SHIPPING = ['Sea', 'Air', 'Road', 'DDP', 'Not Applicable']
// Terms & Conditions fields with their own show/hide eye; saved as show_<field>.
const TC: [string, string][] = [['lead_time', 'Lead Time'], ['delivery_dest', 'Delivery Destination'], ['country_import', 'Country of Import'],
  ['shipping_mode', 'Mode of Shipping'], ['manufacturer', 'Manufacturer'], ['weight', 'Total Weight'], ['currency', 'Currency']]

type Fields = Record<string, string>
function blankFields(company: Company): Fields {
  return {
    company, client: '', client_name: '', quote_number: '', reference_po: '', quote_date: todayLocal(), validity_days: '30',
    prepared_by: currentUser()?.name || '', recipient_name: '', recipient_dept: '', recipient_company: '', recipient_email: '',
    recipient_tel: '', subject: '', lead_time: '', delivery_dest: '', country_import: '', shipping_mode: 'Sea', manufacturer: '',
    weight: '', currency: 'USD', discount_pct: '0', markup_pct: '0', vat_pct: '0', shipping_charges: '0',
  }
}
let keySeq = 1
const newRow = (r: Partial<Row> = {}): Row => ({ _k: keySeq++, product_no: '', description: '', uom: 'EA', quantity: 1, unit_price: 0, tax_rate: 0, line_total: 0, extra: {}, ...r })
/** A saved line's added-column values, as plain strings. */
const readExtra = (v: unknown): Record<string, string> =>
  v && typeof v === 'object' ? Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, String(x ?? '')])) : {}
// Form widths (%) of the built-in columns; the description takes what's left.
const COL_W: Record<string, number> = { product_no: 15, uom: 7, quantity: 8, unit_price: 15, line_total: 13 }
const ADDED_W = 11
/** A line with something in it - not the blank starter row. */
const filled = (r: Row) => !!(r.product_no.trim() || r.description.replace(/<[^>]*>|&nbsp;/g, '').trim() || Number(r.unit_price))
const lineTotal = (r: Row) => Math.round((Number(r.quantity) || 0) * (Number(r.unit_price) || 0) * (1 + (Number(r.tax_rate) || 0)) * 100) / 100
const n = (v: string) => parseFloat(v) || 0

function Eye({ on, onToggle, what }: { on: boolean; onToggle: () => void; what: string }) {
  return (
    <button type="button" className={`eye${on ? '' : ' off'}`} onClick={onToggle} aria-pressed={on}
            title={on ? `${what} shown in PDF — click to hide` : `${what} hidden from PDF — click to show`}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {on ? <><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></>
          : <path d="M17.94 17.94A10 10 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9 9 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19M1 1l22 22" />}
      </svg>
    </button>
  )
}

export default function QuoteForm() {
  const { id: routeId } = useParams()
  const nav = useNavigate()
  const isNewRoute = !routeId || routeId === 'new'
  const [qid, setQid] = useState<string | null>(isNewRoute ? null : routeId!)
  const [f, setF] = useState<Fields>(() => blankFields(readCompany()))
  const [tcShow, setTcShow] = useState<Record<string, boolean>>(() => Object.fromEntries(TC.map(([k]) => [k, true])))
  const [markupShow, setMarkupShow] = useState(true)
  const [vatShow, setVatShow] = useState(true)
  const [rows, setRows] = useState<Row[]>(() => (isNewRoute ? [newRow()] : []))
  const [notesInit, setNotesInit] = useState(isNewRoute ? 'Looking forward to your business.' : '')
  const notes = useRef(notesInit)
  const [formKey, setFormKey] = useState(0)                 // remounts the rich cells after a load
  const [loading, setLoading] = useState(!isNewRoute)
  const [loadErr, setLoadErr] = useState('')
  const [saving, setSaving] = useState<'idle' | 'saving' | 'saved'>('idle')
  const [pdfState, setPdfState] = useState('')
  const [banner, setBanner] = useState<{ ok: boolean; msg: string } | null>(null)
  const [importing, setImporting] = useState('')
  const [pasting, setPasting] = useState(false)
  // This quote's own column layout; null = the standard one.
  const [cols, setCols] = useState<Col[] | null>(null)
  const hadCols = useRef(false)                             // the saved quote had its own layout
  const [renameKey, setRenameKey] = useState('')            // a just-added column, named straight away
  const [dropCol, setDropCol] = useState<Col | null>(null)  // an added column with values, waiting on "are you sure"
  const enquiryInput = useRef<HTMLInputElement>(null)
  const priceInput = useRef<HTMLInputElement>(null)
  const status = useRef('draft')

  const set = (k: string, v: string) => setF(x => ({ ...x, [k]: v }))
  const isNlng = f.client === 'nlng'
  const layout = cols ?? defaultCols(isNlng)
  const hasProductNo = layout.some(c => c.key === 'product_no')
  const addedKeys = useMemo(() => (cols || []).filter(c => isAdded(c.key)).map(c => c.key), [cols])
  const addedCount = addedKeys.length
  const pair = secondPrice(layout)
  // The grand total takes the Total column's name once it's renamed ("FCA Extended Total").
  const grandLabel = (layout.find(c => c.key === 'line_total')?.label.trim() || 'Total').toUpperCase()
  const canPrice = !pair && addedCount <= MAX_ADDED - 2
  /** A line's total at the second price: qty x that price. */
  const total2 = (r: Row) => (pair ? Math.round((Number(r.quantity) || 0) * priceOf(r.extra[pair.price.key]) * 100) / 100 : 0)
  const s = sym(f.currency)
  const money = (v: number) => s + (Number(v) || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

  // ── loading a saved quote ──
  useEffect(() => {
    if (isNewRoute) return
    let gone = false
    getJson<Record<string, unknown> & { line_items?: Partial<Row>[] }>(`/api/quotations/${routeId}`).then(q => {
      if (gone) return
      const str = (k: string, d = '') => (q[k] == null ? d : String(q[k]))
      setF({
        company: str('company', 'spm'), client: str('client'), client_name: q.client === 'others' ? str('recipient_company') : '',
        quote_number: str('quote_number'), reference_po: str('reference_po'), quote_date: str('quote_date').slice(0, 10),
        validity_days: String(q.validity_days || 30), prepared_by: str('prepared_by'), recipient_name: str('recipient_name'),
        recipient_dept: str('recipient_dept'), recipient_company: str('recipient_company'), recipient_email: str('recipient_email'),
        recipient_tel: str('recipient_tel'), subject: str('subject'), lead_time: str('lead_time'), delivery_dest: str('delivery_dest'),
        country_import: str('country_import'), shipping_mode: str('shipping_mode', 'Sea'), manufacturer: str('manufacturer'),
        weight: str('weight'), currency: str('currency', 'USD'), discount_pct: String(q.discount_pct || 0),
        markup_pct: String(q.markup_pct || 0), vat_pct: String(q.vat_pct || 0), shipping_charges: String(q.shipping_charges || 0),
      })
      status.current = str('status', 'draft')
      // Anything absent on an older quote counts as shown.
      setTcShow(Object.fromEntries(TC.map(([k]) => [k, q['show_' + k] == null ? true : !!q['show_' + k]])))
      setMarkupShow(q.markup_visible !== false)
      setVatShow(q.vat_visible !== false)
      setRows((q.line_items || []).map(it => newRow({
        product_no: it.product_no || '', description: it.description || '', uom: it.uom || 'EA',
        quantity: Number(it.quantity) || 1, unit_price: Number(it.unit_price) || 0, tax_rate: Number(it.tax_rate) || 0, line_total: Number(it.line_total) || 0,
        extra: readExtra(it.extra),
      })))
      const saved = readCols(q.line_columns, q.client === 'nlng')
      setCols(saved); hadCols.current = !!saved
      const nh = notesToHtml(q.notes as string)
      notes.current = nh; setNotesInit(nh); setFormKey(k => k + 1)
      setLoading(false)
    }, e => { if (!gone) { setLoadErr('Could not load quote: ' + (e as Error).message); setLoading(false) } })
    return () => { gone = true }
  }, [routeId, isNewRoute])

  // ── totals (recalcTotals in script.js) ──
  const t = useMemo(() => {
    const sub = rows.reduce((a, r) => a + (Number(r.line_total) || 0), 0)
    const da = sub * n(f.discount_pct) / 100
    const ma = sub * n(f.markup_pct) / 100
    // VAT is on the goods after discount and markup, before shipping.
    const va = (sub - da + ma) * n(f.vat_pct) / 100
    const ship = n(f.shipping_charges)
    // The second price's total gets the same discount, markup and VAT, but
    // never the shipping charge: a delivered (DDP) price already includes it.
    const sub2 = rows.reduce((a, r) => a + total2(r), 0)
    const goods2 = sub2 - sub2 * n(f.discount_pct) / 100 + sub2 * n(f.markup_pct) / 100
    return { sub, da, ma, va, ship, total: sub - da + ma + va + ship, total2: goods2 + goods2 * n(f.vat_pct) / 100 }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, f.discount_pct, f.markup_pct, f.vat_pct, f.shipping_charges, pair?.price.key])

  // ── client choice fills in what is still blank ──
  const pickClient = (c: string) => {
    setF(x => {
      const d = CLIENT_DEFAULT[c]
      return {
        ...x, client: c, client_name: c === 'others' ? x.client_name : '',
        recipient_company: x.recipient_company || d?.company || '', delivery_dest: x.delivery_dest || d?.dest || '',
        country_import: x.country_import || d?.country || '',
      }
    })
  }

  const updateRow = (k: number, patch: Partial<Row>) => setRows(rs => rs.map(r => {
    if (r._k !== k) return r
    const next = { ...r, ...patch }
    return 'quantity' in patch || 'unit_price' in patch || 'tax_rate' in patch ? { ...next, line_total: lineTotal(next) } : next
  }))

  // ── this quote's columns ──
  // The first change copies the standard layout; from then on the quote keeps its own.
  const editCols = (fn: (c: Col[]) => Col[]) => setCols(c => fn(c ?? defaultCols(isNlng)))
  const renameCol = (key: string, label: string) => editCols(cs => cs.map(c => {
    if (c.key !== key) return c
    // A blank name puts a built-in heading back to its standard name; an added column keeps its old one.
    return { ...c, label: label || (isAdded(key) ? c.label : '') }
  }))
  // A second price goes as a pair: its total means nothing without it.
  const dropKeys = (key: string) => {
    const col = layout.find(c => c.key === key)
    return col?.kind && pair ? [pair.price.key, pair.total.key] : [key]
  }
  const removeCol = (key: string) => { const gone = dropKeys(key); editCols(cs => cs.filter(c => !gone.includes(c.key))) }
  const colAction = (key: string, a: Action) => {
    if (a === 'remove') {
      const col = layout.find(c => c.key === key)
      // An added column's values go with it, so ask first if there are any.
      if (col && isAdded(key) && rows.some(r => dropKeys(key).some(k => (r.extra[k] || '').trim()))) setDropCol(col)
      else removeCol(key)
      return
    }
    editCols(cs => {
      const i = cs.findIndex(c => c.key === key)
      const next = [...cs]
      if (a === 'add-price') {
        if (secondPrice(cs) || cs.filter(c => isAdded(c.key)).length > MAX_ADDED - 2) return cs
        next.splice(i + 1, 0, ...newPricePair(cs))
        return next
      }
      if (a === 'left' || a === 'right') {
        const j = a === 'left' ? i - 1 : i + 1
        if (i < 0 || j < 0 || j >= cs.length) return cs
        ;[next[i], next[j]] = [next[j], next[i]]
        return next
      }
      if (cs.filter(c => isAdded(c.key)).length >= MAX_ADDED) return cs
      const k = newKey(cs)
      next.splice(a === 'add-left' ? i : i + 1, 0, { key: k, label: 'New column' })
      setRenameKey(k)
      return next
    })
  }
  const addColAtEnd = () => {
    if (addedCount >= MAX_ADDED) return
    const k = newKey(layout)
    editCols(cs => [...cs, { key: k, label: 'New column' }])
    setRenameKey(k)
  }
  // UOM or Product No. taken off earlier: put back where it normally sits.
  const restoreCol = (key: string) => editCols(cs => {
    const after = key === 'product_no' ? 'description' : cs.some(c => c.key === 'product_no') ? 'product_no' : 'description'
    const i = cs.findIndex(c => c.key === after)
    const next = [...cs]
    next.splice(i + 1, 0, { key, label: '' })
    return next
  })
  // Product No. is only standard on NLNG quotes; elsewhere it was never there to put back.
  const removedBuiltIns = REMOVABLE.filter(k => !layout.some(c => c.key === k) && (k !== 'product_no' || isNlng))

  /** Where pasted "New column" values go: a column of that name already on the
      quote, a new one, or - past the limit - a line in the description. */
  const placePasted = (items: Item[]) => {
    const next = [...layout]
    const keyOf: Record<string, string | null> = {}
    // New columns go in after the text columns, before UOM and the figures.
    let at = next.findIndex(c => ['uom', 'quantity', 'unit_price', 'line_total'].includes(c.key))
    if (at < 0) at = next.length
    for (const label of [...new Set(items.flatMap(it => it.cols.map(c => c.label)))]) {
      const have = next.find(c => isAdded(c.key) && labelOf(c).toLowerCase() === label.trim().toLowerCase())
      if (have) { keyOf[label] = have.key; continue }
      if (next.filter(c => isAdded(c.key)).length >= MAX_ADDED) { keyOf[label] = null; continue }
      const k = newKey(next)
      next.splice(at++, 0, { key: k, label: label.trim().slice(0, 40) || 'New column' })
      keyOf[label] = k
    }
    const changed = next.length !== layout.length
    if (changed) setCols(next)
    const spill = Object.values(keyOf).filter(v => v === null).length
    const apply = (it: Item, r: { description: string; extra: Record<string, string> }) => {
      const extra = { ...r.extra }
      const notes: string[] = []
      for (const c of it.cols) {
        if (!c.value.trim()) continue
        const k = keyOf[c.label]
        const kind = k ? next.find(x => x.key === k)?.kind : undefined
        if (kind === 'total') continue                         // worked out from the price, never pasted over
        if (k) extra[k] = kind === 'price' ? String(priceOf(c.value)) : c.value.trim()
        else notes.push(`${esc(c.label)}: ${esc(c.value.trim())}`)
      }
      return { extra, description: [r.description, ...notes].filter(Boolean).join('<br>') }
    }
    return { apply, spill }
  }

  // ── what goes to the server (_collectQData) ──
  const collect = useCallback((st: string) => {
    const sub = rows.reduce((a, r) => a + (Number(r.line_total) || 0), 0)
    const goods = sub - sub * n(f.discount_pct) / 100 + sub * n(f.markup_pct) / 100
    const total = Math.round((goods + goods * n(f.vat_pct) / 100 + n(f.shipping_charges)) * 100) / 100
    const g = (k: string) => (f[k] || '').trim()
    return {
      company: g('company') || 'spm', client: g('client'), status: st || 'draft', reference_po: g('reference_po'),
      prepared_by: g('prepared_by'), quote_date: g('quote_date'), validity_days: parseInt(g('validity_days')) || 30,
      recipient_name: g('recipient_name'), recipient_dept: g('recipient_dept'), recipient_company: g('recipient_company'),
      recipient_email: g('recipient_email'), recipient_tel: g('recipient_tel'), subject: g('subject'), lead_time: g('lead_time'),
      delivery_dest: g('delivery_dest'), country_import: g('country_import'), shipping_mode: g('shipping_mode'),
      manufacturer: g('manufacturer'), weight: g('weight'), currency: g('currency') || 'USD',
      discount_pct: n(f.discount_pct), markup_pct: n(f.markup_pct), markup_visible: markupShow,
      vat_pct: n(f.vat_pct), vat_visible: vatShow, shipping_charges: n(f.shipping_charges),
      sub_total: Math.round(sub * 100) / 100, total, notes: clean(notes.current),
      // Cleaned once more on the way out: typing can leave markup the paste
      // cleaner never saw, such as the "<b><br></b>" a blank bold line produces.
      line_items: rows.map((r, i) => ({
        item_no: i + 1, product_no: r.product_no || '', description: clean(r.description || ''), uom: r.uom || 'EA',
        quantity: Number(r.quantity) || 0, unit_price: Number(r.unit_price) || 0, tax_rate: Number(r.tax_rate) || 0, line_total: Number(r.line_total) || 0,
        // Only the columns still on the quote; a removed column's values go with it.
        // A second price's total is saved as worked out, so it reads right anywhere.
        ...(addedKeys.length ? { extra: Object.fromEntries(addedKeys.map(k => [k, k === pair?.total.key ? total2(r).toFixed(2) : (r.extra[k] || '').trim()])) } : {}),
      })),
      // Sent only by a quote that has (or had) its own layout, so a standard
      // quote saves exactly as it always did.
      ...(cols || hadCols.current ? { line_columns: cols } : {}),
      ...Object.fromEntries(TC.map(([k]) => ['show_' + k, tcShow[k]])),
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, f, markupShow, vatShow, tcShow, cols, addedKeys])

  const save = async () => {
    if (!f.client) { alert('Please select a client before saving.'); return }
    setSaving('saving')
    try {
      if (!qid) {
        const r = await send<{ id: string; quote_number?: string }>('POST', '/api/quotations', collect('draft'))
        setQid(r.id)
        if (r.quote_number) set('quote_number', r.quote_number)
        nav(`/quotes/${r.id}`, { replace: true })
      } else {
        const r = await send<{ quote_number?: string }>('PUT', `/api/quotations/${qid}`, collect('draft'))
        // The number follows the client if it was changed on a saved draft.
        if (r?.quote_number) set('quote_number', r.quote_number)
      }
      status.current = 'draft'
      setSaving('saved')
      setTimeout(() => setSaving('idle'), 2000)
    } catch (e) {
      // Never clear the form on a failed save - it may be the only copy of the lines.
      alert('Save failed — your quote has NOT been saved.\n\n' + (e as Error).message + '\n\nEverything is still on screen. Press Save again.')
      setSaving('idle')
    }
  }

  const pdf = async () => {
    if (!qid) return
    // The PDF is built from the SAVED record, so save first - keeping the
    // quote's status - and make no PDF at all if that save fails.
    setPdfState('Saving…')
    try {
      await send('PUT', `/api/quotations/${qid}`, collect(status.current))
    } catch (e) {
      alert('NOT saved, so no PDF was generated — it would have been built from the previous version.\n\n' + (e as Error).message + '\n\nYour changes are still on screen. Press Save, then try again.')
      setPdfState(''); return
    }
    setPdfState('Generating…')
    try { await downloadQuotePdf(qid, f.subject || f.quote_number || qid) }
    catch (e) { alert('PDF error: ' + (e as Error).message) }
    setPdfState('')
  }

  // ── import from an enquiry file ──
  const importEnquiry = async (file: File) => {
    setImporting('enquiry')
    try {
      const data = (await readDataUrl(file)).split(',')[1]
      const d = await send<Record<string, unknown> & { line_items?: Partial<Row>[]; count?: number }>('POST', '/api/quotations/parse-enquiry', { filename: file.name, data })
      const map: [string, string][] = [['client', 'client'], ['client_name', 'client_name'], ['reference_po', 'reference_po'], ['subject', 'subject'],
        ['delivery_dest', 'delivery_dest'], ['bill_to_company', 'recipient_company'], ['department', 'recipient_dept'], ['currency', 'currency'],
        ['quote_date', 'quote_date'], ['recipient_name', 'recipient_name'], ['recipient_dept', 'recipient_dept'], ['recipient_email', 'recipient_email'],
        ['recipient_tel', 'recipient_tel'], ['manufacturer', 'manufacturer'], ['lead_time', 'lead_time'], ['validity_days', 'validity_days'], ['prepared_by', 'prepared_by']]
      setF(x => { const y = { ...x }; for (const [from, to] of map) if (d[from]) y[to] = String(d[from]); return y })
      if (Array.isArray(d.line_items) && d.line_items.length) {
        setRows(d.line_items.map(it => newRow({
          product_no: it.product_no || '', description: it.description || '', uom: it.uom || 'EA', quantity: Number(it.quantity) || 1,
          unit_price: parseFloat(String(it.unit_price)) || 0, tax_rate: parseFloat(String(it.tax_rate)) || 0, line_total: parseFloat(String(it.line_total)) || 0,
        })))
      }
      let label = `${d.count || 0} line item${d.count === 1 ? '' : 's'} imported`
      if (d.original_quote_number) label += '  ·  ' + d.original_quote_number
      else if (d.reference_po) label += '  ·  ' + d.reference_po
      if (d.delivery_dest) label += '  ·  ' + d.delivery_dest
      setBanner({ ok: true, msg: label })
    } catch (e) { alert('Import failed: ' + (e as Error).message) }
    finally { setImporting(''); if (enquiryInput.current) enquiryInput.current.value = '' }
  }

  // ── paste a table (PasteItems) ──
  // Empty lines - the blank starter row - are simply replaced. Filled ones
  // are kept unless the person chose Replace.
  const addPasted = (items: Item[], replace: boolean) => {
    const { apply, spill } = placePasted(items)
    const fresh = items.map(it => {
      const r = newRow({ product_no: it.product_no, uom: it.uom, quantity: it.quantity, unit_price: it.unit_price, ...apply(it, { description: it.description, extra: {} }) })
      return { ...r, line_total: lineTotal(r) }
    })
    setRows(rs => (replace ? fresh : [...rs.filter(filled), ...fresh]))
    setPasting(false)
    setBanner({ ok: true, msg: `${fresh.length} line item${fresh.length === 1 ? '' : 's'} ${replace ? 'replaced the old lines' : 'added from the paste'}.${spillNote(spill)} Check them before saving.` })
  }
  const spillNote = (n: number) => (n ? ` Only ${MAX_ADDED} added columns fit, so ${n} went into the description instead.` : '')

  // Fill: the pasted columns go into the lines already there, first pasted
  // row into the first line, and so on. Only matched columns with something
  // in them change; a column set as "Add to description" goes under the text.
  const fillPasted = (items: Item[], rowCols: Field[][]) => {
    const { apply, spill } = placePasted(items)
    let i = 0
    setRows(rs => rs.map(r => {
      if (!filled(r) || i >= items.length) return r
      const cols = rowCols[i]
      const it = items[i++]
      const { _k, ...rest } = r
      const description = cols.includes('description') ? it.description
        : it.description ? [r.description, it.description].filter(Boolean).join('<br>') : r.description
      const next = newRow({
        ...rest,  // a new key, so the description box shows the new text
        ...(cols.includes('product_no') && hasProductNo ? { product_no: it.product_no } : {}),
        ...(cols.includes('uom') ? { uom: it.uom } : {}),
        ...(cols.includes('quantity') ? { quantity: it.quantity } : {}),
        ...(cols.includes('unit_price') ? { unit_price: it.unit_price } : {}),
        ...apply(it, { description, extra: r.extra }),
      })
      return { ...next, line_total: lineTotal(next) }
    }))
    const used = Math.min(items.length, rows.filter(filled).length)
    const left = items.length - used
    setPasting(false)
    setBanner({ ok: true, msg: `${used} line${used === 1 ? '' : 's'} filled from the paste${left ? ` - ${left} pasted row${left === 1 ? ' was' : 's were'} left over, as there were more than lines` : ''}.${spillNote(spill)} Check them before saving.` })
  }

  // ── export the lines to Excel ──
  const exportXlsx = async () => {
    if (!rows.length) { alert('No line items to export.'); return }
    const ref = (f.reference_po || 'quote').replace(/[^a-z0-9_-]/gi, '_')
    if (qid) {
      // A saved quote gets the server's real .xlsx, which survives an Excel save and re-open.
      try { saveBlob(await getBlob(`/api/quotations/${qid}/export_xlsx`), `line_items_${ref}.xlsx`) }
      catch (e) { setBanner({ ok: false, msg: 'Export failed: ' + (e as Error).message }) }
      return
    }
    const headers = isNlng ? ['#', 'Product No.', 'Description', 'UOM', 'Qty', `Unit Price (${s})`, 'Tax %', `Total (${s})`]
      : ['#', 'Description', 'UOM', 'Qty', `Unit Price (${s})`, 'Tax %', `Total (${s})`]
    const e2 = (v: unknown) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    const body = rows.map((r, i) => {
      const base: unknown[] = [i + 1, r.description || '', r.uom || '', r.quantity || 0, r.unit_price || 0, r.tax_rate || 0, r.line_total || 0]
      if (isNlng) base.splice(1, 0, r.product_no || '')
      return '<tr>' + base.map(c => `<td>${e2(c)}</td>`).join('') + '</tr>'
    }).join('')
    const html = '<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns="http://www.w3.org/TR/REC-html40"><head><meta charset="UTF-8"/></head><body><table>'
      + '<tr>' + headers.map(h => `<th>${e2(h)}</th>`).join('') + '</tr>' + body + '</table></body></html>'
    saveBlob(new Blob([html], { type: 'application/vnd.ms-excel;charset=utf-8' }), `line_items_${ref}.xls`)
  }

  // ── import prices back from that file ──
  const importPrices = async (file: File) => {
    if (!qid) { setBanner({ ok: false, msg: 'Save the quote first before importing prices.' }); if (priceInput.current) priceInput.current.value = ''; return }
    setImporting('prices')
    try {
      const d = await send<{ line_items?: Partial<Row>[] }>('POST', `/api/quotations/${qid}/import_prices`, { file_data: await readDataUrl(file) })
      // Never let a successful-looking reply empty the form: what is on screen is still the good copy.
      if (!Array.isArray(d.line_items) || !d.line_items.length) {
        setBanner({ ok: false, msg: 'No prices were imported — your line items are unchanged. Check the file matches this quote.' })
      } else {
        const next = d.line_items.map(it => newRow({
          product_no: it.product_no || '', description: it.description || '', uom: it.uom || 'EA', quantity: Number(it.quantity) || 0,
          unit_price: Number(it.unit_price) || 0, tax_rate: Number(it.tax_rate) || 0, line_total: Number(it.line_total) || 0,
          extra: readExtra(it.extra),
        }))
        setRows(next)
        setBanner({ ok: true, msg: `${next.length} line item${next.length === 1 ? '' : 's'} updated with imported prices.` })
      }
    } catch (e) { setBanner({ ok: false, msg: 'Import failed — your line items are unchanged. ' + (e as Error).message }) }
    finally { setImporting(''); if (priceInput.current) priceInput.current.value = '' }
  }

  if (loading) return <div className="page quotes"><p className="muted">Loading quote…</p></div>
  if (loadErr) return <div className="page quotes"><p className="err-text">{loadErr}</p><Link to="/quotes" className="link">← Back to quotations</Link></div>

  const saveLabel = saving === 'saving' ? 'Saving…' : saving === 'saved' ? 'Saved ✓' : 'Save draft'
  const inp = (k: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <input className="qf-inp" value={f[k] ?? ''} onChange={e => set(k, e.target.value)} {...props} />
  )
  const field = (label: React.ReactNode, control: React.ReactNode, key?: string) => (
    <label className="qf-f" key={key}><span className="qf-lbl">{label}</span>{control}</label>
  )
  // One line's cell in one column.
  const cell = (r: Row, c: Col) => {
    switch (c.key) {
      case 'description':
        return <Rich key={`${formKey}-${r._k}`} html={r.description} placeholder="Item description…" onChange={h => setRows(rs => rs.map(x => (x._k === r._k ? { ...x, description: h } : x)))} />
      case 'product_no':
        return <input className="qf-cell" value={r.product_no} placeholder={labelOf(c)} onChange={e => updateRow(r._k, { product_no: e.target.value })} />
      case 'uom':
        return <select className="qf-cell" value={r.uom || 'EA'} onChange={e => updateRow(r._k, { uom: e.target.value })}>{UOMS.map(u => <option key={u}>{u}</option>)}</select>
      case 'quantity':
        return <input className="qf-cell r" type="number" min={0} step="0.01" value={r.quantity} onChange={e => updateRow(r._k, { quantity: parseFloat(e.target.value) || 0 })} />
      case 'unit_price':
        return <input className="qf-cell r" type="number" min={0} step="0.01" value={r.unit_price || ''} placeholder="0.00" onChange={e => updateRow(r._k, { unit_price: parseFloat(e.target.value) || 0 })} />
      case 'line_total':
        return money(r.line_total)
      default:
        if (c.kind === 'total') return money(total2(r))
        if (c.kind === 'price') {
          return <input className="qf-cell r" type="number" min={0} step="0.01" value={r.extra[c.key] || ''} placeholder="0.00" aria-label={labelOf(c)}
                        onChange={e => setRows(rs => rs.map(x => (x._k === r._k ? { ...x, extra: { ...x.extra, [c.key]: e.target.value } } : x)))} />
        }
        return <input className="qf-cell" value={r.extra[c.key] || ''} maxLength={200} aria-label={labelOf(c)}
                      onChange={e => setRows(rs => rs.map(x => (x._k === r._k ? { ...x, extra: { ...x.extra, [c.key]: e.target.value } } : x)))} />
    }
  }

  return (
    <div className="page quotes qf">
      <div className="ph">
        <div className="qf-title">
          <Link className="btn ghost sm" to="/quotes">← Back</Link>
          <div><h1>{qid ? 'Edit quote' : 'New quote'}</h1><p>{COMPANY_NAME[(f.company as Company) || 'spm'] || COMPANY_NAME.spm}</p></div>
        </div>
        <div className="ph-side">
          <button type="button" className="btn" onClick={save} disabled={saving === 'saving'}>{saveLabel}</button>
          {qid && <button type="button" className="btn ghost" onClick={pdf} disabled={!!pdfState}>{pdfState || '⬇ Download PDF'}</button>}
        </div>
      </div>

      <div className="card qf-card">
        <div className="qf-import">
          <input ref={enquiryInput} type="file" accept=".xlsx,.pdf" hidden onChange={e => { const x = e.target.files?.[0]; if (x) importEnquiry(x) }} />
          <button type="button" className="btn ghost sm" onClick={() => enquiryInput.current?.click()} disabled={!!importing}>
            {importing === 'enquiry' ? 'Importing…' : '↑ Import from enquiry file'}
          </button>
          <span className="muted sm">Chevron (.xlsx) or NLNG RFx (.pdf)</span>
        </div>
        {banner && (
          <div className={`qf-banner ${banner.ok ? 'ok' : 'bad'}`} role="status">
            <span>{banner.msg}</span>
            <button type="button" onClick={() => setBanner(null)} aria-label="Dismiss">×</button>
          </div>
        )}

        <h3 className="qf-sec">Quote details</h3>
        <div className="qf-grid">
          {field(<>Client <em>*</em></>, (
            <select className="qf-inp" value={f.client} onChange={e => pickClient(e.target.value)}>
              <option value="">Select client…</option>
              {CLIENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          ))}
          {field('Quote #', <input className="qf-inp mono" value={f.quote_number} readOnly placeholder="Auto-generated on save" />)}
          {f.client === 'others' && field('Client company name', (
            <input className="qf-inp" value={f.client_name} placeholder="e.g. Sahara Group Limited"
                   onChange={e => setF(x => ({ ...x, client_name: e.target.value, recipient_company: e.target.value }))} />
          ))}
          {field('Reference REQ #', inp('reference_po', { placeholder: 'e.g. 0061XXXXXXXX' }))}
          {field('Quote date', inp('quote_date', { type: 'date' }))}
          {field('Offer validity (days)', inp('validity_days', { type: 'number', min: 1 }))}
          {field('Prepared by', inp('prepared_by', { placeholder: 'SPM contact name' }))}
        </div>

        <h3 className="qf-sec">Bill to (recipient)</h3>
        <div className="qf-grid">
          {field('Recipient name', inp('recipient_name', { placeholder: 'e.g. Abiodun Sina' }))}
          {field('Department', inp('recipient_dept', { placeholder: 'Procurement Dept.' }))}
          {field('Company', inp('recipient_company', { placeholder: 'Auto-fills from client' }))}
          {field('Email', inp('recipient_email', { type: 'email' }))}
          {field('Tel', inp('recipient_tel'))}
          {field('Subject', inp('subject', { placeholder: 'Quotation for…' }))}
        </div>

        <h3 className="qf-sec">Terms &amp; conditions <span className="hint">The eye decides whether a line appears on the PDF</span></h3>
        <div className="qf-grid">
          {TC.map(([k, label]) => field(
            <><Eye on={tcShow[k]} onToggle={() => setTcShow(x => ({ ...x, [k]: !x[k] }))} what={label} />{label}</>,
            k === 'shipping_mode' ? (
              <select className="qf-inp" value={f.shipping_mode} onChange={e => set('shipping_mode', e.target.value)}>{SHIPPING.map(o => <option key={o}>{o}</option>)}</select>
            ) : k === 'currency' ? (
              <select className="qf-inp" value={f.currency} onChange={e => set('currency', e.target.value)}>
                <option value="USD">USD – US Dollar ($)</option><option value="GBP">GBP – British Pound (£)</option><option value="NGN">NGN – Nigerian Naira (₦)</option>
              </select>
            ) : inp(k, { placeholder: ({ lead_time: '14 WEEKS (DDP Basis)', delivery_dest: 'Your Chevron Warri Warehouse', country_import: 'United States / United Kingdom', manufacturer: 'e.g. Flexitallic', weight: 'e.g. 1,250 KGS' } as Record<string, string>)[k] }),
            k,
          ))}
        </div>

        <div className="qf-sec row">
          <h3>Line items <span className="count">{rows.length}</span>
            {/* In the heading row, so it never pushes the table down when it appears. */}
            {cols && (
              <span className="qf-cols-bar">
                Own columns
                {removedBuiltIns.map(k => (
                  <button key={k} type="button" className="link" onClick={() => restoreCol(k)}>Put back {DEFAULT_LABEL[k as 'uom' | 'product_no']}</button>
                ))}
                <button type="button" className="link" onClick={() => setCols(null)}
                        title="Standard columns and names; values typed into added columns are dropped on save">Back to standard</button>
              </span>
            )}
          </h3>
          <div className="qf-li-acts">
            <button type="button" className="btn ghost sm" onClick={exportXlsx} title="Export line items to Excel">↓ Export .xlsx</button>
            <input ref={priceInput} type="file" accept=".xls,.xlsx" hidden onChange={e => { const x = e.target.files?.[0]; if (x) importPrices(x) }} />
            <button type="button" className="btn ghost sm" onClick={() => priceInput.current?.click()} disabled={!!importing} title="Import prices from the exported file">
              {importing === 'prices' ? 'Importing…' : '↑ Import prices'}
            </button>
            <button type="button" className="btn ghost sm" onClick={() => setPasting(true)} title="Paste a table copied from an email, Word, Excel or a PDF">⧉ Paste items</button>
            <button type="button" className="btn ghost sm" onClick={addColAtEnd} disabled={addedCount >= MAX_ADDED}
                    title={addedCount >= MAX_ADDED ? `Up to ${MAX_ADDED} added columns fit on the PDF` : 'Add a column at the end of the table'}>+ Add column</button>
            <button type="button" className="btn sm" onClick={() => setRows(rs => [...rs, newRow()])}>+ Add row</button>
          </div>
        </div>
        {pasting && <PasteItems lineCount={rows.filter(filled).length} showsProductNo={hasProductNo} onClose={() => setPasting(false)} onAdd={addPasted} onFill={fillPasted} />}
        <div className="qf-items-wrap">
          <table className={`qf-items${hasProductNo ? ' nlng' : ''}`} style={{ minWidth: 760 + addedCount * 120 }}>
            <colgroup>
              <col style={{ width: '3%' }} />
              {layout.map(c => {
                const used = 3 + 14 + 3 + layout.reduce((a, x) => a + (x.key === 'description' ? 0 : COL_W[x.key] ?? ADDED_W), 0)
                const w = c.key === 'description' ? Math.max(16, 100 - used) : COL_W[c.key] ?? ADDED_W
                return [
                  c.key === 'line_total' && <col key="tax" style={{ width: '14%' }} />,
                  <col key={c.key} style={{ width: `${w}%` }} />,
                ]
              })}
              <col style={{ width: '3%' }} />
            </colgroup>
            <thead>
              <tr>
                <th>#</th>
                {layout.map((c, ci) => [
                  c.key === 'line_total' && <th key="tax">Tax</th>,
                  <th key={c.key} className={c.key === 'uom' ? 'c' : ['quantity', 'unit_price', 'line_total'].includes(c.key) || c.kind ? 'r' : ''}>
                    <ColumnHead key={c.key === renameKey ? 'rn-' + c.key : c.key} col={c} index={ci} count={layout.length} added={addedCount}
                                canPrice={canPrice} startRenaming={c.key === renameKey} onRenamed={() => setRenameKey('')}
                                suffix={c.key === 'unit_price' || c.key === 'line_total' || c.kind ? ` (${s})` : ''}
                                onRename={l => renameCol(c.key, l)} onAction={a => colAction(c.key, a)} />
                  </th>,
                ])}
                <th aria-label="Remove" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r._k}>
                  <td className="c mono no">{i + 1}</td>
                  {layout.map(c => [
                    c.key === 'line_total' && (
                      <td key="tax">
                        <select className="qf-cell" value={String(Number(r.tax_rate) === 0.075 ? 0.075 : 0)} onChange={e => updateRow(r._k, { tax_rate: parseFloat(e.target.value) })}>
                          <option value="0">No Tax</option><option value="0.075">VAT (7.5%)</option>
                        </select>
                      </td>
                    ),
                    <td key={c.key} className={c.key === 'line_total' || c.kind === 'total' ? 'r mono tot' : undefined}>{cell(r, c)}</td>,
                  ])}
                  <td className="c"><button type="button" className="row-del" onClick={() => setRows(rs => rs.filter(x => x._k !== r._k))} title="Remove" aria-label={`Remove line ${i + 1}`}>×</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && <p className="card-empty">No line items — press Add row or import an enquiry file.</p>}
        </div>

        <div className="qf-bottom">
          <div className="qf-notes">
            <span className="qf-lbl">Customer notes</span>
            <Rich key={`notes-${formKey}`} html={notesInit} placeholder="Looking forward to your business." className="notes" onChange={h => { notes.current = h }} />
          </div>
          <div className="qf-totals">
            <div><span>Sub total</span><b className="mono">{money(t.sub)}</b></div>
            <div><label>Discount <input className="qf-inline" type="number" min={0} max={100} step="0.1" value={f.discount_pct} onChange={e => set('discount_pct', e.target.value)} /> %</label><b className="mono">— {money(t.da)}</b></div>
            <div><label><Eye on={markupShow} onToggle={() => setMarkupShow(v => !v)} what="Markup" />Markup <input className="qf-inline" type="number" min={0} step="0.1" value={f.markup_pct} onChange={e => set('markup_pct', e.target.value)} /> %</label><b className="mono">+ {money(t.ma)}</b></div>
            <div><label><Eye on={vatShow} onToggle={() => setVatShow(v => !v)} what="VAT" />VAT <input className="qf-inline" type="number" min={0} max={100} step="0.1" value={f.vat_pct} onChange={e => set('vat_pct', e.target.value)} /> %</label><b className="mono">+ {money(t.va)}</b></div>
            <div><label>Shipping charges <input className="qf-inline wide" type="number" min={0} step="0.01" value={f.shipping_charges} onChange={e => set('shipping_charges', e.target.value)} /></label><b className="mono">{money(t.ship)}</b></div>
            <div className="grand"><span>{grandLabel} ({s})</span><b className="mono">{money(t.total)}</b></div>
            {pair && (
              <div className="grand second" title="Same discount, markup and VAT as above; no shipping charge, as this price already includes delivery">
                <span>{labelOf(pair.total).toUpperCase()} ({s})</span><b className="mono">{money(t.total2)}</b>
              </div>
            )}
          </div>
        </div>

        <div className="qf-actions">
          <button type="button" className="btn" onClick={save} disabled={saving === 'saving'}>{saveLabel}</button>
          {qid && <button type="button" className="btn ghost" onClick={pdf} disabled={!!pdfState}>{pdfState || '⬇ Download PDF'}</button>}
          <span className="sp" />
          <Link className="btn ghost" to="/quotes">Cancel</Link>
        </div>
      </div>
      <FormatBar />
      {dropCol && (
        <ConfirmBox title={dropCol.kind ? 'Remove the second price?' : `Remove the ${labelOf(dropCol)} column?`} confirmLabel="Remove"
                    onCancel={() => setDropCol(null)} onConfirm={() => { removeCol(dropCol.key); setDropCol(null) }}>
          {dropCol.kind ? 'Both its columns and the prices typed into them go, once the quote is saved.'
            : "What's typed in it on each line goes too, once the quote is saved."}
        </ConfirmBox>
      )}
    </div>
  )
}
