/*
  Order rules shared by every page. Each one is copied from web/script.js so
  the new pages count exactly what the old ones count; when a rule changes
  there, change it here too.
*/

export type LineItem = {
  line_no?: number; description?: string; quantity?: number
  required_delivery_date?: string; promised_date?: string
}
export type NlngLineItem = { item_no?: string; description?: string; quantity?: number; uom?: string; net_amount?: number; delivery_date?: string }

export type ChevronOrder = {
  id: string
  buyer_po_number?: string
  po_amount?: number | string
  po_currency?: string
  buyer_name?: string
  extracted_description?: string
  notification_received_at?: string
  required_delivery_date?: string
  spm_po_number?: string
  spm_po_sent_at?: string
  so_number?: string
  so_received_at?: string
  flex_dispatch_ready_at?: string
  dispatch_instructions_sent_at?: string
  ready_for_dispatch_at?: string
  dispatched_at?: string
  delivery_requested_at?: string
  delivered_at?: string
  overall_status?: string
  order_line_items?: LineItem[]
  [k: string]: unknown
}

export type NlngOrder = {
  id: string
  po_number?: string
  net_value?: number | string
  currency?: string
  notification_received_at?: string
  required_delivery_date?: string
  spm_po_number?: string
  spm_po_sent_at?: string
  so_number?: string
  so_received_at?: string
  flex_dispatch_ready_at?: string
  dispatch_instructions_sent_at?: string
  ready_for_dispatch_at?: string
  dispatched_at?: string
  delivered_at?: string
  overall_status?: string
  nlng_order_line_items?: NlngLineItem[]
  [k: string]: unknown
}

export type Tone = 'n' | 'r' | 'w' | 'b' | 'ok' | 'crit'
export type Stage = { lbl: string; tone: Tone }

export const STAGE_MAP: Record<string, Stage> = {
  new: { lbl: 'New', tone: 'n' },
  pending_acknowledgment: { lbl: 'Pending ack', tone: 'n' },
  acknowledged: { lbl: 'Acknowledged', tone: 'n' },
  awaiting_warehouse_stock_check: { lbl: 'Stock check', tone: 'n' },
  stock_check_needs_review: { lbl: 'Review needed', tone: 'w' },
  stock_check_complete: { lbl: 'Stock OK', tone: 'n' },
  pricing: { lbl: 'Pricing', tone: 'n' },
  po_sent: { lbl: 'SPM PO sent', tone: 'r' },
  awaiting_supplier_so: { lbl: 'Awaiting SO', tone: 'r' },
  supplier_acknowledged: { lbl: 'SO received', tone: 'r' },
  dispatch_packed_awaiting_instruction: { lbl: 'Packed – awaiting instr.', tone: 'w' },
  dispatch_instruction_sent: { lbl: 'Instr. sent', tone: 'w' },
  so_sent_to_warehouse: { lbl: 'SO to warehouse', tone: 'b' },
  ready_for_dispatch: { lbl: 'Collection arranged', tone: 'b' },
  dispatched: { lbl: 'With shipping co.', tone: 'b' },
  delivery_requested: { lbl: 'Delivery requested', tone: 'b' },
  delivered: { lbl: 'Delivered', tone: 'ok' },
  waybill_received: { lbl: 'Waybill received', tone: 'ok' },
  invoiced: { lbl: 'Invoiced', tone: 'ok' },
  paid: { lbl: 'Paid', tone: 'ok' },
  closed: { lbl: 'Closed', tone: 'ok' },
  cancelled: { lbl: 'Cancelled', tone: 'crit' },
}

export const NLNG_STAGE_MAP: Record<string, Stage> = {
  notification_received: { lbl: 'Notified', tone: 'n' },
  awaiting_warehouse_stock_check: { lbl: 'Sent to warehouse', tone: 'n' },
  stock_check_complete: { lbl: 'Stock check done', tone: 'n' },
  po_sent: { lbl: 'SPM PO sent', tone: 'r' },
  awaiting_supplier_so: { lbl: 'Awaiting SO', tone: 'r' },
  supplier_acknowledged: { lbl: 'SO received', tone: 'r' },
  so_sent_to_warehouse: { lbl: 'SO to warehouse', tone: 'b' },
  dispatch_packed_awaiting_instruction: { lbl: 'Packed – awaiting instr.', tone: 'w' },
  dispatch_instruction_sent: { lbl: 'Instr. sent', tone: 'w' },
  ready_for_dispatch: { lbl: 'With shipping co.', tone: 'b' },
  dispatched: { lbl: 'Shipped', tone: 'b' },
  delivered: { lbl: 'Delivered', tone: 'ok' },
}

