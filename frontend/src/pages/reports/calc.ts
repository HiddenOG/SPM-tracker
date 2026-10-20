import {
  type ChevronOrder, type NlngOrder, CLOSED_STATUSES, otdClass, otdDate,
} from '../../lib/orders'

/*
  Every figure on the Reports page, copied from web/reports.js (the live
  version - the older copies in script.js are shadowed by it). Keep in step.
*/

export type O = ChevronOrder & NlngOrder
export type LiField = 'order_line_items' | 'nlng_order_line_items'

// ── Pipeline stages (the donut) ─────────────────────────────────────────────
export const RPT_STAGES: { label: string; color: string; statuses: Set<string> | null }[] = [
  { label: 'In Transit', color: 'var(--ch1)', statuses: new Set(['dispatched', 'delivery_requested']) },
  { label: 'At Supplier', color: 'var(--ch2)', statuses: new Set(['po_sent', 'awaiting_supplier_so', 'supplier_acknowledged']) },
  { label: 'Dispatching', color: 'var(--ch3)', statuses: new Set(['dispatch_packed_awaiting_instruction', 'dispatch_instruction_sent', 'so_sent_to_warehouse', 'ready_for_dispatch']) },
  { label: 'Pending', color: 'var(--ch4)', statuses: null },
  { label: 'Delivered', color: 'var(--ch6)', statuses: CLOSED_STATUSES },
]

export function bucketCh(o: O): number {
  const s = o.overall_status || ''
  for (let i = 0; i < RPT_STAGES.length; i++) if (RPT_STAGES[i].statuses?.has(s)) return i
  return 3
}
export function bucketNl(o: O): number {
  if (o.overall_status === 'delivered' || o.delivered_at) return 4
  const s = o.overall_status || ''
  for (let i = 0; i < 3; i++) if (RPT_STAGES[i].statuses?.has(s)) return i
  return 3
}

// ── Months ──────────────────────────────────────────────────────────────────
export type Month = { y: number; m: number; lbl: string; v: number }
const short = (y: number, m: number) => new Date(y, m, 1).toLocaleString('en', { month: 'short' })

/** A year's Jan–Dec, or (no year) the last 8 months rolling. */
export function monthBuckets(orders: O[], field: string, year?: number | null): Month[] {
  const now = new Date()
  const months: Month[] = year
    ? Array.from({ length: 12 }, (_, i) => ({ y: year, m: i, lbl: short(year, i), v: 0 }))
    : Array.from({ length: 8 }, (_, k) => { const d = new Date(now.getFullYear(), now.getMonth() - (7 - k), 1); return { y: d.getFullYear(), m: d.getMonth(), lbl: short(d.getFullYear(), d.getMonth()), v: 0 } })
  for (const o of orders) {
    const raw = o[field] as string | undefined
    if (!raw) continue
    const d = new Date(raw)
    if (isNaN(d.getTime())) continue
    const hit = months.find(x => x.y === d.getFullYear() && x.m === d.getMonth())
    if (hit) hit.v++
  }
  return months
}

export function dueYears(orders: O[]): number[] {
  const ys = new Set<number>([new Date().getFullYear()])
  for (const o of orders) {
    if (!o.required_delivery_date) continue
    const d = new Date(o.required_delivery_date)
    if (!isNaN(d.getTime())) ys.add(d.getFullYear())
  }
  return [...ys].sort()
}

// ── OTD, weighted by line items ─────────────────────────────────────────────
export type Otd = { onTime: number; late: number; total: number; score: number | null; avgDays: number | null; avgLateDays: number | null; minDays: number | null }

export function otd(delivered: O[], li: LiField): Otd {
  let liOtd = 0, liLate = 0, totalDays = 0, daysN = 0, lateDays = 0, lateN = 0, minDays = Infinity
  for (const o of delivered) {
    const cls = otdClass(o)
    const n = ((o[li] as unknown[]) || []).length || 1
    const del = o.delivered_at ? new Date(o.delivered_at) : null
    const start = o.notification_received_at ? new Date(o.notification_received_at) : null
    if (del && start && !isNaN(del.getTime()) && !isNaN(start.getTime())) {
      const days = Math.round((del.getTime() - start.getTime()) / 86400000)
      if (days > 0) { totalDays += days; daysN++; if (days < minDays) minDays = days }
    }
    if (cls === 'del-otd') liOtd += n
    else if (cls === 'del-late') {
      liLate += n
      const rdd = o.required_delivery_date ? new Date(o.required_delivery_date) : null
      if (del && rdd && !isNaN(del.getTime()) && !isNaN(rdd.getTime())) { lateDays += Math.round((del.getTime() - rdd.getTime()) / 86400000); lateN++ }
    }
  }
  const total = liOtd + liLate
  return {
    onTime: liOtd, late: liLate, total,
    score: total > 0 ? Math.round((liOtd / total) * 100) : null,
    avgDays: daysN > 0 ? Math.round(totalDays / daysN) : null,
    avgLateDays: lateN > 0 ? Math.round(lateDays / lateN) : null,
    minDays: minDays < Infinity ? minDays : null,
  }
}

