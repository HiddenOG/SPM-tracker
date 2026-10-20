import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useOrders } from '../../lib/data'
import {
  type ChevronOrder, type NlngOrder, CLOSED_STATUSES, DISPATCH_STAGES,
  chevronClosed, nlngClosed, money, ngnPerUsd,
} from '../../lib/orders'
import './dashboard.css'

/*
  Dashboard: the headline numbers, the year's supply pipeline, the latest
  orders and what happened most recently - Chevron and NLNG together.
  The counts follow updateDashboard() in web/script.js exactly.
*/

type Row = {
  key: string; id: string; client: 'Chevron' | 'NLNG'; po: string; desc: string
  value: string; at: string; tone: 'ok' | 'warn' | 'accent'
}
type Event = { key: string; ts: Date; tone: 'ok' | 'warn' | 'accent' | 'muted'; text: (string | { mono: string })[] }

// Most recent first; one entry per date an order has. Same list as the old feed.
const EVENTS: { f: string; tone: Event['tone']; text: (o: ChevronOrder & NlngOrder, client: string, po: string) => Event['text'] }[] = [
  { f: 'delivered_at', tone: 'ok', text: (_o, _c, po) => ['Delivered — ', { mono: po }] },
  { f: 'delivery_requested_at', tone: 'ok', text: (_o, _c, po) => ['Delivery requested — ', { mono: po }] },
  { f: 'dispatched_at', tone: 'warn', text: (o, _c, po) => ['Shipping co. received — ', { mono: o.so_number || po }] },
  { f: 'ready_for_dispatch_at', tone: 'warn', text: (o, _c, po) => ['Dispatched to shipping co. — ', { mono: o.so_number || po }] },
  { f: 'dispatch_instructions_sent_at', tone: 'warn', text: (o, _c, po) => ['Dispatch instructions sent for ', { mono: o.so_number || po }] },
  { f: 'flex_dispatch_ready_at', tone: 'warn', text: (o, _c, po) => ['Dispatch ready — ', { mono: o.so_number || po }, ' packed'] },
  { f: 'so_received_at', tone: 'accent', text: (o, _c, po) => ['SO received — ', { mono: o.so_number || '?' }, ' for ', { mono: po }] },
  { f: 'spm_po_sent_at', tone: 'accent', text: o => {
      const r = o.spm_po_number || '?'
      return ['SPM PO sent — ref ', { mono: r.length > 20 ? r.slice(0, 20) + '…' : r }]
    } },
  { f: 'notification_received_at', tone: 'muted', text: (_o, c, po) => [`${c} PO `, { mono: po }, ' received'] },
]

const fmtWhen = (d: Date) =>
  d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' · ' +
  d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })

function chevronTone(o: ChevronOrder): Row['tone'] {
  return CLOSED_STATUSES.has(o.overall_status || '') ? 'ok'
    : DISPATCH_STAGES.includes(o.overall_status || '') ? 'warn' : 'accent'
}