/** The status filter chips, in the old app's order. */
export const CHIP_STAGES: { key: string; label: string }[] = [
  { key: 'all', label: 'All' }, { key: 'live', label: 'Live' }, { key: 'closed', label: 'Closed' },
  { key: 'awaiting_warehouse_stock_check', label: 'Stock check' },
  { key: 'stock_check_needs_review', label: 'Review needed' },
  { key: 'stock_check_complete', label: 'Stock OK' },
  { key: 'po_sent', label: 'SPM PO sent' },
  { key: 'awaiting_supplier_so', label: 'Awaiting SO' },
  { key: 'supplier_acknowledged', label: 'SO received' },
  { key: 'dispatch_packed_awaiting_instruction', label: 'Packed – awaiting instr.' },
  { key: 'ready_for_dispatch', label: 'Collection arranged' },
  { key: 'dispatched', label: 'With shipping co.' },
  { key: 'delivery_requested', label: 'Delivery requested' },
  { key: 'delivered', label: 'Delivered' },
  { key: 'cancelled', label: 'Cancelled' },
]

export const NLNG_CHIP_STAGES: { key: string; label: string }[] = [
  { key: 'all', label: 'All' }, { key: 'live', label: 'Live' }, { key: 'closed', label: 'Closed' },
  { key: 'awaiting_warehouse_stock_check', label: 'Stock check' },
  { key: 'stock_check_complete', label: 'Stock OK' },
  { key: 'po_sent', label: 'SPM PO sent' },
  { key: 'awaiting_supplier_so', label: 'Awaiting SO' },
  { key: 'supplier_acknowledged', label: 'SO received' },
  { key: 'so_sent_to_warehouse', label: 'SO → Warehouse' },
  { key: 'dispatch_packed_awaiting_instruction', label: 'Packed – awaiting instr.' },
  { key: 'dispatch_instruction_sent', label: 'Instr. sent' },
  { key: 'ready_for_dispatch', label: 'With shipping co.' },
  { key: 'dispatched', label: 'Shipped' },
  { key: 'delivered', label: 'Delivered' },
]

// ── Warehouse stock replies ─────────────────────────────────────────────────

/** Drop the quoted history from an email reply, keeping only the new lines. */
export function stripEmailThread(text: string): string {
  const lines = text.split(/\r?\n/)
  let cut = lines.length
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i].trim()
    if (/^On .{5,100}wrote:\s*$/i.test(ln) || ln.startsWith('>')) { cut = i; break }
  }
  return lines.slice(0, cut).join('\n').trim()
}

