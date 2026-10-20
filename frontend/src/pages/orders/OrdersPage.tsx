import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useOrders } from '../../lib/data'
import {
  type ChevronOrder, type NlngOrder, CHIP_STAGES, CLOSED_STATUSES, NLNG_CHIP_STAGES,
  nlngClosed, poPromisedDate,
} from '../../lib/orders'
import { usePopover } from '../../lib/usePopover'
import Pager from '../../lib/Pager'
import { CHEVRON_COLS, NLNG_COLS, type Col, type Order } from './columns'
import OrderPanel from './OrderPanel'
import './orders.css'

/*
  Orders: every Chevron or NLNG purchase order, searchable and filterable the
  way the old table was. Clicking a row opens the order's panel on the right;
  the open order is in the address (?client=…&id=…) so it can be linked to.
*/

type Client = 'chevron' | 'nlng'
const PER_PAGE = 100

const colsKey = (client: Client) => `spm_cols_${client}`
function readCols(client: Client, all: Col<never>[]): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(colsKey(client)) || 'null')
    if (Array.isArray(v)) return v
  } catch { /* use the defaults */ }
  return all.filter(c => c.show).map(c => c.key)
}

function matchesSearch(o: Order, q: string, items: { description?: string }[]): boolean {
  if (!q) return true
  for (const v of Object.values(o)) {
    if (v == null || typeof v === 'object') continue
    if (String(v).toLowerCase().includes(q)) return true
  }
  return items.some(li => (li.description || '').toLowerCase().includes(q))
}

function inRange(d: string | null | undefined, from: string, to: string): boolean {
  if (!from && !to) return true
  if (!d) return false
  const day = String(d).slice(0, 10)
  return !(from && day < from) && !(to && day > to)
}

function DateRange({ label, from, to, onChange }: { label: string; from: string; to: string; onChange: (f: string, t: string) => void }) {
  const p = usePopover()
  const short = (d: string) => d.slice(8, 10) + '/' + d.slice(5, 7)
  const text = !from && !to ? 'Any time' : from && to ? `${short(from)} – ${short(to)}` : from ? `From ${short(from)}` : `Until ${short(to)}`
  return (
    <div className="pop-wrap" ref={p.ref}>
      <button type="button" className={`ctl${from || to ? ' set' : ''}`} onClick={() => p.setOpen(o => !o)} aria-expanded={p.open}>
        <span className="ctl-lbl">{label}</span>{text}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="m6 9 6 6 6-6" /></svg>
      </button>
      {p.open && (
        <div className="pop dr">
          <label>From<input type="date" value={from} onChange={e => onChange(e.target.value, to)} /></label>
          <label>To<input type="date" value={to} onChange={e => onChange(from, e.target.value)} /></label>
          <button type="button" className="link" onClick={() => { onChange('', ''); p.setOpen(false) }}>Clear</button>
        </div>
      )}
    </div>
  )
}

