import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, getJson, send } from '../../lib/api'
import { fmtDate } from '../../lib/orders'
import { currentUser } from '../../lib/session'
import Pager from '../../lib/Pager'
import './stock.css'

/*
  Stock: what is physically on the shelf, counted and entered by hand.

  A port of web/inventory.js. It is a manual register, not a calculated
  balance - nothing in the order pipeline records quantities - so every change
  is logged and every way of losing a count someone walked the warehouse to
  take is guarded against: blank is not zero, a count changed by someone else
  in the meantime is refused, and unsaved work is never thrown away silently.
*/

type Item = {
  id: string; stock_id?: string; stock_seq?: number; description?: string
  quantity?: number | string; uom?: string; unit_cost?: number | string | null
  notes?: string | null; last_counted_at?: string
}
type Move = { change: number | string; note?: string; created_by?: string; created_at?: string }

const PER_PAGE = 100
const UNITS = ['EA', 'PC', 'SET', 'KIT', 'SH', 'MTR', 'ROLL', 'BOX', 'PACK', 'KG', 'LTR']

const num = (v: unknown) => { const n = parseFloat(String(v)); return isNaN(n) ? 0 : n }
const money = (n: number) => '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
// Counts are stored to three decimals but are whole numbers nearly always.
const qty = (v: unknown) => { const n = num(v); return n === Math.round(n) ? String(Math.round(n)) : n.toLocaleString('en-US', { maximumFractionDigits: 3 }) }
const priced = (x: Item) => x.unit_cost != null && x.unit_cost !== ''
const when = (v?: string) => { const f = fmtDate(v); return f ? f.d + (f.t ? ' ' + f.t : '') : '—' }
const canDelete = () => ['admin', 'procurement'].includes(currentUser()?.role || '')

type Form = { description: string; quantity: string; uom: string; unit_cost: string; notes: string; note: string }
const EMPTY: Form = { description: '', quantity: '0', uom: 'EA', unit_cost: '', notes: '', note: '' }
const snap = (f: Form) => Object.values(f).map(v => v.trim()).join('\u0000')