export type TrendPoint = { lbl: string; score: number | null; total: number }

export function otdByMonth(orders: O[], li: LiField, mode: 'delivery' | 'due'): TrendPoint[] {
  const now = new Date()
  const months = Array.from({ length: 12 }, (_, k) => {
    const d = new Date(now.getFullYear(), now.getMonth() - (11 - k), 1)
    return { y: d.getFullYear(), m: d.getMonth(), lbl: short(d.getFullYear(), d.getMonth()), liOtd: 0, liLate: 0 }
  })
  for (const o of orders) {
    if (!o.delivered_at) continue
    const cls = otdClass(o)
    if (cls !== 'del-otd' && cls !== 'del-late') continue
    let g: Date
    if (mode === 'due') { const due = otdDate(o); if (!due) continue; g = new Date(due) }
    else g = new Date(o.delivered_at)
    if (isNaN(g.getTime())) continue
    const n = ((o[li] as unknown[]) || []).length || 1
    const hit = months.find(x => x.y === g.getFullYear() && x.m === g.getMonth())
    if (hit) { if (cls === 'del-otd') hit.liOtd += n; else hit.liLate += n }
  }
  return months.map(m => { const t = m.liOtd + m.liLate; return { lbl: m.lbl, score: t > 0 ? Math.round((m.liOtd / t) * 100) : null, total: t } })
}

// ── Product categories: first match wins, specific before generic ───────────
const CATS: { name: string; keys: string[] }[] = [
  { name: 'Spiral Wound', keys: ['SPIRAL', 'SPW', 'SPWD', 'SW GASKET'] },
  { name: 'Ring Joint', keys: ['RING JOINT', 'RING-JOINT', 'RTJ', 'APIR'] },
  { name: 'Kammprofile', keys: ['KAMMPROFILE', 'KAMM', 'GROOVED', 'CORRUGATED'] },
  { name: 'Heat Exchanger', keys: ['HEAT EXCHANGER', 'H/E SEAL', 'HX SEAL'] },
  { name: 'Lens Ring', keys: ['LENS RING', 'LENS GASKET'] },
  { name: 'Soft Iron', keys: ['SOFT IRON'] },
  { name: 'Gasket / Seal', keys: ['GASKET', 'GSKT', 'SEAL'] },
  { name: 'Stud Bolts', keys: ['STUDBOLT', 'STUD BOLT', 'SSTUDBOLTS', 'BOLT:STUD'] },
  { name: 'Skillet', keys: ['SKILLET'] },
  { name: 'Valves', keys: ['REPAIRKIT', 'REPAIR KIT', 'VALVE REPAIR', 'GATE VALVE', 'GATEVALVE', 'GLOBE VALVE', 'GLOBEVALVE', 'CHECK VALVE', 'CHECKVALVE', 'BALL VALVE', 'BALLVALVE', 'BUTTERFLY', 'NOZZLE:', 'VALVE'] },
  { name: 'Filter', keys: ['FILTER'] },
  { name: 'Instruments', keys: ['TRANSMITTER', 'REGULATOR', 'IGNITOR', 'PANEL', 'SWITCH', 'SENSOR', 'GAUGE', 'LIGHT:', 'ACTUATOR'] },
  { name: 'Pipe Fitting', keys: ['RISER:', 'BELLOWS:', 'ELBOW:', 'TEE:', 'NIPPLE:', 'SPACER', 'FLANGE:', 'PIPE', 'PIPING', 'FITTING'] },
  { name: 'Hardware', keys: ['RIVET', 'SCREW', 'LINER', 'GLAND', 'THREADTAPE', 'THREAD TAPE', 'NUT:', 'WASHER', 'FASTENER', 'CLAMP', 'BRACKET', 'ASSEMBLY'] },
  { name: 'Rotating Equipment', keys: ['MOTOR', 'GEARBOX', 'COUPLING', 'SPRING', 'BUSHING', 'REDUCER', 'BEARING', 'BELT', 'COMPRESSOR', 'PUMP', 'TURBINE'] },
  { name: 'Structural', keys: ['BEAM', 'TUBE', 'SHEET', 'COIL', 'RAFTER', 'COLUMN'] },
  { name: 'Consumables', keys: ['ELECTRODE', 'FLUID', 'CAN:', 'PALLET', 'REFRACTORY', 'INSULATION', 'LUBRICANT', 'PAINT', 'CHEMICAL'] },
  { name: 'Services', keys: ['TRAINING', '%HCD', 'SERVICE CHARGE'] },
]