function ColumnsMenu<O>({ all, shown, onChange }: { all: Col<O>[]; shown: string[]; onChange: (keys: string[]) => void }) {
  const p = usePopover()
  const set = new Set(shown)
  const toggle = (k: string) => onChange(all.filter(c => !c.fixed && (c.key === k ? !set.has(k) : set.has(c.key))).map(c => c.key))
  return (
    <div className="pop-wrap" ref={p.ref}>
      <button type="button" className="ctl" onClick={() => p.setOpen(o => !o)} aria-expanded={p.open}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M4 5h16M4 12h16M4 19h16M9 3v4M15 10v4M7 17v4" /></svg>
        Columns <span className="count">{shown.length}</span>
      </button>
      {p.open && (
        <div className="pop cols">
          <div className="pop-hd">
            <span>Show columns</span>
            <button type="button" className="link" onClick={() => onChange(all.filter(c => c.show).map(c => c.key))}>Reset</button>
          </div>
          <div className="cols-list">
            {all.filter(c => !c.fixed).map(c => (
              <label key={c.key} className="check">
                <input type="checkbox" checked={set.has(c.key)} onChange={() => toggle(c.key)} />{c.label}
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function csvDownload<O>(rows: O[], cols: Col<O>[], name: string) {
  const q = (s: string) => '"' + s.replace(/"/g, '""') + '"'
  const lines = [cols.map(c => q(c.label)).join(','), ...rows.map(o => cols.map(c => q(c.csv(o))).join(','))]
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' }))
  a.download = `${name}-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}

export default function OrdersPage() {
  const { chevron, nlng, loaded, error, reload } = useOrders()
  const [params, setParams] = useSearchParams()
  const client: Client = params.get('client') === 'nlng' ? 'nlng' : 'chevron'
  const openId = params.get('id')

  const [q, setQ] = useState('')
  const [stage, setStage] = useState('all')
  const [hasSo, setHasSo] = useState(false)
  const [rcv, setRcv] = useState<[string, string]>(['', ''])
  const [prm, setPrm] = useState<[string, string]>(['', ''])
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: 'notification_received_at', dir: -1 })
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [refreshing, setRefreshing] = useState(false)

  const allCols = (client === 'nlng' ? NLNG_COLS : CHEVRON_COLS) as Col<Order>[]
  const [shown, setShown] = useState<string[]>(() => readCols(client, allCols as Col<never>[]))

  // A different client: its own columns, filters back to the start.
  useEffect(() => {
    setShown(readCols(client, allCols as Col<never>[]))
    setStage('all'); setPage(1); setSelected(new Set())
  }, [client]) // eslint-disable-line react-hooks/exhaustive-deps

  const saveCols = (keys: string[]) => {
    setShown(keys)
    try { localStorage.setItem(colsKey(client), JSON.stringify(keys)) } catch { /* not remembered */ }
  }

  const rows = useMemo<Order[]>(() => {
    const needle = q.trim().toLowerCase()
    let out: Order[]
    if (client === 'nlng') {
      out = nlng.filter(o =>
        matchesSearch(o, needle, o.nlng_order_line_items || []) &&
        (stage === 'all' || (stage === 'live' ? !nlngClosed(o) : stage === 'closed' ? nlngClosed(o) : o.overall_status === stage)) &&
        (!hasSo || !!o.so_number) &&
        inRange(o.notification_received_at, rcv[0], rcv[1]) &&
        inRange(o.promised_date as string | undefined, prm[0], prm[1]))
    } else {
      out = chevron.filter(o =>
        matchesSearch(o, needle, o.order_line_items || []) &&
        (stage === 'all' || (stage === 'live' ? !CLOSED_STATUSES.has(o.overall_status || '')
          : stage === 'closed' ? CLOSED_STATUSES.has(o.overall_status || '') : o.overall_status === stage)) &&
        (!hasSo || !!o.so_number) &&
        inRange(o.notification_received_at, rcv[0], rcv[1]) &&
        inRange(poPromisedDate(o), prm[0], prm[1]))
    }
    const col = allCols.find(c => c.key === sort.key)
    if (col) {
      out = [...out].sort((a, b) => {
        const av = col.sort(a), bv = col.sort(b)
        const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv
          : String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' })
        return cmp * sort.dir
      })
    }
    return out
  }, [client, chevron, nlng, q, stage, hasSo, rcv, prm, sort, allCols])

  useEffect(() => { setPage(1) }, [q, stage, hasSo, rcv, prm, sort])
  const pages = Math.max(1, Math.ceil(rows.length / PER_PAGE))
  const pageRows = rows.slice((Math.min(page, pages) - 1) * PER_PAGE, Math.min(page, pages) * PER_PAGE)

  const cols = allCols.filter(c => c.fixed || shown.includes(c.key))
  const chips = client === 'nlng' ? NLNG_CHIP_STAGES : CHIP_STAGES
  const total = client === 'nlng' ? nlng.length : chevron.length
  const filtered = !!(q || stage !== 'all' || hasSo || rcv[0] || rcv[1] || prm[0] || prm[1])

  const open = (o: Order) => { const p = new URLSearchParams(params); p.set('id', String(o.id)); setParams(p) }
  const close = () => { const p = new URLSearchParams(params); p.delete('id'); p.delete('tab'); setParams(p) }
  const setClient = (c: Client) => setParams(c === 'nlng' ? { client: 'nlng' } : {})

  const pageIds = pageRows.map(o => String(o.id))
  const allOnPage = pageIds.length > 0 && pageIds.every(id => selected.has(id))
  const togglePage = () => setSelected(s => {
    const n = new Set(s)
    pageIds.forEach(id => (allOnPage ? n.delete(id) : n.add(id)))
    return n
  })
  const toggleOne = (id: string) => setSelected(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })

  const exportCsv = () => {
    const picked = selected.size ? rows.filter(o => selected.has(String(o.id))) : rows
    csvDownload(picked, allCols, client === 'nlng' ? 'nlng-orders' : 'orders')
  }
  const refresh = async () => { setRefreshing(true); await reload(); setRefreshing(false) }

  const openOrder = openId ? (client === 'nlng' ? nlng : chevron).find(o => String(o.id) === openId) : undefined

  const sortBy = (key: string) => setSort(s => s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: 1 })

  return (
    <div className="page orders">
      <div className="ph">
        <div>
          <h1>Orders</h1>
          {error ? <p className="err-note">Couldn’t load orders: {error}</p>
            : <p>{loaded ? (filtered ? `${rows.length} of ${total} ` : `${total} `) + (client === 'nlng' ? 'NLNG' : 'Chevron') + ' purchase orders' : 'Loading orders…'}</p>}
        </div>
        <div className="ph-side">
          <div className="seg" role="tablist" aria-label="Client">
            <button type="button" role="tab" aria-selected={client === 'chevron'} className={client === 'chevron' ? 'on' : ''} onClick={() => setClient('chevron')}>Chevron</button>
            <button type="button" role="tab" aria-selected={client === 'nlng'} className={client === 'nlng' ? 'on' : ''} onClick={() => setClient('nlng')}>NLNG</button>
          </div>
          <button type="button" className="btn ghost" onClick={refresh} disabled={refreshing} title="Load the latest orders">
            <svg className={refreshing ? 'spin' : ''} width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5" /></svg>
            Refresh
          </button>
          <button type="button" className="btn" onClick={exportCsv} disabled={!rows.length}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 3v12M7 10l5 5 5-5M5 21h14" /></svg>
            {selected.size ? `Export ${selected.size}` : 'Export'}
          </button>
        </div>
      </div>

      <div className="filters">
        <label className="search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input type="search" value={q} onChange={e => setQ(e.target.value)}
                 placeholder={client === 'nlng' ? 'Search PO, description, buyer…' : 'Search PO, description, buyer, SO…'} aria-label="Search orders" />
        </label>
        <div className="chips" role="group" aria-label="Status">
          {chips.map(c => (
            <button key={c.key} type="button" className={`chip${stage === c.key ? ' on' : ''}`} onClick={() => setStage(c.key)}>{c.label}</button>
          ))}
          <span className="chip-sep" aria-hidden="true" />
          <button type="button" className={`chip${hasSo ? ' on' : ''}`} onClick={() => setHasSo(v => !v)} aria-pressed={hasSo}
                  title="Only orders with a Flexitallic SO number">Has SO</button>
        </div>
        <div className="ctls">
          <DateRange label="Received" from={rcv[0]} to={rcv[1]} onChange={(f, t) => setRcv([f, t])} />
          <DateRange label="Promised" from={prm[0]} to={prm[1]} onChange={(f, t) => setPrm([f, t])} />
          {filtered && (
            <button type="button" className="link" onClick={() => { setQ(''); setStage('all'); setHasSo(false); setRcv(['', '']); setPrm(['', '']) }}>
              Clear filters
            </button>
          )}
          <span className="sp" />
          <ColumnsMenu all={allCols} shown={shown} onChange={saveCols} />
        </div>
      </div>

      <div className="card table-card">
        <div className="tw">
          <table className="ot">
            <thead>
              <tr>
                <th className="cb"><input type="checkbox" checked={allOnPage} onChange={togglePage} aria-label="Select this page" /></th>
                {cols.map(c => (
                  <th key={c.key} className={`${c.fixed ? 'fixed' : ''}${c.align === 'right' ? ' r' : ''}`}
                      aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : undefined}>
                    <button type="button" onClick={() => sortBy(c.key)}>
                      {c.label}
                      <span className="sort" aria-hidden="true">{sort.key === c.key ? (sort.dir === 1 ? '↑' : '↓') : ''}</span>
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {!loaded && Array.from({ length: 8 }, (_, i) => (
                <tr key={'s' + i} className="skel-row"><td className="cb" />{cols.map(c => <td key={c.key}><span className="skel" /></td>)}</tr>
              ))}
              {pageRows.map(o => {
                const id = String(o.id)
                return (
                  <tr key={id} className={`${openId === id ? 'open' : ''}${selected.has(id) ? ' sel' : ''}`} onClick={() => open(o)}>
                    <td className="cb" onClick={e => e.stopPropagation()}>
                      <input type="checkbox" checked={selected.has(id)} onChange={() => toggleOne(id)} aria-label="Select order" />
                    </td>
                    {cols.map(c => <td key={c.key} className={`${c.fixed ? 'fixed' : ''}${c.align === 'right' ? ' r' : ''}`}>{c.cell(o)}</td>)}
                  </tr>
                )
              })}
            </tbody>
          </table>
          {loaded && !rows.length && (
            <p className="card-empty">{total ? 'No orders match these filters.' : 'No orders yet.'}</p>
          )}
        </div>
        <Pager page={Math.min(page, pages)} total={rows.length} perPage={PER_PAGE} onPage={setPage} />
      </div>

      {openId && (
        <OrderPanel
          client={client}
          order={openOrder as (ChevronOrder & NlngOrder) | undefined}
          loaded={loaded}
          tab={params.get('tab') || 'overview'}
          onTab={t => { const p = new URLSearchParams(params); p.set('tab', t); setParams(p, { replace: true }) }}
          onClose={close}
          onSaved={reload}
        />
      )}
    </div>
  )
}
