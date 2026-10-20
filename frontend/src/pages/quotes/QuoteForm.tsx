import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getBlob, getJson, readDataUrl, saveBlob, send } from '../../lib/api'
import { currentUser } from '../../lib/session'
import { todayLocal } from '../../lib/orders'
import { clean, notesToHtml } from './rich'
import { FormatBar, Rich } from './RichEditor'
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

type Row = { _k: number; product_no: string; description: string; uom: string; quantity: number; unit_price: number; tax_rate: number; line_total: number }

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
const newRow = (r: Partial<Row> = {}): Row => ({ _k: keySeq++, product_no: '', description: '', uom: 'EA', quantity: 1, unit_price: 0, tax_rate: 0, line_total: 0, ...r })
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
  const enquiryInput = useRef<HTMLInputElement>(null)
  const priceInput = useRef<HTMLInputElement>(null)
  const status = useRef('draft')

  const set = (k: string, v: string) => setF(x => ({ ...x, [k]: v }))
  const isNlng = f.client === 'nlng'
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
      })))
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
    return { sub, da, ma, va, ship, total: sub - da + ma + va + ship }
  }, [rows, f.discount_pct, f.markup_pct, f.vat_pct, f.shipping_charges])

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
      })),
      ...Object.fromEntries(TC.map(([k]) => ['show_' + k, tcShow[k]])),
    }
  }, [rows, f, markupShow, vatShow, tcShow])

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
          <h3>Line items <span className="count">{rows.length}</span></h3>
          <div className="qf-li-acts">
            <button type="button" className="btn ghost sm" onClick={exportXlsx} title="Export line items to Excel">↓ Export .xlsx</button>
            <input ref={priceInput} type="file" accept=".xls,.xlsx" hidden onChange={e => { const x = e.target.files?.[0]; if (x) importPrices(x) }} />
            <button type="button" className="btn ghost sm" onClick={() => priceInput.current?.click()} disabled={!!importing} title="Import prices from the exported file">
              {importing === 'prices' ? 'Importing…' : '↑ Import prices'}
            </button>
            <button type="button" className="btn sm" onClick={() => setRows(rs => [...rs, newRow()])}>+ Add row</button>
          </div>
        </div>
        <div className="qf-items-wrap">
          <table className={`qf-items${isNlng ? ' nlng' : ''}`}>
            <colgroup>
              <col style={{ width: '3%' }} /><col style={{ width: isNlng ? '22%' : '37%' }} />{isNlng && <col style={{ width: '15%' }} />}
              <col style={{ width: '7%' }} /><col style={{ width: '8%' }} /><col style={{ width: '15%' }} /><col style={{ width: '14%' }} /><col style={{ width: '13%' }} /><col style={{ width: '3%' }} />
            </colgroup>
            <thead>
              <tr>
                <th>#</th><th>Item description</th>{isNlng && <th>Product No.</th>}<th className="c">U.O.M</th><th className="r">QTY</th>
                <th className="r">Unit price ({s})</th><th>Tax</th><th className="r">Total ({s})</th><th aria-label="Remove" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r._k}>
                  <td className="c mono no">{i + 1}</td>
                  <td><Rich key={`${formKey}-${r._k}`} html={r.description} placeholder="Item description…" onChange={h => setRows(rs => rs.map(x => (x._k === r._k ? { ...x, description: h } : x)))} /></td>
                  {isNlng && <td><input className="qf-cell" value={r.product_no} placeholder="Product No." onChange={e => updateRow(r._k, { product_no: e.target.value })} /></td>}
                  <td><select className="qf-cell" value={r.uom || 'EA'} onChange={e => updateRow(r._k, { uom: e.target.value })}>{UOMS.map(u => <option key={u}>{u}</option>)}</select></td>
                  <td><input className="qf-cell r" type="number" min={0} step="0.01" value={r.quantity} onChange={e => updateRow(r._k, { quantity: parseFloat(e.target.value) || 0 })} /></td>
                  <td><input className="qf-cell r" type="number" min={0} step="0.01" value={r.unit_price || ''} placeholder="0.00" onChange={e => updateRow(r._k, { unit_price: parseFloat(e.target.value) || 0 })} /></td>
                  <td>
                    <select className="qf-cell" value={String(Number(r.tax_rate) === 0.075 ? 0.075 : 0)} onChange={e => updateRow(r._k, { tax_rate: parseFloat(e.target.value) })}>
                      <option value="0">No Tax</option><option value="0.075">VAT (7.5%)</option>
                    </select>
                  </td>
                  <td className="r mono tot">{money(r.line_total)}</td>
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
            <div className="grand"><span>TOTAL ({s})</span><b className="mono">{money(t.total)}</b></div>
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
    </div>
  )
}
