import { createContext, useCallback, useContext, useState, type MouseEvent, type ReactNode } from 'react'
import type { Cat, Month, Otd, TrendPoint } from './calc'

/*
  The Reports charts, each one the SVG reports.js drew - same view boxes,
  ticks, labels and tooltips - written as React components. Colours come from
  CSS variables so both themes work.
*/

// ── Tooltip ─────────────────────────────────────────────────────────────────
type Tip = { x: number; y: number; lbl: string; val: string; items?: string[] } | null
const TipCtx = createContext<{ show: (e: MouseEvent, lbl: string, val: string, items?: string[]) => void; hide: () => void }>({ show: () => {}, hide: () => {} })

export function TipProvider({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<Tip>(null)
  const show = useCallback((e: MouseEvent, lbl: string, val: string, items?: string[]) => setTip({ x: e.clientX, y: e.clientY, lbl, val, items }), [])
  const hide = useCallback(() => setTip(null), [])
  const left = tip ? Math.min(tip.x + 12, window.innerWidth - 228) : 0
  const top = tip ? Math.max(tip.y - 28, 8) : 0
  return (
    <TipCtx.Provider value={{ show, hide }}>
      {children}
      {tip && (
        <div className="rpt-tip" style={{ left, top }} role="tooltip">
          <div className="rpt-tip-lbl">{tip.lbl}</div>
          <div className="rpt-tip-val">{tip.val}</div>
          {!!tip.items?.length && (
            <div className="rpt-tip-items">{tip.items.slice(0, 4).map((s, i) => <span key={i}>· {s.length > 44 ? s.slice(0, 43) + '…' : s}</span>)}</div>
          )}
        </div>
      )}
    </TipCtx.Provider>
  )
}
const useTip = () => useContext(TipCtx)

// ── Donut (pipeline stages, or product value by category) ──────────────────
export type Seg = { label: string; v: number; color: string; samples?: string[] }

function arc(cx: number, cy: number, R: number, ri: number, a0: number, ang: number): string {
  const p = (r: number, a: number) => `${(cx + r * Math.cos(a)).toFixed(2)} ${(cy + r * Math.sin(a)).toFixed(2)}`
  if (ang >= 2 * Math.PI - 0.001) {
    return `M ${p(R, a0)} A ${R} ${R} 0 1 1 ${p(R, a0 + Math.PI)} A ${R} ${R} 0 1 1 ${p(R, a0)} M ${p(ri, a0)} A ${ri} ${ri} 0 1 0 ${p(ri, a0 + Math.PI)} A ${ri} ${ri} 0 1 0 ${p(ri, a0)} Z`
  }
  const a1 = a0 + ang, large = ang > Math.PI ? 1 : 0
  return `M ${p(R, a0)} A ${R} ${R} 0 ${large} 1 ${p(R, a1)} L ${p(ri, a1)} A ${ri} ${ri} 0 ${large} 0 ${p(ri, a0)} Z`
}

export function Donut({ segs, centre, centreLbl, fmt, tipVal }: {
  segs: Seg[]; centre: string; centreLbl: string; fmt: (v: number) => string; tipVal: (s: Seg, pct: string) => string
}) {
  const tip = useTip()
  const live = segs.filter(s => s.v > 0)
  const total = live.reduce((s, x) => s + x.v, 0)
  if (!total) return <p className="rpt-none">No data available</p>
  const VH = Math.max(200, live.length * 19 + 24), VW = 490, cx = 100, cy = VH / 2, R = 80, ri = 50
  let a = -Math.PI / 2
  const legY0 = (VH - live.length * 19) / 2
  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label={`${centreLbl}: ${centre}`}>
      {live.map((s, i) => {
        const ang = (s.v / total) * 2 * Math.PI
        const d = arc(cx, cy, R, ri, a, ang)
        a += ang
        if (ang < 0.001) return null
        const pct = ((s.v / total) * 100).toFixed(1)
        // Keyed by position: "Other" can be a category and the lumped rest at once, as on the old page.
        return <path key={i} d={d} style={{ fill: s.color, cursor: 'pointer' }} stroke="var(--surface)" strokeWidth={1.5}
                     onMouseMove={e => tip.show(e, s.label, tipVal(s, pct), s.samples)} onMouseLeave={tip.hide} />
      })}
      <text x={cx} y={cy - 5} textAnchor="middle" className="rch-dv" fontSize={centre.length > 6 ? 13 : 20}>{centre}</text>
      <text x={cx} y={cy + 12} textAnchor="middle" className="rch-dl">{centreLbl}</text>
      {live.map((s, i) => {
        const y = legY0 + i * 19
        return (
          <g key={i}>
            <rect x={205} y={y + 1} width={10} height={10} rx={2} style={{ fill: s.color }} />
            <text x={219} y={y + 9} className="rch-ax">{s.label}</text>
            <text x={VW - 44} y={y + 9} textAnchor="end" className="rch-ax">{fmt(s.v)}</text>
            <text x={VW - 2} y={y + 9} textAnchor="end" className="rch-ax">{((s.v / total) * 100).toFixed(1)}%</text>
          </g>
        )
      })}
    </svg>
  )
}

