import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getJson, send } from '../../lib/api'
import { todayLocal } from '../../lib/orders'
import { clean } from '../quotes/rich'
import { FormatBar, Rich } from '../quotes/RichEditor'
import { type SpmPo, PO_CURRENCY, downloadPoPdf, fmtPo } from './PurchaseOrdersPage'
import '../quotes/quotes.css'

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

/*
  One purchase order, new or saved: a port of the form half of the
  purchase-orders module in web/script.js.

  Every field is typed - the material comes from a different supplier almost
  every time. The one piece of automation is the PO number,
      S.P.M.-C.N.L.-3101-0061451715-FLEXITALLIC
  company · client code · running reference · every client PO on the lines ·
  supplier. The reference is issued by the server at save time and never
  reissued, so until then the preview shows ####.

  As before: problems are said on the page beside the box that is wrong, a
  failed save never clears the form, and a PDF is only made after a save.
*/

type Row = { _k: number; client_po_no: string; quote_ref: string; description: string; quantity: number; unit_price: number; line_total: number }

const CLIENTS: [string, string][] = [['chevron', 'Chevron Nigeria Limited'], ['nlng', 'NLNG'], ['seplat', 'SEPLAT'], ['exxon', 'ExxonMobil'], ['total', 'TotalEnergies'], ['gca', 'GCA Energy'], ['others', 'Others']]
const CLIENT_CODES: Record<string, string> = { chevron: 'C.N.L.', nlng: 'NLNG', seplat: 'SEP', exxon: 'MPN', total: 'TEN', gca: 'GCA', others: 'OTH' }
// Vendors SPM buys from regularly; the picker adds every vendor on a saved order.
const VENDOR_SEEDS = [
  { company: 'Flexitallic', address: '', email: '', name: '', role: 'Sales' },
  { company: 'MetalsDepot', address: '4200 Revilo Rd, Winchester, KY 40391 USA', email: '', name: '', role: 'Sales' },
  { company: 'Rollstud Limited', address: '', email: '', name: '', role: 'Sales' },
]
const CO_SUFFIX = /\b(LIMITED|LTD|INCORPORATED|INC|LLC|PLC|COMPANY|CO|CORP|CORPORATION|GMBH|BV|SA|NIG)\b\.?/gi

const supplierSlug = (c: string) => String(c || '').replace(CO_SUFFIX, ' ').replace(/[^A-Za-z0-9]/g, '').toUpperCase()
/** Chevron POs are ten digits; an 8- or 9-digit one typed short gets its zeros back. */
function pad(v: string): string {
  const t = String(v ?? '').trim()
  if (!/^\d+$/.test(t) || t.length < 8 || t.length > 9) return t
  return t.padStart(10, '0')
}
const initials = (name: string) => {
  const w = String(name || '').replace(/[^A-Za-z\s]/g, '').split(/\s+/).filter(Boolean)
  return w.length ? w.map(x => x[0].toUpperCase()).join('') : 'OTH'
}

let keySeq = 1
const newRow = (r: Partial<Row> = {}): Row => ({ _k: keySeq++, client_po_no: '', quote_ref: '', description: '', quantity: 1, unit_price: 0, line_total: 0, ...r })

type Fields = Record<string, string>
const blank = (): Fields => ({
  po_date: todayLocal(), po_number: '', client: '', client_name: '', manager: 'FELIX AKARUE',
  sales_person: 'IJALA STYLES ONOME', vendor_name: '', vendor_role: 'Sales', vendor_company: '', vendor_address: '',
  vendor_email: '', shipping_terms: '', shipping_method: '', delivery_date: '', deliver_to: '', currency: 'USD', notes: '',
  discount_pct: '0', markup_pct: '0',
})

const n = (v: string) => parseFloat(v) || 0

