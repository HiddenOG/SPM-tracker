import { Fragment, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useOrders } from '../../lib/data'
import {
  type ChevronOrder, type NlngOrder, type OtdClass,
  chevronStage, dateMidnight, fmtDur, inReceivedPeriod, nlngOtdStage, otdClass, otdDate, otdDaysLeft,
  poPromisedDate, receivedPeriods,
} from '../../lib/orders'
import { usePopover } from '../../lib/usePopover'
import Pager from '../../lib/Pager'
import './delays.css'

/*
  Delays: is every order going to arrive by its date? The cards count each
  state and filter the table; the score is weighted by line items, as Chevron
  measures it. Every count and label follows renderOTD() in web/script.js.
*/

type Client = 'chevron' | 'nlng'
type O = ChevronOrder & NlngOrder
type Row = { o: O; cls: OtdClass; stage: string; lastTs: number; items: number }

const PER_PAGE = 100
const CHEVRON_STEPS: [string, string][] = [
  ['notification_received_at', 'Received'], ['order_submitted_on', 'Submitted'], ['sent_to_warehouse_at', 'Warehouse'],
  ['stock_check_completed_at', 'Stock ✓'], ['spm_po_sent_at', 'PO → Flex'], ['so_received_at', 'SO rcvd'],
  ['so_sent_to_warehouse_at', 'WH fwd'], ['flex_dispatch_ready_at', 'Packed'], ['dispatch_instructions_sent_at', 'Instr sent'],
  ['ready_for_dispatch_at', 'Coll. arr.'], ['dispatched_at', 'Dispatched'], ['delivery_requested_at', 'Del. req.'], ['delivered_at', 'Delivered'],
]
const NLNG_STEPS: [string, string][] = [
  ['notification_received_at', 'Received'], ['sent_to_warehouse_at', 'To warehouse'], ['stock_check_completed_at', 'Stock ✓'],
  ['spm_po_sent_at', 'PO → Flex'], ['so_received_at', 'SO rcvd'], ['so_sent_to_warehouse_at', 'WH fwd'],
  ['flex_dispatch_ready_at', 'Packed'], ['dispatch_instructions_sent_at', 'Instr sent'], ['ready_for_dispatch_at', 'Coll. arr.'],
  ['dispatched_at', 'Dispatched'], ['delivered_at', 'Delivered'],
]
const OTD_LABEL: Record<OtdClass, string> = {
  'on-track': 'On track', 'at-risk': 'At risk', overdue: 'Overdue', critical: 'Critical',
  'del-otd': '✓ On time', 'del-late': 'Late delivery', 'no-date': 'No date',
}
const CLS_ORDER: Record<OtdClass, number> = { 'on-track': 1, 'at-risk': 2, overdue: 3, critical: 4, 'del-otd': 5, 'del-late': 6, 'no-date': 7 }
const FILTERS: [string, string][] = [['all', 'All'], ['on-track', 'On track'], ['at-risk', 'At risk'], ['critical', 'Critical'], ['overdue', 'Overdue'], ['delivered', 'Delivered']]

const day = (s?: string | null) => (s ? new Date(s).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: '2-digit' }) : '')
const short = (s?: string | null) => (s ? String(s).slice(5, 10).replace('-', '/') : '')
const ms = (s?: string | null) => (s ? new Date(s).getTime() : 0)

function DaysLeft({ o }: { o: O }) {
  const d = otdDate(o)
  if (o.delivered_at && d) {
    const diff = Math.round((dateMidnight(d)!.getTime() - dateMidnight(o.delivered_at)!.getTime()) / 86400000)
    return diff >= 0 ? <span className="dl ok"><b>✓</b><small>{diff}d early</small></span>
      : <span className="dl warn"><b>Late</b><small>{-diff}d after</small></span>
  }
  const dl = otdDaysLeft(o)
  if (dl === null) return <span className="dl grey"><b>—</b><small>no date</small></span>
  if (dl < 0) return <span className="dl crit"><b>−{-dl}d</b><small>overdue</small></span>
  if (dl < 7) return <span className="dl crit"><b>{dl}d</b><small>critical</small></span>
  return <span className={`dl ${dl < 30 ? 'warn' : 'ok'}`}><b>{dl}d</b><small>remaining</small></span>
}