// ── Monthly intake bars (+ the delivered line in Received mode) ─────────────
export function Bars({ months, color, tipLabel, delivered }: { months: Month[]; color: string; tipLabel: string; delivered?: Month[] | null }) {
  const tip = useTip()
  const VW = 380, VH = 158, ml = 28, mr = 6, mt = 10, mb = 24, pw = VW - ml - mr, ph = VH - mt - mb
  let maxV = Math.max(...months.map(m => m.v))
  if (delivered) maxV = Math.max(maxV, ...delivered.map(m => m.v))
  if (!maxV) maxV = 1
  const raw = maxV / 5, mag = Math.pow(10, Math.floor(Math.log10(raw || 1)))
  let step = [1, 2, 5, 10].reduce((c, n) => (n * mag >= raw && n * mag < c ? n * mag : c), Infinity)
  step = Math.max(Math.round(step), 1)
  const top = Math.ceil(maxV / step) * step || step
  const slot = pw / months.length, barW = Math.min(22, slot * 0.55)
  const yOf = (v: number) => mt + ph - (v / top) * ph
  const ticks: number[] = []
  for (let t = 0; t <= top; t += step) ticks.push(t)
  const line = (ms: Month[]) => ms.map((m, i) => `${ml + i * slot + slot / 2},${yOf(m.v)}`).join(' ')
  const dots = (ms: Month[], fill: string, label: string) => ms.map((m, i) => (
    <circle key={i} cx={ml + i * slot + slot / 2} cy={yOf(m.v)} r={3.5} style={{ fill, stroke: 'var(--surface)', strokeWidth: 1.5 }}
            onMouseMove={e => tip.show(e, m.lbl, `${m.v} ${label}`)} onMouseLeave={tip.hide} />
  ))
  return (
    <>
      <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label={`Monthly ${tipLabel}`}>
        {ticks.map(t => (
          <g key={t}>
            <line x1={ml} x2={ml + pw} y1={yOf(t)} y2={yOf(t)} strokeWidth={1} style={{ stroke: t === 0 ? 'var(--ink-3)' : 'var(--line)' }} />
            {t > 0 && <text x={ml - 4} y={yOf(t)} textAnchor="end" dominantBaseline="central" className="rch-ax">{t}</text>}
          </g>
        ))}
        {months.map((m, i) => {
          const x = ml + i * slot + (slot - barW) / 2, h = Math.max((m.v / top) * ph, 2), y = mt + ph - h
          return (
            <g key={i}>
              <rect x={x} y={y} width={barW} height={h} rx={3} style={{ fill: color, opacity: 0.85, cursor: 'pointer' }}
                    onMouseMove={e => tip.show(e, m.lbl, `${m.v} ${tipLabel}`)} onMouseLeave={tip.hide} />
              {!(months.length > 12 && i % 2) && <text x={x + barW / 2} y={mt + ph + 16} textAnchor="middle" className="rch-ax">{m.lbl}</text>}
              {(m.v === maxV || i === months.length - 1) && <text x={x + barW / 2} y={y - 5} textAnchor="middle" className="rch-tip">{m.v}</text>}
            </g>
          )
        })}
        {delivered ? (
          <>
            <polyline points={line(delivered)} fill="none" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ stroke: 'var(--ok)', pointerEvents: 'none' }} />
            {dots(delivered, 'var(--ok)', 'delivered')}
          </>
        ) : (
          <>
            <polyline points={line(months)} fill="none" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" opacity={0.55} style={{ stroke: color, pointerEvents: 'none' }} />
            {dots(months, color, tipLabel)}
          </>
        )}
      </svg>
      {delivered && (
        <div className="rpt-bars-legend">
          <span><i style={{ background: color }} />Received</span>
          <span className="ok"><i style={{ background: 'var(--ok)' }} />Delivered</span>
        </div>
      )}
    </>
  )
}