export default function PoForm() {
  const { id: routeId } = useParams()
  const nav = useNavigate()
  const isNew = !routeId || routeId === 'new'
  const [poId, setPoId] = useState<string | null>(isNew ? null : routeId!)
  const [f, setF] = useState<Fields>(blank)
  const [rows, setRows] = useState<Row[]>(() => (isNew ? [newRow()] : []))
  const [ref, setRef] = useState<number | null>(null)
  const [vendorPick, setVendorPick] = useState('')
  const [saved, setSaved] = useState<SpmPo[]>([])
  const [discountShow, setDiscountShow] = useState(true)
  const [markupShow, setMarkupShow] = useState(true)
  const [poNumberLocked, setPoNumberLocked] = useState(true)
  const [loading, setLoading] = useState(!isNew)
  const [notice, setNotice] = useState<{ ok: boolean; msg: string } | null>(null)
  const [invalid, setInvalid] = useState<string | null>(null)
  const [saving, setSaving] = useState<'idle' | 'saving' | 'saved'>('idle')
  const [pdfState, setPdfState] = useState('')
  const [formKey, setFormKey] = useState(0)
  // A saved order shows its stored number until something the number is built from changes.
  const [touched, setTouched] = useState(false)
  const fieldRefs = useRef<Record<string, HTMLElement | null>>({})
  const noticeRef = useRef<HTMLDivElement>(null)

  const NUMBER_PARTS = new Set(['client', 'client_name', 'vendor_company'])
  const set = (k: string, v: string) => {
    setF(x => ({ ...x, [k]: v }))
    if (invalid === k) setInvalid(null)
    if (NUMBER_PARTS.has(k)) setTouched(true)
  }
  const sym = PO_CURRENCY[f.currency] || '$'

  // Saved orders feed the vendor picker, whether or not one is being edited.
  useEffect(() => { getJson<SpmPo[]>('/api/spm_pos').then(setSaved, () => {}) }, [])

  useEffect(() => {
    if (isNew) return
    getJson<Record<string, unknown> & { line_items?: Partial<Row>[] }>(`/api/spm_pos/${routeId}`).then(p => {
      const s = (k: string) => (p[k] == null ? '' : String(p[k]))
      const savedNumber = s('po_number')
      setF({
        po_date: s('po_date').slice(0, 10), po_number: savedNumber, client: s('client'), client_name: s('client_name'),
        manager: s('manager'), sales_person: s('sales_person'), vendor_name: s('vendor_name'), vendor_role: s('vendor_role'),
        vendor_company: s('vendor_company'), vendor_address: s('vendor_address'), vendor_email: s('vendor_email'),
        shipping_terms: s('shipping_terms'), shipping_method: s('shipping_method'), delivery_date: s('delivery_date'),
        deliver_to: s('deliver_to'), currency: (s('currency') || 'USD').toUpperCase(), notes: s('notes'),
        discount_pct: String(p.discount_pct ?? 0), markup_pct: String(p.markup_pct ?? 0),
      })
      setDiscountShow(p.discount_visible !== false)
      setMarkupShow(p.markup_visible !== false)
      // If the saved number differs from what the auto-formula would produce, it
      // was manually overridden before — keep it unlocked so the user can see and
      // edit the override rather than silently discarding it.
      setPoNumberLocked(!!p.po_number_override === false)
      setRef(p.po_seq ? Number(p.po_seq) : null)
      setVendorPick(s('vendor_company').toLowerCase())
      setRows((p.line_items || []).map(it => newRow({
        client_po_no: it.client_po_no || '', quote_ref: it.quote_ref || '', description: it.description || '',
        quantity: Number(it.quantity) || 0, unit_price: Number(it.unit_price) || 0, line_total: Number(it.line_total) || 0,
      })))
      setFormKey(k => k + 1); setLoading(false)
    }, e => { setNotice({ ok: false, msg: 'Could not load the order: ' + (e as Error).message + '. Go back and try again.' }); setLoading(false) })
  }, [routeId, isNew])

  // ── vendors: the seeds plus every saved order's, most recent detail winning ──
  const vendors = useMemo(() => {
    const out: Record<string, typeof VENDOR_SEEDS[number]> = {}
    VENDOR_SEEDS.forEach(v => { out[v.company.toLowerCase()] = v })
    ;[...saved].reverse().forEach(p => {
      if (!p.vendor_company) return
      out[p.vendor_company.toLowerCase()] = { company: p.vendor_company, address: p.vendor_address || '', email: p.vendor_email || '', name: p.vendor_name || '', role: p.vendor_role || '' }
    })
    return out
  }, [saved])

  const pickVendor = (key: string) => {
    setVendorPick(key)
    if (!key) return
    if (key === '__new') {
      // A new supplier: clear the block rather than leave the last vendor's details under a new name.
      setF(x => ({ ...x, vendor_company: '', vendor_name: '', vendor_address: '', vendor_email: '', vendor_role: 'Sales' }))
      setTouched(true)
      setTimeout(() => fieldRefs.current.vendor_company?.focus(), 0)
      return
    }
    const v = vendors[key]
    if (v) setF(x => ({ ...x, vendor_company: v.company || '', vendor_address: v.address || '', vendor_email: v.email || '', vendor_name: v.name || '', vendor_role: v.role || 'Sales' }))
    setTouched(true)
  }

  // ── the PO number preview ──
  const code = f.client === 'others' ? initials(f.client_name) : CLIENT_CODES[f.client] || 'OTH'
  const clientPos = useMemo(() => {
    const seen = new Set<string>(), out: string[] = []
    rows.forEach(r => { const p = pad(r.client_po_no); if (p && !seen.has(p)) { seen.add(p); out.push(p) } })
    return out
  }, [rows])
  const preview = ['S.P.M.', code, ref ? String(ref) : '####', ...clientPos, ...(supplierSlug(f.vendor_company) ? [supplierSlug(f.vendor_company)] : [])].join('-')

  const t = useMemo(() => {
    const sub = rows.reduce((a, r) => a + (Number(r.line_total) || 0), 0)
    const da = sub * n(f.discount_pct) / 100
    const ma = sub * n(f.markup_pct) / 100
    return { sub, da, ma, grand: sub - da + ma }
  }, [rows, f.discount_pct, f.markup_pct])

  const updateRow = (k: number, patch: Partial<Row>) => { if ('client_po_no' in patch) setTouched(true); setRows(rs => rs.map(r => {
    if (r._k !== k) return r
    const next = { ...r, ...patch }
    return 'quantity' in patch || 'unit_price' in patch ? { ...next, line_total: Math.round((Number(next.quantity) || 0) * (Number(next.unit_price) || 0) * 100) / 100 } : next
  })) }
  // A new line inherits the client PO from the one above - most orders sit against one.
  const addRow = () => { setTouched(true); setRows(rs => [...rs, newRow({ client_po_no: rs.length ? rs[rs.length - 1].client_po_no : '' })]) }
  const removeRow = (k: number) => { setTouched(true); setRows(rs => rs.filter(x => x._k !== k)) }

  const say = (ok: boolean, msg: string) => {
    setNotice({ ok, msg })
    setTimeout(() => noticeRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 0)
    if (ok) setTimeout(() => setNotice(n => (n && n.ok && n.msg === msg ? null : n)), 4000)
  }
  const bad = (k: string, msg: string) => { say(false, msg); setInvalid(k); setTimeout(() => fieldRefs.current[k]?.focus(), 0); return false }

  // Everything that must be true before saving, in the order the form is read.
  const validate = () => {
    setInvalid(null)
    if (!f.client) return bad('client', 'Choose the end client.')
    if (f.client === 'others' && !f.client_name.trim()) return bad('client_name', "Type the customer's company name — its initials become the code in the PO number.")
    if (!f.vendor_company.trim()) return bad('vendor_company', 'Enter the supplier company — the PO number ends with it.')
    if (!rows.some(r => r.description.replace(/<[^>]*>/g, '').trim() || Number(r.line_total))) {
      say(false, 'Add at least one line item before saving — an order with no lines has nothing to send.'); return false
    }
    return true
  }

  const collect = useCallback(() => {
    const g = (k: string) => (f[k] || '').trim()
    return {
      po_date: g('po_date'), client: g('client'), client_name: g('client_name'), manager: g('manager'), sales_person: g('sales_person'),
      vendor_name: g('vendor_name'), vendor_role: g('vendor_role'), vendor_company: g('vendor_company'), vendor_address: g('vendor_address'),
      vendor_email: g('vendor_email'), deliver_to: g('deliver_to'), shipping_terms: g('shipping_terms'), shipping_method: g('shipping_method'),
      delivery_date: g('delivery_date'), currency: g('currency') || 'USD', notes: g('notes'),
      discount_pct: n(f.discount_pct), markup_pct: n(f.markup_pct),
      discount_visible: discountShow, markup_visible: markupShow,
      total: Math.round(t.grand * 100) / 100,
      // When the user has unlocked and typed their own number, send it so the
      // server stores it verbatim instead of re-deriving the formula number.
      ...(!poNumberLocked && f.po_number.trim() ? { po_number_override: f.po_number.trim() } : {}),
      line_items: rows.map((r, i) => ({
        item_no: i + 1, client_po_no: r.client_po_no || '', quote_ref: r.quote_ref || '', description: clean(r.description || ''),
        quantity: Number(r.quantity) || 0, unit_price: Number(r.unit_price) || 0, line_total: Number(r.line_total) || 0,
      })),
    }
  }, [f, rows, t.grand, discountShow, markupShow, poNumberLocked])

  /** Save. Throws on failure, and the form is left exactly as it is. */
  const doSave = async (): Promise<{ id: string; number: string }> => {
    const r = await send<{ id?: string; po_number?: string; po_seq?: number }>(poId ? 'PUT' : 'POST', poId ? `/api/spm_pos/${poId}` : '/api/spm_pos', collect())
    let id = poId
    if (!poId && r.id) { id = r.id; setPoId(r.id); nav(`/purchase-orders/${r.id}`, { replace: true }) }
    if (r.po_seq) setRef(r.po_seq)
    // Update the stored number from the server response; if we sent an override
    // the server echoes it back and we keep the form in unlocked mode.
    if (r.po_number) { setF(x => ({ ...x, po_number: r.po_number! })); if (poNumberLocked) setTouched(false) }
    getJson<SpmPo[]>('/api/spm_pos').then(setSaved, () => {})
    return { id: id!, number: r.po_number || preview }
  }

  const save = async () => {
    if (!validate()) return
    setSaving('saving')
    try {
      const { number } = await doSave()
      say(true, `Saved as ${number}.`)
      setSaving('saved'); setTimeout(() => setSaving('idle'), 2000)
    } catch (e) {
      say(false, 'NOT saved — ' + (e as Error).message + ' Everything is still on screen; press Save again.')
      setSaving('idle')
    }
  }

  const pdf = async () => {
    // Built from the SAVED record, so save first; no PDF if that fails.
    if (!validate()) return
    setPdfState('Saving…')
    let id: string
    try { id = (await doSave()).id }
    catch (e) { say(false, 'NOT saved, so no PDF was made — it would have been built from the previous version. ' + (e as Error).message + ' Your changes are still on screen; press Save, then try again.'); setPdfState(''); return }
    setPdfState('Generating…')
    try { await downloadPoPdf(id, f.po_number || id) } catch (e) { say(false, 'PDF failed: ' + (e as Error).message) }
    setPdfState('')
  }

  if (loading) return <div className="page quotes"><p className="muted">Loading the order…</p></div>

  const saveLabel = saving === 'saving' ? 'Saving…' : saving === 'saved' ? 'Saved ✓' : 'Save'
  const reg = (k: string) => (el: HTMLElement | null) => { fieldRefs.current[k] = el }
  const inp = (k: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <input ref={reg(k)} className={`qf-inp${invalid === k ? ' invalid' : ''}`} value={f[k] ?? ''} onChange={e => set(k, e.target.value)} {...props} />
  )
  const field = (label: React.ReactNode, control: React.ReactNode, wide = false) => (
    <label className={`qf-f${wide ? ' wide' : ''}`}><span className="qf-lbl">{label}</span>{control}</label>
  )

  return (
    <div className="page quotes qf">
      <div className="ph">
        <div className="qf-title">
          <Link className="btn ghost sm" to="/purchase-orders">← Back</Link>
          <div><h1>{poId ? 'Edit purchase order' : 'New purchase order'}</h1><p>Special Piping Materials (Nig.) Ltd.</p></div>
        </div>
        <div className="ph-side">
          <button type="button" className="btn" onClick={save} disabled={saving === 'saving'}>{saveLabel}</button>
          {poId && <button type="button" className="btn ghost" onClick={pdf} disabled={!!pdfState}>{pdfState || '⬇ Download PDF'}</button>}
        </div>
      </div>

      <div className="card qf-card">
        {notice && (
          <div ref={noticeRef} className={`qf-banner ${notice.ok ? 'ok' : 'bad'}`} role={notice.ok ? 'status' : 'alert'}>
            <span>{notice.msg}</span><button type="button" onClick={() => setNotice(null)} aria-label="Dismiss">×</button>
          </div>
        )}

        <h3 className="qf-sec">Order</h3>
        <div className="qf-grid">
          {field(<>End client <em>*</em></>, (
            <select ref={reg('client')} className={`qf-inp${invalid === 'client' ? ' invalid' : ''}`} value={f.client}
                    onChange={e => { const c = e.target.value; setF(x => ({ ...x, client: c, client_name: c === 'others' ? x.client_name : '' })); setNotice(null); setInvalid(null); setTouched(true) }}>
              <option value="">Select client…</option>
              {CLIENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          ))}
          {field('Date', inp('po_date', { type: 'date' }))}
          {f.client === 'others' && field('Client company name', inp('client_name', { placeholder: 'e.g. Sahara Group Limited' }))}
          {f.client === 'others' && field('Code', <span className="mono code-hint">{code}</span>)}
          {field('PO number', (
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              {poNumberLocked
                ? <input
                    className="qf-inp mono"
                    style={{ flex: 1 }}
                    value={poId && f.po_number && !touched ? f.po_number : preview}
                    readOnly
                    title="Auto-generated — click the pencil to override"
                  />
                : <input
                    ref={reg('po_number')}
                    className="qf-inp mono"
                    style={{ flex: 1 }}
                    value={f.po_number}
                    onChange={e => set('po_number', e.target.value)}
                    placeholder={preview}
                    title="Type a custom PO number — or click the lock to revert to auto"
                  />
              }
              <button
                type="button"
                className="eye"
                style={{ flexShrink: 0 }}
                title={poNumberLocked ? 'Click to edit PO number manually' : 'Click to revert to auto-generated number'}
                onClick={() => {
                  if (poNumberLocked) {
                    // Unlock: pre-fill with whatever is currently shown
                    setF(x => ({ ...x, po_number: (poId && f.po_number && !touched ? f.po_number : preview) }))
                    setPoNumberLocked(false)
                    setTimeout(() => fieldRefs.current.po_number?.focus(), 0)
                  } else {
                    // Re-lock: go back to auto
                    setPoNumberLocked(true)
                  }
                }}
              >
                {poNumberLocked
                  ? /* pencil */ <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                  : /* lock  */ <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                }
              </button>
            </div>
          ), true)}
          {field('Manager', inp('manager', { placeholder: 'FELIX AKARUE' }))}
          {field('Sales person', inp('sales_person', { placeholder: 'IJALA STYLES ONOME' }))}
        </div>

        <h3 className="qf-sec">Vendor</h3>
        <div className="qf-grid">
          {field('Saved vendor', (
            <select className="qf-inp" value={vendorPick} onChange={e => pickVendor(e.target.value)}>
              <option value="">Select a vendor…</option>
              {Object.keys(vendors).sort().map(k => <option key={k} value={k}>{vendors[k].company}</option>)}
              <option value="__new">+ New supplier…</option>
            </select>
          ))}
          {field(<>Company <em>*</em></>, inp('vendor_company', { placeholder: 'Flexitallic' }))}
          {field('Contact', inp('vendor_name', { placeholder: 'DANIEL' }))}
          {field('Role', inp('vendor_role', { placeholder: 'Sales' }))}
          {field('Email', inp('vendor_email', { placeholder: 'sales@vendor.com' }))}
          {field('Address', inp('vendor_address', { placeholder: '4200 Revilo Rd, Winchester, KY 40391 USA' }))}
        </div>

        <h3 className="qf-sec">Shipping</h3>
        <div className="qf-grid">
          {field('Terms', inp('shipping_terms', { placeholder: 'EXW' }))}
          {field('Method', inp('shipping_method', { placeholder: 'Sea' }))}
          {field('Delivery date', inp('delivery_date', { placeholder: '4 weeks ARO' }))}
          {field('Currency', (
            <select className="qf-inp" value={f.currency} onChange={e => set('currency', e.target.value)}>
              <option value="USD">USD ($)</option><option value="GBP">GBP (£)</option><option value="EUR">EUR (€)</option><option value="NGN">NGN (₦)</option>
            </select>
          ))}
          {field('Deliver to', (
            <textarea className="qf-inp ta" rows={2} value={f.deliver_to} onChange={e => set('deliver_to', e.target.value)}
                      placeholder={'Special Piping Material c/o CARIGO LLC\n8600 Commerce Park Drive, Houston TX 77036'} />
          ), true)}
        </div>

        <div className="qf-sec row">
          <h3>Line items <span className="count">{rows.length}</span></h3>
          <div className="qf-li-acts"><button type="button" className="btn sm" onClick={addRow}>+ Add row</button></div>
        </div>
        <div className="qf-items-wrap">
          <table className="qf-items">
            <colgroup>
              <col style={{ width: '4%' }} /><col style={{ width: '14%' }} /><col style={{ width: '12%' }} /><col style={{ width: '34%' }} />
              <col style={{ width: '8%' }} /><col style={{ width: '13%' }} /><col style={{ width: '12%' }} /><col style={{ width: '3%' }} />
            </colgroup>
            <thead>
              <tr><th>#</th><th>P.O. No.</th><th>Quote ref.</th><th>Description</th><th className="r">Qty</th><th className="r">Unit price ({sym})</th><th className="r">Total ({sym})</th><th aria-label="Remove" /></tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r._k}>
                  <td className="c mono no">{i + 1}</td>
                  <td><input className="qf-cell mono" value={r.client_po_no} placeholder="61484185" onChange={e => updateRow(r._k, { client_po_no: e.target.value })} /></td>
                  <td><input className="qf-cell mono" value={r.quote_ref} placeholder="1041744" onChange={e => updateRow(r._k, { quote_ref: e.target.value })} /></td>
                  <td><Rich key={`${formKey}-${r._k}`} html={r.description} placeholder="Size, grade, spec…" onChange={h => setRows(rs => rs.map(x => (x._k === r._k ? { ...x, description: h } : x)))} /></td>
                  <td><input className="qf-cell r" type="number" min={0} step="0.01" value={r.quantity || ''} onChange={e => updateRow(r._k, { quantity: parseFloat(e.target.value) || 0 })} /></td>
                  <td><input className="qf-cell r" type="number" min={0} step="0.01" value={r.unit_price || ''} placeholder="0.00" onChange={e => updateRow(r._k, { unit_price: parseFloat(e.target.value) || 0 })} /></td>
                  <td className="r mono tot">{fmtPo(r.line_total, sym)}</td>
                  <td className="c"><button type="button" className="row-del" onClick={() => removeRow(r._k)} title="Remove" aria-label={`Remove line ${i + 1}`}>×</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && <p className="card-empty">No line items — press Add row.</p>}
        </div>

        <div className="qf-bottom">
          <div className="qf-notes">
            <span className="qf-lbl">Notes to the vendor</span>
            <textarea className="qf-inp ta notes" rows={4} value={f.notes} onChange={e => set('notes', e.target.value)} placeholder="Please quote our P.O. number on all documents and packing lists." />
          </div>
          <div className="qf-totals">
            <div><span>Line items</span><b className="mono">{rows.length}</b></div>
            <div><span>Currency</span><b className="mono">{f.currency}</b></div>
            <div><span>Sub-total</span><b className="mono">{fmtPo(t.sub, sym)}</b></div>
            <div>
              <label><Eye on={discountShow} onToggle={() => setDiscountShow(v => !v)} what="Discount" />Discount <input className="qf-inline" type="number" min={0} max={100} step="0.1" value={f.discount_pct} onChange={e => set('discount_pct', e.target.value)} /> %</label>
              <b className="mono">— {fmtPo(t.da, sym)}</b>
            </div>
            <div>
              <label><Eye on={markupShow} onToggle={() => setMarkupShow(v => !v)} what="Markup" />Markup <input className="qf-inline" type="number" min={0} step="0.1" value={f.markup_pct} onChange={e => set('markup_pct', e.target.value)} /> %</label>
              <b className="mono">+ {fmtPo(t.ma, sym)}</b>
            </div>
            <div className="grand"><span>TOTAL ({sym})</span><b className="mono">{fmtPo(t.grand, sym)}</b></div>
          </div>
        </div>

        <div className="qf-actions">
          <button type="button" className="btn" onClick={save} disabled={saving === 'saving'}>{saveLabel}</button>
          {poId && <button type="button" className="btn ghost" onClick={pdf} disabled={!!pdfState}>{pdfState || '⬇ Download PDF'}</button>}
          <span className="sp" />
          <Link className="btn ghost" to="/purchase-orders">Cancel</Link>
        </div>
      </div>
      <FormatBar />
    </div>
  )
}