function Gap({ o }: { o: O }) {
  if (!o.promised_date || !o.required_delivery_date) return <span className="null">—</span>
  const g = Math.round((ms(o.promised_date as string) - ms(o.required_delivery_date)) / 86400000)
  if (g < 0) return <span className="gap early">{-g}d early</span>
  if (g > 0) return <span className="gap late">+{g}d late</span>
  return <span className="gap">same day</span>
}

function Timeline({ o, steps }: { o: O; steps: [string, string][] }) {
  const last = steps.reduce((l, [k], i) => (o[k] ? i : l), -1)
  const curr = last === steps.length - 1 ? last : last + 1
  return (
    <ol className="tl">
      {steps.map(([k, label], i) => {
        const v = o[k] as string | undefined
        const state = v ? 'done' : i === curr ? 'curr' : 'todo'
        const next = steps[i + 1] && (o[steps[i + 1][0]] as string | undefined)
        const dur = i === steps.length - 1 ? null
          : v && next ? fmtDur(ms(next) - ms(v))
          : v && i === last && !o.delivered_at ? fmtDur(Date.now() - ms(v)) : '…'
        return (
          <li key={k} className={state}>
            <i aria-hidden="true" />
            <b>{label}</b>
            <small>{v ? short(v) : state === 'curr' ? 'now' : ''}</small>
            {dur !== null && <span className={`dur${v && i === last && !o.delivered_at ? ' live' : ''}`}>{dur}</span>}
          </li>
        )
      })}
    </ol>
  )
}

