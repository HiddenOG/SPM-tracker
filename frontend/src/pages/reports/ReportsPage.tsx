import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useOrders } from '../../lib/data'
import {
  type LiField, type O, RPT_STAGES, bucketCh, bucketNl, catQuantities, catValues, dueYears,
  filterByDate, fmtQty, fmtUsd, monthBuckets, orderYears, otd, otdByMonth,
} from './calc'
import { Bars, Columns, Donut, Perf, TipProvider, Trend, type Seg } from './charts'
import { CLOSED_STATUSES } from '../../lib/orders'
import './reports.css'

/*
  Reports: summary analytics per client, a port of web/reports.js.
  Chevron draws in the accent colour, NLNG in --ch1, as before.
*/

type Client = 'chevron' | 'nlng'
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
// The product-value donut's own palette (reports.js _RPT_PIE_COLORS).
const PIE = ['#3B82F6', '#F59E0B', '#10B981', '#8B5CF6', '#EF4444', '#06B6D4', '#F97316', '#84CC16', '#EC4899', '#6366F1', '#14B8A6', '#A16207']

function Report({ client, orders }: { client: Client; orders: O[] }) {
  const isCh = client === 'chevron'
  const li: LiField = isCh ? 'order_line_items' : 'nlng_order_line_items'
  const color = isCh ? 'var(--accent)' : 'var(--ch1)'

  const [barMode, setBarMode] = useState<'received' | 'due'>('received')
  const [barYear, setBarYear] = useState(new Date().getFullYear())
  const [yr, setYr] = useState('')
  const [mo, setMo] = useState('')
  const [trendMode, setTrendMode] = useState<'delivery' | 'due'>('delivery')

  const s = useMemo(() => {
    const closed = isCh ? orders.filter(o => CLOSED_STATUSES.has(o.overall_status || '')) : orders.filter(o => o.overall_status === 'delivered' || !!o.delivered_at)
    const active = isCh ? orders.filter(o => !CLOSED_STATUSES.has(o.overall_status || '')) : orders.filter(o => o.overall_status !== 'delivered' && !o.delivered_at)
    const overdue = active.filter(o => o.required_delivery_date && new Date(o.required_delivery_date) < new Date()).length
    const buckets = [0, 0, 0, 0, 0]
    orders.forEach(o => { buckets[(isCh ? bucketCh : bucketNl)(o)]++ })
    return { closed, active, overdue, otd: otd(closed, li), buckets }
  }, [orders, isCh, li])

  const bars = useMemo(() => ({
    months: monthBuckets(orders, barMode === 'received' ? 'notification_received_at' : 'required_delivery_date', barMode === 'due' ? barYear : null),
    delivered: barMode === 'received' ? monthBuckets(orders, 'delivered_at') : null,
  }), [orders, barMode, barYear])

  const filtered = useMemo(() => filterByDate(orders, yr, mo), [orders, yr, mo])
  const qty = useMemo(() => catQuantities(filtered, li), [filtered, li])
  const value = useMemo(() => catValues(filtered, li), [filtered, li])
  const trend = useMemo(() => otdByMonth(orders, li, trendMode), [orders, li, trendMode])
  const years = useMemo(() => orderYears(orders), [orders])

  const valueSegs: Seg[] = useMemo(() => {
    const top = value.slice(0, 10).map((c, i) => ({ label: c.name, v: c.v, color: PIE[i % PIE.length], samples: c.samples }))
    const rest = value.slice(10)
    const other = rest.reduce((t, c) => t + c.v, 0)
    return other > 0 ? [...top, { label: 'Other', v: other, color: PIE[top.length % PIE.length], samples: rest.map(c => c.name) }] : top
  }, [value])
  const valueTotal = valueSegs.reduce((t, x) => t + x.v, 0)

  const score = s.otd.score
  const scoreTone = score == null ? '' : score >= 90 ? 'ok' : score >= 75 ? 'warn' : 'crit'

  return (
    <>
      <div className="kpis rpt-kpis">
        <div className="kpi card"><span className="kpi-lbl">Total POs</span><b className="kpi-val">{orders.length}</b><span className="kpi-note">Received from {isCh ? 'Chevron' : 'NLNG'}</span></div>
        <div className="kpi card"><span className="kpi-lbl">Active</span><b className="kpi-val">{s.active.length}</b><span className="kpi-note">In pipeline</span></div>
        <div className="kpi card"><span className="kpi-lbl">Delivered</span><b className="kpi-val">{s.closed.length}</b><span className="kpi-note">Complete</span></div>
        <div className="kpi card"><span className="kpi-lbl">OTD score</span><b className={`kpi-val ${scoreTone}`}>{score != null ? `${score}%` : '—'}</b>
          <span className="kpi-note">{s.otd.total > 0 ? `${s.otd.onTime} / ${s.otd.total} line items` : 'On-time delivery rate'}</span></div>
        <div className="kpi card"><span className="kpi-lbl">Active overdue</span><b className={`kpi-val${s.overdue ? ' crit' : ''}`}>{s.overdue}</b><span className="kpi-note">Past required date</span></div>
      </div>

      <div className="rpt-row">
        <section className="card">
          <div className="card-hd"><h2>Pipeline breakdown</h2><span className="hd-note">Current snapshot</span></div>
          <div className="rpt-pad">
            <Donut segs={RPT_STAGES.map((st, i) => ({ label: st.label, v: s.buckets[i], color: st.color }))}
                   centre={String(orders.length)} centreLbl="TOTAL POs" fmt={v => String(v)}
                   tipVal={(sg, pct) => `${sg.v} orders · ${pct}%`} />
          </div>
        </section>
        <section className="card">
          <div className="card-hd">
            <h2>Monthly intake</h2>
            <div className="rpt-btns">
              <button type="button" className={`rpt-btn${barMode === 'received' ? ' on' : ''}`} style={barMode === 'received' ? { background: color, borderColor: color } : undefined} onClick={() => setBarMode('received')}>Received</button>
              <button type="button" className={`rpt-btn${barMode === 'due' ? ' on' : ''}`} style={barMode === 'due' ? { background: color, borderColor: color } : undefined} onClick={() => setBarMode('due')}>Due for delivery</button>
              {barMode === 'due' && (
                <select className="rpt-sel" value={barYear} onChange={e => setBarYear(+e.target.value)} aria-label="Year">
                  {dueYears(orders).map(y => <option key={y} value={y}>{y}</option>)}
                </select>
              )}
            </div>
          </div>
          <div className="rpt-pad sm">
            <Bars months={bars.months} delivered={bars.delivered} color={color} tipLabel={barMode === 'received' ? 'POs received' : 'POs due'} />
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-hd wrap">
          <h2>Product breakdown</h2><span className="hd-note">By quantity ordered</span>
          <span className="hd-ctl">
            <select className="rpt-sel" value={yr} onChange={e => setYr(e.target.value)} aria-label="Year">
              <option value="">All years</option>{years.map(y => <option key={y} value={y}>{y}</option>)}
            </select>
            <select className="rpt-sel" value={mo} onChange={e => setMo(e.target.value)} aria-label="Month">
              <option value="">All months</option>{MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
            </select>
          </span>
        </div>
        <div className="rpt-pad sm">
          <Columns cats={qty} color={color} fmtTip={v => `${Math.round(v)} units`} fmtAxis={fmtQty} />
        </div>
      </section>

      <div className="rpt-row">
        <section className="card">
          <div className="card-hd"><h2>Delivery performance</h2><span className="hd-note">Closed orders</span></div>
          <div className="rpt-pad"><Perf o={s.otd} /></div>
        </section>
        <section className="card">
          <div className="card-hd"><h2>Product value</h2><span className="hd-note">All amounts in USD{yr || mo ? ' · filtered above' : ''}</span></div>
          <div className="rpt-pad sm">
            {valueTotal ? (
              <Donut segs={valueSegs} centre={fmtUsd(valueTotal)} centreLbl="total" fmt={fmtUsd}
                     tipVal={(sg, pct) => `${fmtUsd(sg.v)}  ·  ${pct}%`} />
            ) : <p className="rpt-none">No cost data available</p>}
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-hd">
          <h2>OTD trend</h2><span className="hd-note">on-time delivery rate by month</span>
          <select className="rpt-sel hd-ctl" value={trendMode} onChange={e => setTrendMode(e.target.value as 'delivery' | 'due')} aria-label="Group by">
            <option value="delivery">By delivery month</option>
            <option value="due">By due-date month</option>
          </select>
        </div>
        <div className="rpt-pad sm"><Trend points={trend} color={color} /></div>
      </section>
    </>
  )
}

export default function ReportsPage() {
  const { chevron, nlng, loaded, error } = useOrders()
  const [params, setParams] = useSearchParams()
  const client: Client = params.get('client') === 'nlng' ? 'nlng' : 'chevron'
  return (
    <TipProvider>
      <div className="page reports">
        <div className="ph">
          <div>
            <h1>Reports</h1>
            {error ? <p className="err-note">Couldn’t load orders: {error}</p> : <p>Summary analytics by client</p>}
          </div>
          <div className="seg" role="tablist" aria-label="Client">
            <button type="button" role="tab" aria-selected={client === 'chevron'} className={client === 'chevron' ? 'on' : ''} onClick={() => setParams({})}>Chevron</button>
            <button type="button" role="tab" aria-selected={client === 'nlng'} className={client === 'nlng' ? 'on' : ''} onClick={() => setParams({ client: 'nlng' })}>NLNG</button>
          </div>
        </div>
        {!loaded ? <p className="muted">Loading orders…</p>
          : <Report key={client} client={client} orders={(client === 'nlng' ? nlng : chevron) as O[]} />}
      </div>
    </TipProvider>
  )
}
