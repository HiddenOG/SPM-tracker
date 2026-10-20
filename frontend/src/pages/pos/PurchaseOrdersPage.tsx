import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { getBlob, getJson, saveBlob, send } from '../../lib/api'
import { fmtDate } from '../../lib/orders'
import Pager from '../../lib/Pager'
import '../quotes/quotes.css'

/*
  Purchase orders Special Piping raises on its own suppliers: the list.
  A port of the list half of the purchase-orders module in web/script.js.
*/

export type SpmPo = {
  id: string; po_number?: string; po_seq?: number; vendor_company?: string; vendor_name?: string; vendor_address?: string
  vendor_email?: string; vendor_role?: string; client?: string; client_po?: string; status?: string
  currency?: string; total?: number | null; po_date?: string
}

const PER_PAGE = 100
export const PO_CURRENCY: Record<string, string> = { USD: '$', GBP: '£', EUR: '€', NGN: '₦' }
export const fmtPo = (v: unknown, sym: string) => sym + (Number(v) || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** Download a saved PO's PDF, named after its number, as before. */
export async function downloadPoPdf(id: string, label: string): Promise<void> {
  const blob = await getBlob(`/api/spm_pos/${id}/pdf`)
  const safe = String(label || id).replace(/[^A-Za-z0-9\s._-]/g, '').trim().replace(/\s+/g, '_')
  saveBlob(blob, (safe || id) + '.pdf')
}

export default function PurchaseOrdersPage() {
  const nav = useNavigate()
  const [pos, setPos] = useState<SpmPo[] | null>(null)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [deleting, setDeleting] = useState<SpmPo | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    try { setPos(await getJson<SpmPo[]>('/api/spm_pos')); setErr('') }
    catch (e) { setErr('Could not load purchase orders — ' + (e as Error).message + ' If this page is new, the spm_pos tables may not exist yet.'); setPos([]) }
  }, [])
  useEffect(() => { load() }, [load])

  const list = useMemo(() => {
    const t = q.trim().toLowerCase()
    return (pos || []).filter(p => !t || [p.po_number, p.vendor_company, p.vendor_name, p.client_po, p.client, p.status]
      .some(v => String(v || '').toLowerCase().includes(t)))
  }, [pos, q])
  const pages = Math.max(1, Math.ceil(list.length / PER_PAGE))
  const pg = Math.min(page, pages)
  const rows = list.slice((pg - 1) * PER_PAGE, pg * PER_PAGE)

  const pdf = async (p: SpmPo) => {
    setBusy(p.id)
    try { await downloadPoPdf(p.id, p.po_number || p.id) }
    catch (e) { setErr('PDF failed: ' + (e as Error).message) }
    finally { setBusy(null) }
  }

  return (
    <div className="page quotes">
      <div className="ph">
        <div>
          <h1>Purchase orders</h1>
          <p>Orders Special Piping raises on its suppliers</p>
        </div>
        <Link className="btn" to="/purchase-orders/new">+ New PO</Link>
      </div>

      <div className="filters">
        <label className="search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input type="search" value={q} onChange={e => { setQ(e.target.value); setPage(1) }} placeholder="Search PO number, vendor, client PO…" aria-label="Search purchase orders" />
        </label>
      </div>

      {err && pos && pos.length > 0 && <div className="qf-banner bad card" role="alert"><span>{err}</span><button type="button" onClick={() => setErr('')} aria-label="Dismiss">×</button></div>}

      <div className="card table-card">
        <div className="tw">
          <table className="ot qt-tbl">
            <thead>
              <tr>
                {['PO #', 'Vendor', 'Client', 'Client PO'].map(h => <th key={h}><button type="button" tabIndex={-1}>{h}</button></th>)}
                <th className="r"><button type="button" tabIndex={-1}>Total</button></th>
                <th><button type="button" tabIndex={-1}>Date</button></th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {pos === null && Array.from({ length: 6 }, (_, i) => (
                <tr key={i} className="skel-row">{Array.from({ length: 7 }, (_, j) => <td key={j}><span className="skel" /></td>)}</tr>
              ))}
              {rows.map(p => (
                <tr key={p.id} onClick={() => nav(`/purchase-orders/${p.id}`)}>
                  <td className="mono qn po-num" title={p.po_number || ''}>{p.po_number || '—'}</td>
                  <td>{p.vendor_company || p.vendor_name || '—'}</td>
                  <td>{(p.client || '').toUpperCase() || '—'}</td>
                  <td className="mono ref" title={p.client_po || ''}>{p.client_po || '—'}</td>
                  <td className="r mono">{p.total != null ? fmtPo(p.total, PO_CURRENCY[(p.currency || 'USD').toUpperCase()] || '$') : '—'}</td>
                  <td>{p.po_date ? fmtDate(p.po_date)?.d : '—'}</td>
                  <td className="r acts" onClick={e => e.stopPropagation()}>
                    <Link className="btn ghost sm" to={`/purchase-orders/${p.id}`}>Edit</Link>
                    <button type="button" className="btn ghost sm" onClick={() => pdf(p)} disabled={busy === p.id}>{busy === p.id ? '…' : 'PDF'}</button>
                    <button type="button" className="btn ghost sm del" onClick={() => setDeleting(p)} title="Delete this purchase order">Del</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {pos !== null && !list.length && (
            <p className={`card-empty${err ? ' err-text' : ''}`}>{err || (q ? 'No purchase orders match your search.' : 'No purchase orders yet — click New PO to raise one.')}</p>
          )}
        </div>
        <Pager page={pg} total={list.length} perPage={PER_PAGE} onPage={setPage} />
      </div>

      {deleting && <DeletePo po={deleting} onClose={() => setDeleting(null)} onDone={() => { setDeleting(null); load() }} />}
    </div>
  )
}

function DeletePo({ po, onClose, onDone }: { po: SpmPo; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  const go = async () => {
    setBusy(true); setErr('')
    try { await send('DELETE', `/api/spm_pos/${po.id}`); onDone() }
    catch (e) { setErr('Delete failed: ' + (e as Error).message); setBusy(false) }
  }
  return (
    <div className="cell-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="cell-box" role="dialog" style={{ maxWidth: 380 }}>
        <div className="cell-hd"><span className="cell-col">Delete purchase order</span><button type="button" className="cell-close" onClick={onClose} aria-label="Close">×</button></div>
        <p className="ds-text">Are you sure you want to delete <strong>{po.po_number || 'this purchase order'}</strong>? This removes the order and its line items, and cannot be undone.</p>
        <div className="cell-btns">
          <button type="button" className="btn danger" onClick={go} disabled={busy}>{busy ? 'Deleting…' : 'Delete'}</button>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        </div>
        {err && <p className="err-text" style={{ marginTop: 8 }}>{err}</p>}
      </div>
    </div>
  )
}