/** The readable part of stock_check_raw, which may be text or the parser's JSON. */
export function stockText(v: unknown): string | null {
  if (v == null || v === '') return null
  const fromObj = (o: Record<string, unknown>): string | null => {
    if (!o.needs_human_review && o.confidence === 'high' && o.summary) return String(o.summary).trim()
    if (o.raw_body) return stripEmailThread(String(o.raw_body).trim())
    if (o.summary) {
      const s = String(o.summary).trim().replace(/^Interpretation deferred:\s*/i, '')
      if (/^Error code:|^'type':|^\{/.test(s)) return 'Awaiting re-check'
      return s || null
    }
    return null
  }
  if (typeof v === 'object') return fromObj(v as Record<string, unknown>)
  const s = String(v).trim()
  if (s.startsWith('{')) { try { return fromObj(JSON.parse(s)) || s } catch { /* plain text */ } }
  return s
}

function partialStock(o: ChevronOrder): boolean {
  const lo = (stockText(o.stock_check_raw) || '').toLowerCase()
  if (!lo) return false
  const hasIn = lo.includes('in stock') || lo.includes('rfd') || lo.includes('in-stock')
  const hasOut = lo.includes('not in stock') || lo.includes('not instock') || lo.includes('out of stock') || lo.includes('no stock')
  return hasIn && hasOut
}

function notInStock(o: ChevronOrder): boolean {
  if (partialStock(o)) return false
  const lo = (stockText(o.stock_check_raw) || '').toLowerCase()
  return !!lo && ['not in stock', 'not instock', 'out of stock', 'no stock', 'unavailable', 'not available'].some(w => lo.includes(w))
}

// The stock check's answer, while it still matters (before the SPM PO goes out).
function stockStage(o: ChevronOrder | NlngOrder): Stage | null {
  if (o.spm_po_sent_at) return null
  if (notInStock(o as ChevronOrder)) return { lbl: 'Not in stock', tone: 'crit' }
  if (partialStock(o as ChevronOrder)) return { lbl: 'Partial stock', tone: 'w' }
  if (o.overall_status === 'stock_check_needs_review') {
    const snip = stockText(o.stock_check_raw)
    return snip ? { lbl: 'Review: ' + snip.replace(/[\r\n]+/g, ' ').slice(0, 28) + '…', tone: 'w' } : { lbl: 'Needs review', tone: 'w' }
  }
  return null
}

/** A Chevron order's status pill, with the stock check's answer while it still matters. */
export function chevronStage(o: ChevronOrder): Stage {
  return stockStage(o) || STAGE_MAP[o.overall_status || ''] || { lbl: o.overall_status || '—', tone: 'n' }
}

/** NLNG's stage as the Delays page shows it - stock answer first, as the old page did. */
export function nlngOtdStage(o: NlngOrder): Stage {
  return stockStage(o) || nlngStage(o)
}

export function nlngStage(o: NlngOrder): Stage {
  return NLNG_STAGE_MAP[o.overall_status || ''] || { lbl: o.overall_status || '—', tone: 'n' }
}

/** Live/closed for the State column: closed by status, or by a delivery date. */
export const isClosedOrder = (o: { overall_status?: string; delivered_at?: string }) =>
  CLOSED_STATUSES.has(o.overall_status || '') || !!o.delivered_at

export const CLOSED_STATUSES = new Set(['delivered', 'waybill_received', 'invoiced', 'paid', 'closed', 'cancelled'])
export const DISPATCH_STAGES = ['dispatch_packed_awaiting_instruction', 'dispatch_instruction_sent', 'so_sent_to_warehouse', 'ready_for_dispatch', 'dispatched']

// The dashboard's own tests for "closed" - Chevron by status, NLNG by status or delivery date.
export const chevronClosed = (o: ChevronOrder) => CLOSED_STATUSES.has(o.overall_status || '')
export const nlngClosed = (o: NlngOrder) => o.overall_status === 'delivered' || !!o.delivered_at

// ── Delivery deadlines (OTD) ────────────────────────────────────────────────

/** Any date or datetime as local midnight, so "2026-07-14" and "2026-07-14T09:00Z" agree. */
export function dateMidnight(s?: string | null): Date | null {
  if (!s) return null
  const p = String(s).slice(0, 10).split('-')
  return new Date(+p[0], +p[1] - 1, +p[2])
}

/** Latest promised date across Chevron line items: an order is complete when its last item is. */
export function poPromisedDate(o: ChevronOrder | NlngOrder): string | null {
  const items = ((o as ChevronOrder).order_line_items || []).filter(li => li.promised_date)
  if (!items.length) return null
  return items.reduce((max, li) => (li.promised_date! > max ? li.promised_date! : max), items[0].promised_date!)
}

export function otdDate(o: ChevronOrder | NlngOrder): string | null {
  return poPromisedDate(o) || o.required_delivery_date || null
}

export function otdDaysLeft(o: ChevronOrder | NlngOrder): number | null {
  const d = otdDate(o)
  if (!d || o.delivered_at) return null
  const now = new Date(); now.setHours(0, 0, 0, 0)
  return Math.round((dateMidnight(d)!.getTime() - now.getTime()) / 86400000)
}

export type OtdClass = 'del-otd' | 'del-late' | 'no-date' | 'overdue' | 'critical' | 'at-risk' | 'on-track'

export function otdClass(o: ChevronOrder | NlngOrder): OtdClass {
  if (o.delivered_at) {
    const d = otdDate(o)
    if (!d) return 'del-otd'
    return dateMidnight(o.delivered_at)! <= dateMidnight(d)! ? 'del-otd' : 'del-late'
  }
  const dl = otdDaysLeft(o)
  if (dl === null) return 'no-date'
  if (dl < 0) return 'overdue'
  if (dl < 7) return 'critical'
  if (dl < 30) return 'at-risk'
  return 'on-track'
}

// ── Money ───────────────────────────────────────────────────────────────────

let rate: { v: number; at: number } | null = null

/** Naira per dollar, refreshed hourly; the fallback matches the old app's. */
export async function ngnPerUsd(): Promise<number> {
  if (rate && Date.now() - rate.at < 3600000) return rate.v
  try {
    const d = await (await fetch('https://open.er-api.com/v6/latest/USD')).json()
    if (d?.rates?.NGN) rate = { v: d.rates.NGN, at: Date.now() }
  } catch { /* offline: fall back below */ }
  return rate?.v || 1327
}

export function money(v: unknown, currency?: string): string {
  const n = Number(v)
  if (!v || !isFinite(n)) return '—'
  return (currency === 'NGN' ? '₦' : '$') + n.toLocaleString()
}

// ── Dates ───────────────────────────────────────────────────────────────────

/** "19 Oct 26" and, when the value carries a time, "16:17" beside it. */
export function fmtDate(v: unknown): { d: string; t?: string } | null {
  if (!v) return null
  const s = String(v)
  const d = new Date(s)
  if (isNaN(d.getTime())) return { d: s }
  const day = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit' })
  const hasTime = s.includes('T') || (s.includes(' ') && s.length > 11)
  return hasTime ? { d: day, t: d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) } : { d: day }
}