// ── Product breakdown: columns with a line over their tops ─────────────────
export function Columns({ cats, color, fmtTip, fmtAxis }: { cats: Cat[]; color: string; fmtTip: (v: number) => string; fmtAxis: (v: number) => string }) {
  const tip = useTip()
  if (!cats.length) return <p className="rpt-none">No data available</p>
  const VW = 660, VH = 190, ml = 44, mr = 6, mt = 10, mb = 62, pw = VW - ml - mr, ph = VH - mt - mb
  const maxV = cats[0].v, mag = Math.pow(10, Math.floor(Math.log10(maxV || 1)))
  const top = Math.ceil(maxV / mag) * mag || 1
  const slot = pw / cats.length, barW = Math.max(Math.min(slot * 0.62, 40), 6)
  const pts = cats.map((c, i) => { const h = Math.max((c.v / top) * ph, 2); return { cx: ml + i * slot + slot / 2, y: mt + ph - h, h, c } })
  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Product breakdown by quantity">
      {[0, 0.25, 0.5, 0.75, 1].map(f => {
        const y = mt + ph - f * ph
        return (
          <g key={f}>
            <line x1={ml} x2={ml + pw} y1={y} y2={y} strokeWidth={1} style={{ stroke: f === 0 ? 'var(--ink-3)' : 'var(--line)' }} />
            {f > 0 && <text x={ml - 4} y={y} textAnchor="end" dominantBaseline="central" className="rch-ax">{fmtAxis(top * f)}</text>}
          </g>
        )
      })}
      {pts.map((p, i) => {
        const lbl = p.c.name.length > 13 ? p.c.name.slice(0, 12) + '…' : p.c.name, ly = mt + ph + 5
        return (
          <g key={p.c.name}>
            <rect x={p.cx - barW / 2} y={p.y} width={barW} height={p.h} rx={3} style={{ fill: color, opacity: 0.85, cursor: 'pointer' }}
                  onMouseMove={e => tip.show(e, p.c.name, fmtTip(p.c.v), p.c.samples)} onMouseLeave={tip.hide} />
            {i < 4 && <text x={p.cx} y={p.y - 4} textAnchor="middle" className="rch-tip">{fmtAxis(p.c.v)}</text>}
            <text x={p.cx} y={ly} textAnchor="end" className="rch-ax" transform={`rotate(-42 ${p.cx} ${ly})`}>{lbl}</text>
          </g>
        )
      })}
      {pts.length > 1 && (
        <>
          <polyline points={pts.map(p => `${p.cx},${p.y}`).join(' ')} fill="none" strokeWidth={2.5} style={{ stroke: color, pointerEvents: 'none' }} />
          {pts.map(p => <circle key={p.c.name} cx={p.cx} cy={p.y} r={4} style={{ fill: color, pointerEvents: 'none' }} stroke="var(--surface)" strokeWidth={2} />)}
        </>
      )}
    </svg>
  )
}