function Received({ value, periods, onChange }: { value: string; periods: { key: string; label: string }[]; onChange: (k: string) => void }) {
  const p = usePopover()
  const label = periods.find(x => x.key === value)?.label || 'Any time'
  return (
    <div className="pop-wrap" ref={p.ref}>
      <button type="button" className={`ctl${value !== 'all' ? ' set' : ''}`} onClick={() => p.setOpen(o => !o)} aria-expanded={p.open}>
        <span className="ctl-lbl">Received</span>{label}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="m6 9 6 6 6-6" /></svg>
      </button>
      {p.open && (
        <div className="pop menu" role="listbox">
          {periods.map(x => (
            <button key={x.key} type="button" role="option" aria-selected={x.key === value} className={x.key === value ? 'on' : ''}
                    onClick={() => { onChange(x.key); p.setOpen(false) }}>{x.label}</button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function DelaysPage() {
  const { chevron, nlng, loaded, error } = useOrders()
  const [params, setParams] = useSearchParams()
  const client: Client = params.get('client') === 'nlng' ? 'nlng' : 'chevron'
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState('all')
  const [sub, setSub] = useState<'all' | 'del-otd' | 'del-late'>('all')
  const [period, setPeriod] = useState('all')
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: 'notification_received_at', dir: -1 })
  const [page, setPage] = useState(1)
  const [open, setOpen] = useState<Set<string>>(new Set())

  const orders = (client === 'nlng' ? nlng : chevron) as O[]
  const steps = client === 'nlng' ? NLNG_STEPS : CHEVRON_STEPS
  const periods = useMemo(() => receivedPeriods(orders), [orders])

  const setClient = (c: Client) => {
    setParams(c === 'nlng' ? { client: 'nlng' } : {})
    setFilter('all'); setSub('all'); setPeriod('all'); setPage(1); setOpen(new Set())
  }
  const pick = (f: string) => { setFilter(f); setSub('all'); setPage(1) }

  // Search and the Received period narrow everything, the cards included.
  const scoped = useMemo<Row[]>(() => {
    const needle = q.trim().toLowerCase()
    return orders
      .filter(o => inReceivedPeriod(o, period))
      .filter(o => !needle || [client === 'nlng' ? o.po_number : o.buyer_po_number, o.spm_po_number, o.so_number, o.extracted_description,
        ...((client === 'nlng' ? o.nlng_order_line_items : o.order_line_items) || []).map(li => li.description)]
        .some(v => String(v || '').toLowerCase().includes(needle)))
      .map(o => {
        let lastTs = 0
        for (let i = steps.length - 1; i >= 0; i--) { const v = o[steps[i][0]] as string | undefined; if (v) { lastTs = ms(v); break } }
        const items = ((client === 'nlng' ? o.nlng_order_line_items : o.order_line_items) || []).length || 1
        return { o, cls: otdClass(o), stage: (client === 'nlng' ? nlngOtdStage(o) : chevronStage(o)).lbl, lastTs, items }
      })
  }, [orders, q, period, client, steps])

  const counts = useMemo(() => {
    const c: Partial<Record<OtdClass, number>> = {}
    let liOtd = 0, liLate = 0
    for (const r of scoped) {
      c[r.cls] = (c[r.cls] || 0) + 1
      if (r.cls === 'del-otd') liOtd += r.items
      if (r.cls === 'del-late') liLate += r.items
    }
    return { ...c, liOtd, liTotal: liOtd + liLate }
  }, [scoped])

  const visible = useMemo(() => {
    const keep = scoped.filter(r => {
      if (filter === 'all') return true
      if (filter === 'overdue') return r.cls === 'overdue' || r.cls === 'critical'
      if (filter === 'delivered') return (r.cls === 'del-otd' || r.cls === 'del-late') && (sub === 'all' || r.cls === sub)
      return r.cls === filter
    })
    const val = (r: Row): number | string => {
      switch (sort.key) {
        case '_in_stage': return r.lastTs
        case '_items': return r.items
        case '_otd': return CLS_ORDER[r.cls]
        case '_po_promised': return ms(poPromisedDate(r.o))
        case '_days_left': return ms(otdDate(r.o))
        case '_gap': return ms(r.o.promised_date as string)
        case 'overall_status': return r.stage
        default: return String(r.o[sort.key] ?? '')
      }
    }
    return [...keep].sort((a, b) => {
      const av = val(a), bv = val(b)
      const c = typeof av === 'number' && typeof bv === 'number' ? av - bv
        : String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' })
      return c * sort.dir
    })
  }, [scoped, filter, sub, sort])

  const pages = Math.max(1, Math.ceil(visible.length / PER_PAGE))
  const pg = Math.min(page, pages)
  const rows = visible.slice((pg - 1) * PER_PAGE, pg * PER_PAGE)
  const score = counts.liTotal ? Math.round((counts.liOtd / counts.liTotal) * 100) : null
  const scoreTone = score === null ? 'grey' : score >= 90 ? 'ok' : score >= 70 ? 'warn' : 'crit'
  const n = (v?: number) => (loaded ? v || 0 : <span className="skel" />)

  const cols: { key: string; label: string; c?: boolean }[] = [
    { key: client === 'nlng' ? 'po_number' : 'buyer_po_number', label: 'PO number' },
    { key: 'so_number', label: 'SO number' },
    { key: client === 'nlng' ? 'contact_name' : 'buyer_name', label: 'Buyer / requester' },
    ...(client === 'nlng' ? [] : [{ key: 'po_destination', label: 'Destination' }]),
    { key: 'notification_received_at', label: 'Received' },
    { key: 'required_delivery_date', label: 'Required by' },
    ...(client === 'nlng' ? [] : [{ key: '_po_promised', label: 'PO promised' }]),
    { key: '_days_left', label: 'Days left', c: true },
    { key: 'promised_date', label: 'SO promised' },
    { key: '_gap', label: 'Gap' },
    { key: 'overall_status', label: 'Current stage' },
    { key: '_in_stage', label: 'In stage' },
    { key: '_items', label: 'Items', c: true },
    { key: '_otd', label: 'OTD' },
  ]
  const sortBy = (key: string) => { setSort(s => (s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: 1 })); setPage(1) }
  const toggle = (id: string) => setOpen(s => { const x = new Set(s); x.has(id) ? x.delete(id) : x.add(id); return x })

  const Card = ({ f, tone, label, value, note }: { f: string; tone: string; label: string; value: React.ReactNode; note: string }) => (
    <button type="button" className={`ocard ${tone}${filter === f ? ' on' : ''}`} onClick={() => pick(filter === f ? 'all' : f)} aria-pressed={filter === f}>
      <span className="ocard-lbl">{label}</span><b>{value}</b><small>{note}</small>
    </button>
  )

  return (
    <div className="page delays">
      <div className="ph">
        <div>
          <h1>Delays</h1>
          {error ? <p className="err-note">Couldn’t load orders: {error}</p>
            : <p>On-time delivery for every {client === 'nlng' ? 'NLNG' : 'Chevron'} purchase order</p>}
        </div>
        <div className="seg" role="tablist" aria-label="Client">
          <button type="button" role="tab" aria-selected={client === 'chevron'} className={client === 'chevron' ? 'on' : ''} onClick={() => setClient('chevron')}>Chevron</button>
          <button type="button" role="tab" aria-selected={client === 'nlng'} className={client === 'nlng' ? 'on' : ''} onClick={() => setClient('nlng')}>NLNG</button>
        </div>
      </div>

      <div className="ocards">
        <Card f="on-track" tone="ok" label="On track" value={n(counts['on-track'])} note="More than 30 days left" />
        <Card f="at-risk" tone="warn" label="At risk" value={n(counts['at-risk'])} note="7 – 30 days left" />
        <Card f="overdue" tone="crit" label="Overdue / critical" value={n((counts.overdue || 0) + (counts.critical || 0))} note="Past the date or under 7 days" />
        <Card f="delivered" tone="grey" label="Delivered" value={n((counts['del-otd'] || 0) + (counts['del-late'] || 0))} note="Closed orders" />
        <button type="button" className={`ocard score ${scoreTone}`} onClick={() => pick('delivered')}>
          <span className="ocard-lbl">{client === 'nlng' ? 'NLNG' : 'Chevron'} OTD score</span>
          <b>{loaded ? (score === null ? '—' : `${score}%`) : <span className="skel" />}</b>
          <small>{counts.liTotal ? `${counts.liOtd} of ${counts.liTotal} line items on time` : 'No deliveries yet'}</small>
          <i className="bar" aria-hidden="true"><i style={{ width: `${score ?? 0}%` }} /></i>
        </button>
      </div>

      <div className="filters">
        <label className="search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input type="search" value={q} onChange={e => { setQ(e.target.value); setPage(1) }} placeholder="Search PO, SO, SPM PO or item…" aria-label="Search" />
        </label>
        <div className="ctls">
          <div className="chips" role="group" aria-label="Delivery status">
            {FILTERS.map(([k, l]) => <button key={k} type="button" className={`chip${filter === k ? ' on' : ''}`} onClick={() => pick(k)}>{l}</button>)}
          </div>
          <Received value={period} periods={periods} onChange={k => { setPeriod(k); setPage(1) }} />
        </div>
        {filter === 'delivered' && (
          <div className="chips sub" role="group" aria-label="Delivered">
            {([['all', 'All delivered'], ['del-otd', 'On time'], ['del-late', 'Late']] as const).map(([k, l]) => (
              <button key={k} type="button" className={`chip${sub === k ? ' on' : ''}`} onClick={() => { setSub(k); setPage(1) }}>{l}</button>
            ))}
          </div>
        )}
      </div>

      <div className="card table-card">
        <div className="tw">
          <table className="ot dt-tbl">
            <thead>
              <tr>
                <th className="x" aria-label="Expand" />
                {cols.map(c => (
                  <th key={c.key} className={c.c ? 'c' : ''} aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : undefined}>
                    <button type="button" onClick={() => sortBy(c.key)}>{c.label}<span className="sort" aria-hidden="true">{sort.key === c.key ? (sort.dir === 1 ? '↑' : '↓') : ''}</span></button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {!loaded && Array.from({ length: 8 }, (_, i) => (
                <tr key={'s' + i} className="skel-row"><td className="x" />{cols.map(c => <td key={c.key}><span className="skel" /></td>)}</tr>
              ))}
              {rows.map(({ o, cls, stage, lastTs, items }) => {
                const id = String(o.id)
                const po = (client === 'nlng' ? o.po_number : o.buyer_po_number) || '—'
                const isOpen = open.has(id)
                const value = client === 'nlng'
                  ? (o.net_value ? `${o.currency || 'USD'} ${Number(o.net_value).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '')
                  : (o.po_amount ? (o.po_currency === 'NGN' ? '₦' : '$') + Number(o.po_amount).toLocaleString('en-US', { maximumFractionDigits: 0 }) : '')
                const done = steps.filter(([k]) => o[k]).length
                return (
                  <Fragment key={id}>
                    <tr className={`odr ${cls}${isOpen ? ' open' : ''}`} onClick={() => toggle(id)}>
                      <td className="x"><span className={`chev${isOpen ? ' on' : ''}`} aria-hidden="true">›</span></td>
                      <td><b className="mono po">{po}</b>{value && <small className="sub-v mono">{value}</small>}</td>
                      <td>{o.so_number ? <span className="mono">{o.so_number}</span> : <span className="null">—</span>}</td>
                      <td className="trunc">{(client === 'nlng' ? o.contact_name : o.buyer_name) as string || <span className="null">—</span>}</td>
                      {client === 'chevron' && <td className="trunc">{o.po_destination as string || <span className="null">—</span>}</td>}
                      <td>{day(o.notification_received_at) || '—'}{o.notification_received_at && <small className="sub-v">{fmtDur(Date.now() - ms(o.notification_received_at))} ago</small>}</td>
                      <td>{day(o.required_delivery_date) || <span className="null">—</span>}</td>
                      {client === 'chevron' && <td>{day(poPromisedDate(o)) || <span className="null">—</span>}</td>}
                      <td className="c"><DaysLeft o={o} /></td>
                      <td>{day(o.promised_date as string) || <span className="null">—</span>}</td>
                      <td><Gap o={o} /></td>
                      <td className="trunc" title={stage}>{stage}</td>
                      <td className="mono in-stage">{lastTs && !o.delivered_at ? fmtDur(Date.now() - lastTs) : '—'}</td>
                      <td className="c"><span className={`li ${items >= 10 ? 'crit' : items >= 4 ? 'warn' : ''}`}>{items}</span></td>
                      <td><span className={`otd-b ${cls}`}>{OTD_LABEL[cls]}</span></td>
                    </tr>
                    {isOpen && (
                      <tr className="oxr">
                        <td colSpan={cols.length + 1}>
                          <div className="ox">
                            <div className="ox-hd">
                              <span>Time between each step</span>
                              <Link className="link" to={`/orders?${client === 'nlng' ? 'client=nlng&' : ''}id=${encodeURIComponent(id)}`}>Open the order →</Link>
                            </div>
                            <Timeline o={o} steps={steps} />
                            <dl className="ox-meta">
                              <div><dt>Pipeline age</dt><dd>{o.notification_received_at ? fmtDur(Date.now() - ms(o.notification_received_at)) : '—'}</dd></div>
                              <div><dt>Stages done</dt><dd>{done} / {steps.length}</dd></div>
                              {o.required_delivery_date && <div><dt>Required by</dt><dd>{day(o.required_delivery_date)}</dd></div>}
                              {client === 'chevron' && poPromisedDate(o) && <div><dt>PO promised</dt><dd>{day(poPromisedDate(o))}</dd></div>}
                              {!!o.promised_date && <div><dt>SO promised</dt><dd>{day(o.promised_date as string)}</dd></div>}
                              {o.delivered_at && <div><dt>Delivered</dt><dd>{day(o.delivered_at)}</dd></div>}
                            </dl>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
          {loaded && !visible.length && <p className="card-empty">{orders.length ? 'No orders match these filters.' : 'No orders yet.'}</p>}
        </div>
        <Pager page={pg} total={visible.length} perPage={PER_PAGE} onPage={setPage} />
      </div>
    </div>
  )
}