/** A length of time the way the old app wrote it: "3mo 4d", "5d 2h", "4h 10m", "12m". */
export function fmtDur(ms: number): string {
  if (!ms || isNaN(ms) || ms < 0) return '—'
  let m = Math.floor(ms / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24)
  const mo = Math.floor(d / 30)
  m %= 60; h %= 24; d %= 30
  if (mo >= 2) return `${mo}mo ${d}d`
  if (d >= 1) return (mo ? `${mo}mo ` : '') + `${d}d` + (h ? ` ${h}h` : '')
  if (h >= 1) return `${h}h ${m}m`
  return `${m}m`
}

/** The Received filter's choices: rolling windows, then each month that has orders, newest first. */
export function receivedPeriods(orders: { notification_received_at?: string }[]): { key: string; label: string }[] {
  const seen = new Map<string, string>()
  for (const o of orders) {
    if (!o.notification_received_at) continue
    const d = new Date(o.notification_received_at)
    seen.set(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`,
      d.toLocaleDateString('en-GB', { month: 'short', year: '2-digit' }))
  }
  return [
    { key: 'all', label: 'Any time' }, { key: '24h', label: 'Last 24 hours' }, { key: 'week', label: 'Last week' },
    { key: '2wk', label: 'Last 2 weeks' }, { key: 'month', label: 'Last month' },
    ...[...seen.keys()].sort().reverse().map(k => ({ key: k, label: seen.get(k)! })),
  ]
}

export function inReceivedPeriod(o: { notification_received_at?: string }, key: string): boolean {
  if (key === 'all') return true
  if (!o.notification_received_at) return false
  const d = new Date(o.notification_received_at), age = Date.now() - d.getTime()
  if (key === '24h') return age <= 86400000
  if (key === 'week') return age <= 7 * 86400000
  if (key === '2wk') return age <= 14 * 86400000
  if (key === 'month') return age <= 30 * 86400000
  return d.getFullYear() === +key.slice(0, 4) && d.getMonth() === +key.slice(5, 7) - 1
}

/** Today as YYYY-MM-DD in the local (Lagos) date - toISOString() gives UTC, a day behind just after midnight. */
export function todayLocal(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export type SoLine = { so_number?: string; line_no?: number; item_number?: string; despatch_date?: string; qty?: number; uom?: string; unit_price?: number; extended_price?: number }