// ── Delivery performance ────────────────────────────────────────────────────
export function Perf({ o }: { o: Otd }) {
  const n = o.onTime + o.late
  const on = n > 0 ? Math.round((o.onTime / n) * 100) : 0
  const late = n > 0 ? 100 - on : 0
  return (
    <div className="perf">
      {([['On time', o.onTime, on, 'ok'], ['Late', o.late, late, 'crit']] as const).map(([lbl, count, pct, tone]) => (
        <div key={lbl}>
          <div className="perf-hd"><span className={`perf-st ${tone}`}><i />{lbl}</span><span className="perf-n">{count} line items · {pct}%</span></div>
          <div className="perf-track"><i className={tone} style={{ width: `${pct}%` }} /></div>
        </div>
      ))}
      <dl className="perf-m">
        {o.avgDays != null && <div><dt>Avg days PO → delivery</dt><dd>{o.avgDays} days</dd></div>}
        {o.avgLateDays != null && <div><dt>Avg overdue (late)</dt><dd className="warn">{o.avgLateDays} days</dd></div>}
        {o.minDays != null && <div><dt>Fastest delivery</dt><dd className="ok">{o.minDays} days</dd></div>}
        {n === 0 && <div><dt className="muted">No closed orders with delivery dates on record yet</dt></div>}
      </dl>
    </div>
  )
}

// ── OTD trend ───────────────────────────────────────────────────────────────
export function Trend({ points, color }: { points: TrendPoint[]; color: string }) {
  const tip = useTip()
  if (!points.some(p => p.score !== null)) return <p className="rpt-none">No delivery data yet — OTD trend will appear once orders are delivered</p>
  // mr is 26 rather than the old 16, so the "90%" target label is not clipped.
  const VW = 700, VH = 150, ml = 30, mr = 26, mt = 12, mb = 24, pw = VW - ml - mr, ph = VH - mt - mb
  const slot = pw / points.length
  const pts = points.map((p, i) => (p.score === null ? null : { x: ml + i * slot + slot / 2, y: Math.min(mt + ph - (p.score / 100) * ph, mt + ph - 4), p }))
  const ty = mt + ph - 0.9 * ph
  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="On-time delivery rate by month">
      {[0, 25, 50, 75, 100].map(v => {
        const y = mt + ph - (v / 100) * ph
        return (
          <g key={v}>
            <line x1={ml} x2={ml + pw} y1={y} y2={y} strokeWidth={1} style={{ stroke: v === 0 ? 'var(--ink-3)' : 'var(--line)' }} />
            {v > 0 && <text x={ml - 4} y={y} textAnchor="end" dominantBaseline="central" className="rch-ax">{v}%</text>}
          </g>
        )
      })}
      <line x1={ml} x2={ml + pw} y1={ty} y2={ty} strokeWidth={1} strokeDasharray="4,3" style={{ stroke: 'var(--ok)', opacity: 0.45 }} />
      <text x={ml + pw + 2} y={ty} dominantBaseline="central" className="rch-ax">90%</text>
      {points.map((p, i) => !(points.length > 9 && i % 2) && <text key={i} x={ml + i * slot + slot / 2} y={mt + ph + 16} textAnchor="middle" className="rch-ax">{p.lbl}</text>)}
      {pts.map((p, i) => {
        const prev = pts[i - 1]
        return p && prev ? <line key={'l' + i} x1={prev.x} y1={prev.y} x2={p.x} y2={p.y} strokeWidth={2} strokeLinecap="round" style={{ stroke: color }} /> : null
      })}
      {pts.map((p, i) => p && (
        <circle key={'d' + i} cx={p.x} cy={p.y} r={4} style={{ fill: p.p.score! >= 90 ? 'var(--ok)' : p.p.score! >= 70 ? 'var(--warn)' : 'var(--crit)', stroke: 'var(--surface)', strokeWidth: 1.5, cursor: 'pointer' }}
                onMouseMove={e => tip.show(e, p.p.lbl, `${p.p.score}% OTD · ${p.p.total} line items`)} onMouseLeave={tip.hide} />
      ))}
    </svg>
  )
}
