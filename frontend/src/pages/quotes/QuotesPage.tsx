import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { getBlob, getJson, saveBlob, send } from '../../lib/api'
import { fmtDate } from '../../lib/orders'
import Pager from '../../lib/Pager'
import './quotes.css'

/*
  Quotations: the list. A port of the list half of the quotes module in
  web/script.js. The company switch (Special Piping / Daniel Mag) is
  remembered in the same localStorage key the old page used.
*/

export type Company = 'spm' | 'danmag'
type Quote = {
  id: string; quote_number?: string; company?: string; client?: string; reference_po?: string
  subject?: string; total?: number | null; quote_date?: string; status?: string
}

const PER_PAGE = 100
const STATUS_TONE: Record<string, string> = { draft: 'n', sent: 'ok', accepted: 'ok', rejected: 'r' }
export const COMPANY_NAME: Record<Company, string> = {
  spm: 'Special Piping Materials (Nig.) Ltd.',
  danmag: 'Daniel Mag Obontuaren (Nig) Limited',
}

export function readCompany(): Company {
  try { return localStorage.getItem('activeQuoteCompany') === 'danmag' ? 'danmag' : 'spm' } catch { return 'spm' }
}

/** Download a saved quote's PDF, named after its subject or number, as before. */
export async function downloadQuotePdf(id: string, label: string): Promise<void> {
  const blob = await getBlob(`/api/quotations/${id}/pdf`)
  const safe = (label || id).replace(/[^A-Za-z0-9\s._-]/g, '').trim().replace(/\s+/g, '_')
  saveBlob(blob, (safe || id) + '.pdf')
}

export default function QuotesPage() {
  const nav = useNavigate()
  const [quotes, setQuotes] = useState<Quote[] | null>(null)
  const [err, setErr] = useState('')
  const [company, setCompany] = useState<Company>(readCompany)
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [deleting, setDeleting] = useState<Quote | null>(null)
  const [pdfBusy, setPdfBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    try { setQuotes(await getJson<Quote[]>('/api/quotations')); setErr('') }
    catch (e) { setErr((e as Error).message); setQuotes([]) }
  }, [])
  useEffect(() => { load() }, [load])

  const pick = (c: Company) => {
    setCompany(c); setPage(1)
    try { localStorage.setItem('activeQuoteCompany', c) } catch { /* not remembered */ }
  }

  const list = useMemo(() => {
    const t = q.trim().toLowerCase()
    return (quotes || [])
      .filter(x => (x.company || 'spm') === company)
      .filter(x => !t || [x.quote_number, x.client, x.reference_po, x.subject, x.status].some(v => String(v || '').toLowerCase().includes(t)))
  }, [quotes, company, q])

  const pages = Math.max(1, Math.ceil(list.length / PER_PAGE))
  const pg = Math.min(page, pages)
  const rows = list.slice((pg - 1) * PER_PAGE, pg * PER_PAGE)

  const pdf = async (x: Quote) => {
    setPdfBusy(x.id)
    try { await downloadQuotePdf(x.id, x.subject || x.quote_number || x.id) }
    catch (e) { alert('PDF error: ' + (e as Error).message) }
    finally { setPdfBusy(null) }
  }

  return (
    <div className="page quotes">
      <div className="ph">
        <div className="ph-co">
          <div>
            <h1>Quotations</h1>
            <p>{COMPANY_NAME[company]}</p>
          </div>
          <div className="seg" role="tablist" aria-label="Company">
            <button type="button" role="tab" aria-selected={company === 'spm'} className={company === 'spm' ? 'on' : ''} onClick={() => pick('spm')}>Special Piping</button>
            <button type="button" role="tab" aria-selected={company === 'danmag'} className={company === 'danmag' ? 'on' : ''} onClick={() => pick('danmag')}>Daniel Mag</button>
          </div>
        </div>
        <Link className="btn" to="/quotes/new">+ New quote</Link>
      </div>

      <div className="filters">
        <label className="search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input type="search" value={q} onChange={e => { setQ(e.target.value); setPage(1) }} placeholder="Search quote number, client, reference, subject…" aria-label="Search quotations" />
        </label>
      </div>

      <div className="card table-card">
        <div className="tw">
          <table className="ot qt-tbl">
            <thead>
              <tr>
                {['Quote #', 'Client', 'Reference REQ', 'Subject'].map(h => <th key={h}><button type="button" tabIndex={-1}>{h}</button></th>)}
                <th className="r"><button type="button" tabIndex={-1}>Total ($)</button></th>
                <th><button type="button" tabIndex={-1}>Date</button></th>
                <th><button type="button" tabIndex={-1}>Status</button></th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {quotes === null && Array.from({ length: 6 }, (_, i) => (
                <tr key={i} className="skel-row">{Array.from({ length: 8 }, (_, j) => <td key={j}><span className="skel" /></td>)}</tr>
              ))}
              {rows.map(x => (
                <tr key={x.id} onClick={() => nav(`/quotes/${x.id}`)}>
                  <td className="mono qn">{x.quote_number || '—'}</td>
                  <td>{(x.client || '').toUpperCase()}</td>
                  <td className="mono ref" title={x.reference_po || ''}>{x.reference_po || '—'}</td>
                  <td className="subj" title={x.subject || ''}>{x.subject || '—'}</td>
                  <td className="r mono">{x.total != null ? '$' + Number(x.total).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}</td>
                  <td>{x.quote_date ? fmtDate(x.quote_date)?.d : '—'}</td>
                  <td><span className={`pill ${STATUS_TONE[x.status || 'draft'] || 'n'}`}><i />{x.status || 'draft'}</span></td>
                  <td className="r acts" onClick={e => e.stopPropagation()}>
                    <Link className="btn ghost sm" to={`/quotes/${x.id}`}>Edit</Link>
                    <button type="button" className="btn ghost sm" onClick={() => pdf(x)} disabled={pdfBusy === x.id}>{pdfBusy === x.id ? '…' : 'PDF'}</button>
                    <button type="button" className="btn ghost sm del" onClick={() => setDeleting(x)} title="Delete this quote">Del</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {quotes !== null && !list.length && (
            <p className={`card-empty${err ? ' err-text' : ''}`}>{err || (q ? 'No quotes match your search.' : 'No quotations yet — click New quote to create one.')}</p>
          )}
        </div>
        <Pager page={pg} total={list.length} perPage={PER_PAGE} onPage={setPage} />
      </div>

      {deleting && <DeleteQuote quote={deleting} onClose={() => setDeleting(null)} onDone={() => { setDeleting(null); load() }} />}
    </div>
  )
}

function DeleteQuote({ quote, onClose, onDone }: { quote: Quote; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  const go = async () => {
    setBusy(true); setErr('')
    try { await send('DELETE', `/api/quotations/${quote.id}`); onDone() }
    catch (e) { setErr('Delete failed: ' + (e as Error).message); setBusy(false) }
  }
  return (
    <div className="cell-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="cell-box" role="dialog" style={{ maxWidth: 360 }}>
        <div className="cell-hd"><span className="cell-col">Delete quotation</span><button type="button" className="cell-close" onClick={onClose} aria-label="Close">×</button></div>
        <p className="ds-text">Are you sure you want to delete <strong>{quote.quote_number || 'this quote'}</strong>? This cannot be undone.</p>
        <div className="cell-btns">
          <button type="button" className="btn danger" onClick={go} disabled={busy}>{busy ? 'Deleting…' : 'Delete'}</button>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        </div>
        {err && <p className="err-text" style={{ marginTop: 8 }}>{err}</p>}
      </div>
    </div>
  )
}