export function catItem(desc?: string): string {
  if (!desc) return 'Other'
  const u = desc.toUpperCase()
  for (const c of CATS) if (c.keys.some(k => u.includes(k))) return c.name
  return 'Other'
}

export type Cat = { name: string; v: number; samples: string[] }
type Li = { description?: string; quantity?: number | string; net_amount?: number | string }

function addSample(map: Record<string, string[]>, cat: string, desc?: string) {
  if (!desc) return
  const list = (map[cat] ||= [])
  if (!list.includes(desc) && list.length < 5) list.push(desc)
}

/** Quantity ordered per category (a line with no quantity counts as 1). */
export function catQuantities(orders: O[], li: LiField): Cat[] {
  const totals: Record<string, number> = {}, samples: Record<string, string[]> = {}
  for (const o of orders) for (const it of ((o[li] as Li[]) || [])) {
    const cat = catItem(it.description)
    const q = parseFloat(String(it.quantity))
    totals[cat] = (totals[cat] || 0) + (isNaN(q) ? 1 : q)
    addSample(samples, cat, it.description)
  }
  return Object.keys(totals).map(k => ({ name: k, v: totals[k], samples: samples[k] || [] })).sort((a, b) => b.v - a.v)
}

// The rate reports.js uses for value-by-category - deliberately the page's own
// fixed figure, not the dashboard's live one, so the two numbers match the old page.
export const NGN_USD_RATE = 1580

/** Order value per category in USD. NLNG lines carry amounts; Chevron splits the PO value evenly. */
export function catValues(orders: O[], li: LiField): Cat[] {
  const totals: Record<string, number> = {}, samples: Record<string, string[]> = {}
  for (const o of orders) {
    const curr = String(o.po_currency || o.currency || 'USD').toUpperCase()
    const rate = curr === 'NGN' ? 1 / NGN_USD_RATE : 1
    const items = (o[li] as Li[]) || []
    if (!items.length) continue
    if (items.some(x => parseFloat(String(x.net_amount || 0)) > 0)) {
      for (const it of items) {
        const v = parseFloat(String(it.net_amount || 0)) * rate
        if (v <= 0) continue
        const cat = catItem(it.description)
        totals[cat] = (totals[cat] || 0) + v
        addSample(samples, cat, it.description)
      }
    } else {
      const amount = parseFloat(String(o.po_amount || o.net_value || 0)) * rate
      if (!amount) continue
      for (const it of items) {
        const cat = catItem(it.description)
        totals[cat] = (totals[cat] || 0) + amount / items.length
        addSample(samples, cat, it.description)
      }
    }
  }
  return Object.keys(totals).filter(k => totals[k] > 0).map(k => ({ name: k, v: totals[k], samples: samples[k] || [] })).sort((a, b) => b.v - a.v)
}

const orderDate = (o: O) => String(o.notification_received_at || o.po_date || o.created_at || '')

export function filterByDate(orders: O[], yr: string, mo: string): O[] {
  return orders.filter(o => {
    const d = orderDate(o)
    if (!d) return !yr && !mo
    const dt = new Date(d)
    if (yr && dt.getFullYear() !== parseInt(yr, 10)) return false
    if (mo && dt.getMonth() + 1 !== parseInt(mo, 10)) return false
    return true
  })
}

export function orderYears(orders: O[]): number[] {
  const ys = new Set<number>()
  for (const o of orders) { const d = orderDate(o); if (!d) continue; const y = new Date(d).getFullYear(); if (!isNaN(y)) ys.add(y) }
  return [...ys].sort()
}

export const fmtUsd = (v: number) => (v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1000 ? `$${(v / 1000).toFixed(0)}k` : `$${Math.round(v)}`)
export const fmtQty = (v: number) => (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : String(Math.round(v)))