function StockForm({ editId, stock, onClose, onSaved }: {
  editId: string | null; stock: Item[]; onClose: () => void; onSaved: (logged: boolean) => void
}) {
  const [form, setForm] = useState<Form>(EMPTY)
  const [stockId, setStockId] = useState('')
  const [opened, setOpened] = useState(snap(EMPTY))
  const [seenQty, setSeenQty] = useState(0)
  const [moves, setMoves] = useState<Move[]>([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(!!editId)
  const descRef = useRef<HTMLInputElement>(null)
  const qtyRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!editId) { descRef.current?.focus(); return }
    getJson<Item & { movements?: Move[] }>(`/api/stock/${editId}`).then(item => {
      const f: Form = {
        description: item.description || '', quantity: qty(item.quantity), uom: item.uom || 'EA',
        unit_cost: item.unit_cost != null ? String(item.unit_cost) : '', notes: item.notes || '', note: '',
      }
      setForm(f); setOpened(snap(f)); setSeenQty(num(item.quantity))
      setStockId(item.stock_id || ''); setMoves(item.movements || []); setLoading(false)
    }, e => { setErr('Could not load this item: ' + (e as Error).message); setLoading(false) })
  }, [editId])

  const dirty = snap(form) !== opened
  // A count is typed once, standing at a shelf: closing it by accident asks first.
  const tryClose = useCallback(() => {
    if (dirty && !window.confirm('You have typed something that is not saved yet.\n\nPress Cancel to go back and save it, or OK to throw it away.')) return
    onClose()
  }, [dirty, onClose])

  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') tryClose() }
    const leave = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = '' } }
    window.addEventListener('keydown', key); window.addEventListener('beforeunload', leave)
    return () => { window.removeEventListener('keydown', key); window.removeEventListener('beforeunload', leave) }
  }, [tryClose, dirty])

  // The next number, previewed from the list; the server issues the real one on save.
  const preview = useMemo(() => {
    const high = stock.reduce((m, x) => Math.max(m, parseInt(String(x.stock_seq), 10) || 0), 0)
    return 'STK-' + String(high + 1).padStart(4, '0')
  }, [stock])
  const shownId = editId ? stockId : form.description.trim() ? preview : ''
  const qtyChanged = !!editId && Math.abs(num(form.quantity) - seenQty) > 1e-9

  const set = (k: keyof Form) => (e: { target: { value: string } }) => setForm(f => ({ ...f, [k]: e.target.value }))

  const save = async () => {
    if (!form.description.trim()) { setErr('A description is required — it is what the count is against.'); descRef.current?.focus(); return }
    if (form.quantity.trim() === '') { setErr('Enter the quantity you counted. If there are none left, type 0.'); qtyRef.current?.focus(); return }
    setErr(''); setBusy(true)
    try {
      const body = {
        description: form.description.trim(), quantity: form.quantity.trim(),
        seen_quantity: editId ? seenQty : null, uom: form.uom || 'EA',
        unit_cost: form.unit_cost.trim() === '' ? null : num(form.unit_cost),
        notes: form.notes.trim(), note: form.note.trim(),
      }
      const res = await send<{ logged?: boolean }>(editId ? 'PUT' : 'POST', editId ? `/api/stock/${editId}` : '/api/stock', body)
      onSaved(res?.logged !== false)
    } catch (e) {
      // Never close on a failure: what is typed may be the only record of the count.
      // 409 (someone else counted it) and 400 (what to fix) already say what to do.
      const m = (e as Error).message
      const known = e instanceof ApiError && (e.status === 409 || e.status === 400)
      setErr(known ? m : 'NOT saved — ' + m + ' Your entry is still here; press Save again.')
    } finally { setBusy(false) }
  }

  return (
    <div className="cell-overlay" onClick={e => { if (e.target === e.currentTarget) tryClose() }}>
      <div className="cell-box sf" role="dialog" aria-label={editId ? 'Edit stock item' : 'New stock item'}>
        <div className="cell-hd">
          <span className="cell-col">{editId ? 'Edit stock item' : 'New stock item'}</span>
          <button type="button" className="cell-close" onClick={tryClose} aria-label="Close">×</button>
        </div>
        {err && <div className="sf-err" role="alert">{err}</div>}
        {loading ? <p className="muted">Loading…</p> : (
          <>
            <label className="sf-row">
              <span className="sf-lbl">Description <em>*</em></span>
              <input ref={descRef} value={form.description} onChange={set('description')} placeholder='3" GASKET 150# SPWD 316L GRAPHITE FILLED' />
            </label>
            <label className="sf-row">
              <span className="sf-lbl">ID</span>
              <input className="mono" value={shownId} readOnly placeholder="assigned once you describe the item" />
            </label>
            <div className="sf-grid">
              <label className="sf-row">
                <span className="sf-lbl">Quantity</span>
                <input ref={qtyRef} className="mono r" type="number" step="0.001" value={form.quantity} onChange={set('quantity')} />
              </label>
              <label className="sf-row">
                <span className="sf-lbl">Unit</span>
                <select value={form.uom} onChange={set('uom')}>{UNITS.map(u => <option key={u}>{u}</option>)}</select>
              </label>
            </div>
            <label className="sf-row">
              <span className="sf-lbl">Unit cost ($)</span>
              <input className="mono r" type="number" step="0.0001" value={form.unit_cost} onChange={set('unit_cost')} placeholder="optional" />
            </label>
            <label className="sf-row">
              <span className="sf-lbl">Notes</span>
              <input value={form.notes} onChange={set('notes')} placeholder="optional" />
            </label>
            {qtyChanged && (
              <label className="sf-row">
                <span className="sf-lbl">Reason for the change</span>
                <input value={form.note} onChange={set('note')} placeholder="e.g. recounted, issued to SO721900, damaged" autoFocus />
              </label>
            )}
            {moves.length > 0 && (
              <div className="sf-hist-wrap">
                <span className="sf-lbl">History</span>
                <ol className="sf-history">
                  {moves.map((m, i) => {
                    const d = num(m.change)
                    return (
                      <li key={i}>
                        <b className={d < 0 ? 'down' : 'up'}>{d > 0 ? '+' : ''}{qty(d)}</b>
                        <span>{m.note || 'adjusted'}{m.created_by ? ` · ${m.created_by}` : ''}</span>
                        <time>{when(m.created_at)}</time>
                      </li>
                    )
                  })}
                </ol>
              </div>
            )}
            <div className="cell-btns">
              <button type="button" className="btn" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
              <button type="button" className="btn ghost" onClick={tryClose}>Cancel</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function DeleteBox({ item, onClose, onDone }: { item: Item; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  const go = async () => {
    setBusy(true); setErr('')
    try { await send('DELETE', `/api/stock/${item.id}`); onDone() }
    catch (e) { setErr('Delete failed: ' + (e as Error).message); setBusy(false) }
  }
  return (
    <div className="cell-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="cell-box" role="dialog" style={{ maxWidth: 380 }}>
        <div className="cell-hd">
          <span className="cell-col">Delete stock item</span>
          <button type="button" className="cell-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <p className="ds-text">Remove <strong>{item.description || 'this item'}</strong> from the register? Its count history goes with it. This cannot be undone.</p>
        <div className="cell-btns">
          <button type="button" className="btn danger" onClick={go} disabled={busy}>{busy ? 'Deleting…' : 'Delete'}</button>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        </div>
        {err && <p className="err-text" style={{ marginTop: 8 }}>{err}</p>}
      </div>
    </div>
  )
}

export default function StockPage() {
  const [stock, setStock] = useState<Item[] | null>(null)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<string | null | undefined>(undefined)   // undefined = closed, null = new
  const [deleting, setDeleting] = useState<Item | null>(null)
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try { setStock(await getJson<Item[]>('/api/stock')); setErr('') }
    catch (e) { setErr('Could not load stock — ' + (e as Error).message + ' If this page is new, run migrations/create_stock.sql in Supabase.'); setStock([]) }
  }, [])
  useEffect(() => { load() }, [load])

  const list = useMemo(() => {
    const t = q.trim().toLowerCase()
    return (stock || []).filter(x => !t || [x.description, x.notes, x.stock_id].some(v => String(v || '').toLowerCase().includes(t)))
  }, [stock, q])

  const k = useMemo(() => {
    const valued = list.filter(priced)
    return {
      items: list.length, units: list.reduce((s, x) => s + num(x.quantity), 0),
      worth: valued.reduce((s, x) => s + num(x.quantity) * num(x.unit_cost), 0),
      valued: valued.length, zero: list.filter(x => num(x.quantity) <= 0).length,
    }
  }, [list])

  const pages = Math.max(1, Math.ceil(list.length / PER_PAGE))
  const pg = Math.min(page, pages)
  const rows = list.slice((pg - 1) * PER_PAGE, pg * PER_PAGE)
  const loaded = stock !== null
  const n = (v: string | number) => (loaded ? v : <span className="skel" />)
  const del = canDelete()

  return (
    <div className="page stock">
      <div className="ph">
        <div>
          <h1>Stock</h1>
          <p>What is physically on the shelf — counted and entered by hand</p>
        </div>
        <button type="button" className="btn" onClick={() => setEditing(null)}>+ New item</button>
      </div>

      {notice && <div className="notice" role="status">{notice}<button type="button" className="link" onClick={() => setNotice('')}>Dismiss</button></div>}

      <div className="kpis">
        <div className="kpi card"><span className="kpi-lbl">Items</span><b className="kpi-val mono">{n(k.items)}</b></div>
        <div className="kpi card"><span className="kpi-lbl">Total units</span><b className="kpi-val mono">{n(qty(k.units))}</b></div>
        <div className="kpi card"><span className="kpi-lbl">Value</span><b className="kpi-val mono">{n(money(k.worth))}</b><span className="kpi-note">{k.valued} of {k.items} priced</span></div>
        <div className={`kpi card${k.zero ? ' zero' : ''}`}><span className="kpi-lbl">At zero</span><b className="kpi-val mono">{n(k.zero)}</b></div>
      </div>

      <div className="filters">
        <label className="search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input type="search" value={q} onChange={e => { setQ(e.target.value); setPage(1) }} placeholder="Search ID, description or notes…" aria-label="Search stock" />
        </label>
      </div>

      <div className="card table-card">
        <div className="tw">
          <table className="ot st-tbl">
            <thead>
              <tr>
                <th><button type="button" tabIndex={-1}>ID</button></th>
                <th><button type="button" tabIndex={-1}>Description</button></th>
                <th className="r"><button type="button" tabIndex={-1}>Qty</button></th>
                <th><button type="button" tabIndex={-1}>Unit</button></th>
                <th className="r"><button type="button" tabIndex={-1}>Value</button></th>
                <th><button type="button" tabIndex={-1}>Last counted</button></th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {!loaded && Array.from({ length: 6 }, (_, i) => (
                <tr key={i} className="skel-row">{Array.from({ length: 7 }, (_, j) => <td key={j}><span className="skel" /></td>)}</tr>
              ))}
              {rows.map(x => {
                const low = num(x.quantity) <= 0
                return (
                  <tr key={x.id} className={low ? 'low' : ''} onClick={() => setEditing(x.id)}>
                    <td className="mono id">{x.stock_id || '—'}</td>
                    <td className="desc">{x.description || '—'}</td>
                    <td className={`r mono${low ? ' crit' : ''}`}>{qty(x.quantity)}</td>
                    <td>{x.uom || 'EA'}</td>
                    <td className="r mono">{priced(x) ? money(num(x.quantity) * num(x.unit_cost)) : <span className="null">—</span>}</td>
                    <td>{when(x.last_counted_at)}</td>
                    <td className="r acts" onClick={e => e.stopPropagation()}>
                      <button type="button" className="btn ghost sm" onClick={() => setEditing(x.id)}>Edit</button>
                      {del && <button type="button" className="btn ghost sm del" onClick={() => setDeleting(x)}>Delete</button>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {loaded && !rows.length && (
            <p className={`card-empty${err ? ' err-text' : ''}`}>
              {err || (q ? 'Nothing matches your search.' : 'Nothing counted yet — press New item to record what is on the shelf.')}
            </p>
          )}
        </div>
        <Pager page={pg} total={list.length} perPage={PER_PAGE} onPage={setPage} />
      </div>

      {editing !== undefined && (
        <StockForm editId={editing} stock={stock || []} onClose={() => setEditing(undefined)}
                   onSaved={logged => {
                     setEditing(undefined); load()
                     // The count is saved either way; only its history entry may be missing.
                     if (!logged) setNotice("Saved — but the change could not be added to the item's history. The count itself is recorded.")
                   }} />
      )}
      {deleting && <DeleteBox item={deleting} onClose={() => setDeleting(null)} onDone={() => { setDeleting(null); load() }} />}
    </div>
  )
}