export default function DashboardPage() {
  const { chevron, nlng, loaded, error, lastSync } = useOrders()
  const [rate, setRate] = useState<number | null>(null)
  useEffect(() => { ngnPerUsd().then(setRate) }, [])

  const k = useMemo(() => {
    const r = rate || 1327
    const chActive = chevron.filter(o => !chevronClosed(o))
    const nlActive = nlng.filter(o => !nlngClosed(o))
    const chSo = chevron.filter(o => o.so_received_at).length
    const nlSo = nlng.filter(o => o.so_received_at).length
    const usd = (v: unknown, cur?: string) => { const n = Number(v) || 0; return cur === 'NGN' ? n / r : n }
    const value = chevron.reduce((s, o) => s + usd(o.po_amount, o.po_currency), 0)
                + nlng.reduce((s, o) => s + usd(o.net_value, o.currency), 0)
    return {
      active: chActive.length + nlActive.length, chActive: chActive.length, nlActive: nlActive.length,
      closed: chevron.length - chActive.length + nlng.length - nlActive.length,
      chClosed: chevron.length - chActive.length, nlClosed: nlng.length - nlActive.length,
      so: chSo + nlSo, chSo, nlSo,
      value, rate: r,
      notified: chevron.length + nlng.length,
      spmSent: chevron.filter(o => o.spm_po_sent_at).length + nlng.filter(o => o.spm_po_sent_at).length,
    }
  }, [chevron, nlng, rate])

  const recent = useMemo<Row[]>(() => {
    const rows: Row[] = [
      ...chevron.map(o => ({
        key: 'c' + o.id, id: String(o.id), client: 'Chevron' as const, po: o.buyer_po_number || '—',
        desc: (o.order_line_items?.[0]?.description || o.extracted_description || '').slice(0, 64) || '—',
        value: money(o.po_amount, o.po_currency), at: o.notification_received_at || '', tone: chevronTone(o),
      })),
      ...nlng.map(o => ({
        key: 'n' + o.id, id: String(o.id), client: 'NLNG' as const, po: o.po_number || '—',
        desc: (o.nlng_order_line_items?.[0]?.description || '').slice(0, 64) || '—',
        value: money(o.net_value, o.currency), at: o.notification_received_at || '',
        tone: (nlngClosed(o) ? 'ok' : o.flex_dispatch_ready_at ? 'warn' : 'accent') as Row['tone'],
      })),
    ]
    return rows.sort((a, b) => (b.at > a.at ? 1 : b.at < a.at ? -1 : 0)).slice(0, 6)
  }, [chevron, nlng])

  const events = useMemo<Event[]>(() => {
    const out: Event[] = []
    const walk = (o: ChevronOrder & NlngOrder, client: string, po: string, key: string) => {
      for (const d of EVENTS) {
        const v = o[d.f] as string | undefined
        if (v) out.push({ key: key + d.f, ts: new Date(v), tone: d.tone, text: d.text(o, client, po) })
      }
    }
    chevron.forEach(o => walk(o as ChevronOrder & NlngOrder, 'Chevron', o.buyer_po_number || '?', 'c' + o.id))
    nlng.forEach(o => walk(o as ChevronOrder & NlngOrder, 'NLNG', o.po_number || '?', 'n' + o.id))
    return out.sort((a, b) => b.ts.getTime() - a.ts.getTime()).slice(0, 8)
  }, [chevron, nlng])

  const sync = lastSync ? lastSync.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '…'
  const n = (v: number | string) => loaded ? v : <span className="skel" />

  const value = k.value >= 1e6 ? `$${(k.value / 1e6).toFixed(1)}M` : `$${Math.round(k.value).toLocaleString()}`
  const stages = [
    { label: 'Notified', v: k.notified, tone: 'n' },
    { label: 'SPM PO sent', v: k.spmSent, tone: 'r' },
    { label: "SO ack'd", v: k.so, tone: 'r2' },
    { label: 'Live', v: k.active, tone: 'w' },
    { label: 'Closed', v: k.closed, tone: 'g' },
  ]

  return (
    <div className="page dash">
      <div className="ph">
        <div>
          <h1>Dashboard</h1>
          {error
            ? <p className="err-note">Couldn’t load orders: {error}</p>
            : <p>{loaded ? `${k.active} active orders · Chevron + NLNG · last sync ${sync}` : 'Loading orders…'}</p>}
        </div>
      </div>

      <div className="kpis">
        <div className="kpi card">
          <span className="kpi-lbl">Active orders</span>
          <b className="kpi-val">{n(k.active)}</b>
          <span className="kpi-note">{k.chActive} Chevron · {k.nlActive} NLNG</span>
        </div>
        <div className="kpi card">
          <span className="kpi-lbl">Total value</span>
          <b className="kpi-val mono">{n(value)}</b>
          <span className="kpi-note">Chevron + NLNG · ₦ at $1 = ₦{Math.round(k.rate).toLocaleString()}</span>
        </div>
        <div className="kpi card">
          <span className="kpi-lbl">Delivered</span>
          <b className="kpi-val">{n(k.closed)}</b>
          <span className="kpi-note">{k.chClosed} Chevron · {k.nlClosed} NLNG</span>
        </div>
        <div className="kpi card">
          <span className="kpi-lbl">SOs from Flexitallic</span>
          <b className="kpi-val">{n(k.so)}</b>
          <span className="kpi-note">{k.chSo} Chevron · {k.nlSo} NLNG</span>
        </div>
      </div>

      <section className="pipe-wrap" aria-label="2026 supply pipeline">
        <div className="sec-lbl">2026 supply pipeline</div>
        <ol className="pipe">
          {stages.map(s => (
            <li key={s.label} className={`stage ${s.tone}`}>
              <b className="mono">{n(s.v)}</b>
              <span>{s.label}</span>
              <i className="fill" style={{ width: k.notified ? `${(s.v / k.notified) * 100}%` : 0 }} aria-hidden="true" />
            </li>
          ))}
        </ol>
      </section>

      <div className="dash-grid">
        <section className="card">
          <div className="card-hd"><h2>Recent orders</h2><Link className="card-act" to="/orders">View all →</Link></div>
          {loaded && !recent.length && <p className="card-empty">No orders yet</p>}
          <ul className="recent">
            {recent.map(r => (
              <li key={r.key}>
                <Link to={`/orders?${r.client === 'NLNG' ? 'client=nlng&' : ''}id=${encodeURIComponent(r.id)}`}>
                  <i className={`dot ${r.tone}`} aria-hidden="true" />
                  <span className="r-main">
                    <span className="r-top"><b className="mono">{r.po}</b><span className={`tag${r.client === 'NLNG' ? ' nlng' : ''}`}>{r.client}</span></span>
                    <span className="r-desc">{r.desc}</span>
                  </span>
                  <span className="r-val mono">{r.value}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>

        <section className="card">
          <div className="card-hd"><h2>Activity</h2></div>
          {loaded && !events.length && <p className="card-empty">No activity yet</p>}
          <ol className="feed">
            {events.map(e => (
              <li key={e.key}>
                <i className={`dot ${e.tone}`} aria-hidden="true" />
                <div>
                  <p>{e.text.map((t, i) => typeof t === 'string' ? t : <span key={i} className="mono ref">{t.mono}</span>)}</p>
                  <time>{fmtWhen(e.ts)}</time>
                </div>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </div>
  )
}
