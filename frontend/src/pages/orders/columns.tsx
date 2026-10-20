import type { ReactNode } from 'react'
import {
  type ChevronOrder, type NlngOrder, type SoLine, type Stage,
  chevronStage, fmtDate, isClosedOrder, nlngStage, poPromisedDate, stockText,
} from '../../lib/orders'

/*
  Every column the old Orders table had, for both clients, in the same order.
  `show` marks the ones visible until someone picks their own set in the
  Columns menu. Each column says how to draw a cell, sort it and export it.
*/

export type Order = ChevronOrder | NlngOrder
export type Col<O> = {
  key: string
  label: string
  show?: boolean
  fixed?: boolean          // always shown, pinned to the left
  align?: 'right'
  sort: (o: O) => string | number
  cell: (o: O) => ReactNode
  csv: (o: O) => string
}

const str = (v: unknown) => (v == null ? '' : typeof v === 'object' ? (stockText(v) || JSON.stringify(v)) : String(v))

export function Pill({ s }: { s: Stage }) {
  return <span className={`pill ${s.tone}`}><i />{s.lbl}</span>
}
export function LivePill({ o }: { o: Order }) {
  const done = isClosedOrder(o)
  return <span className={`pill ${done ? 'closed' : 'live'}`}><i />{done ? 'Closed' : 'Live'}</span>
}
export function DateCell({ v }: { v: unknown }) {
  const f = fmtDate(v)
  if (!f) return <span className="null">—</span>
  return <span className="dt">{f.d}{f.t && <small>{f.t}</small>}</span>
}
function Text({ v, n = 48 }: { v: unknown; n?: number }) {
  const s = str(v).replace(/\s+/g, ' ').trim()
  if (!s) return <span className="null">—</span>
  return <span title={s.length > n ? s.slice(0, 300) : undefined}>{s.length > n ? s.slice(0, n) + '…' : s}</span>
}
function Items({ first, count }: { first: string; count: number }) {
  if (!count && !first) return <span className="null">—</span>
  return <span className="items">{first.length > 42 ? first.slice(0, 42) + '…' : first}{count > 1 && <b className="more">+{count - 1}</b>}</span>
}
function SoItems({ o }: { o: Order }) {
  const items = (o.so_number ? (o.so_line_items as SoLine[] | undefined) : undefined) || []
  if (!items.length) return <span className="null">—</span>
  const dates = [...new Set(items.map(i => i.despatch_date).filter(Boolean))] as string[]
  return (
    <span className="items">
      {(items[0].item_number || '').slice(0, 28)}{items.length > 1 && <b className="more">+{items.length - 1}</b>}
      {dates.length === 1 && <small> {fmtDate(dates[0])?.d}</small>}
      {dates.length > 1 && <small> {dates.length} dates</small>}
    </span>
  )
}
const soCsv = (o: Order) => ((o.so_line_items as SoLine[] | undefined) || [])
  .map(li => (li.item_number || '') + (li.qty ? ` x${li.qty} ${li.uom || ''}` : '') + (li.despatch_date ? ` [${li.despatch_date}]` : '')).join(' | ')

function date<O extends Order>(key: string, label: string, show = false): Col<O> {
  const g = (o: O) => (o as Record<string, unknown>)[key]
  return { key, label, show, sort: o => str(g(o)), cell: o => <DateCell v={g(o)} />, csv: o => str(g(o)) }
}
function text<O extends Order>(key: string, label: string, show = false, n = 48): Col<O> {
  const g = (o: O) => (o as Record<string, unknown>)[key]
  return { key, label, show, sort: o => str(g(o)), cell: o => <Text v={g(o)} n={n} />, csv: o => str(g(o)) }
}
const mono = (v: unknown) => v ? <span className="mono">{String(v)}</span> : <span className="null">—</span>

function amount(v: unknown, cur: string): string {
  const n = Number(v)
  if (v == null || v === '' || !isFinite(n)) return ''
  return n >= 1e6 ? `${cur}${(n / 1e6).toFixed(1)}M` : `${cur}${n.toLocaleString()}`
}

// ── Chevron ─────────────────────────────────────────────────────────────────

const chItems = (o: ChevronOrder) => [...(o.order_line_items || [])].sort((a, b) => (a.line_no || 0) - (b.line_no || 0))

export const CHEVRON_COLS: Col<ChevronOrder>[] = [
  { key: 'buyer_po_number', label: 'Chevron PO', fixed: true, sort: o => o.buyer_po_number || '',
    cell: o => <b className="mono po">{o.buyer_po_number || '—'}</b>, csv: o => o.buyer_po_number || '' },
  { key: '_live', label: 'State', show: true, sort: o => (isClosedOrder(o) ? 1 : 0), cell: o => <LivePill o={o} />,
    csv: o => (isClosedOrder(o) ? 'Closed' : 'Live') },
  { key: 'overall_status', label: 'Status', show: true, sort: o => chevronStage(o).lbl, cell: o => <Pill s={chevronStage(o)} />,
    csv: o => o.overall_status || '' },
  { key: 'po_amount', label: 'Value', show: true, align: 'right', sort: o => Number(o.po_amount) || 0,
    cell: o => amount(o.po_amount, o.po_currency === 'NGN' ? '₦' : '$') ? <span className="mono">{amount(o.po_amount, o.po_currency === 'NGN' ? '₦' : '$')}</span> : <span className="null">—</span>,
    csv: o => str(o.po_amount) },
  date('notification_received_at', 'Notified', true),
  date('order_submitted_on', 'Submitted'),
  { key: 'order_line_items', label: 'Items', show: true, sort: o => chItems(o)[0]?.description || o.extracted_description || '',
    cell: o => { const it = chItems(o); return <Items first={it[0]?.description || o.extracted_description || ''} count={it.length} /> },
    csv: o => chItems(o).map(li => `${li.line_no || ''}. ${li.description || ''}${li.quantity ? ' x' + li.quantity : ''}`).join(' | ') },
  { ...text<ChevronOrder>('req_number', 'REQ#'), cell: o => mono(o.req_number) },
  text('buyer_name', 'Buyer / requester', true, 28),
  date('required_delivery_date', 'Req. delivery'),
  { key: '_po_promised', label: 'PO promised', show: true, sort: o => poPromisedDate(o) || '',
    cell: o => <DateCell v={poPromisedDate(o)} />, csv: o => poPromisedDate(o) || '' },
  text('po_destination', 'Destination'),
  text('transportation', 'Transport'),
  text('acknowledgment_status', 'Ack status'),
  date('acknowledged_at', 'Acknowledged'),
  date('sent_to_warehouse_at', 'Sent to wh.'),
  text('warehouse_routing_raw', 'Routing note'),
  date('stock_check_completed_at', 'Stock check'),
  { ...text<ChevronOrder>('stock_check_raw', 'Stock notes'), cell: o => <Text v={stockText(o.stock_check_raw)} /> },
  { ...text<ChevronOrder>('spm_po_number', 'SPM PO'), cell: o => mono(o.spm_po_number) },
  date('spm_po_sent_at', 'SPM PO sent'),
  { ...text<ChevronOrder>('so_number', 'SO number', true), cell: o => mono(o.so_number) },
  { key: '_so_items', label: 'SO items', sort: o => ((o.so_line_items as SoLine[]) || [])[0]?.item_number || '', cell: o => <SoItems o={o} />, csv: soCsv },
  date('promised_date', 'Promised dispatch'),
  date('so_received_at', 'SO received'),
  date('so_sent_to_warehouse_at', 'SO → wh.'),
  date('flex_dispatch_ready_at', 'Dispatch ready'),
  date('dispatch_instructions_sent_at', 'Dispatch instr.'),
  date('ready_for_dispatch_at', 'To shipping co.'),
  date('dispatched_at', 'Shipping co. rcvd'),
  date('delivery_requested_at', 'Delivery request'),
  date('delivered_at', 'Delivered', true),
]

// ── NLNG ────────────────────────────────────────────────────────────────────

const nlItems = (o: NlngOrder) => [...(o.nlng_order_line_items || [])].sort((a, b) => Number(a.item_no || 0) - Number(b.item_no || 0))

export const NLNG_COLS: Col<NlngOrder>[] = [
  { key: 'po_number', label: 'NLNG PO', fixed: true, sort: o => o.po_number || '',
    cell: o => <b className="mono po">{o.po_number || '—'}</b>, csv: o => o.po_number || '' },
  { key: '_live', label: 'State', show: true, sort: o => (isClosedOrder(o) ? 1 : 0), cell: o => <LivePill o={o} />,
    csv: o => (isClosedOrder(o) ? 'Closed' : 'Live') },
  { key: 'overall_status', label: 'Status', show: true, sort: o => nlngStage(o).lbl, cell: o => <Pill s={nlngStage(o)} />,
    csv: o => o.overall_status || '' },
  { key: 'net_value', label: 'Value', show: true, align: 'right', sort: o => Number(o.net_value) || 0,
    cell: o => amount(o.net_value, (o.currency || 'USD') + ' ') ? <span className="mono">{amount(o.net_value, (o.currency || 'USD') + ' ')}</span> : <span className="null">—</span>,
    csv: o => (o.net_value != null ? `${o.net_value} ${o.currency || 'USD'}` : '') },
  date('notification_received_at', 'Notified', true),
  { key: 'nlng_order_line_items', label: 'Items', show: true, sort: o => nlItems(o)[0]?.description || '',
    cell: o => { const it = nlItems(o); return <Items first={it[0]?.description || ''} count={it.length} /> },
    csv: o => nlItems(o).map(li => `${li.item_no || ''}. ${li.description || ''}${li.quantity ? ` x${li.quantity} ${li.uom || ''}` : ''}`).join(' | ') },
  text('contact_name', 'Buyer / requester', true, 28),
  { ...text<NlngOrder>('enquiry_number', 'ENQ#'), cell: o => mono(o.enquiry_number) },
  date('required_delivery_date', 'RDD', true),
  { key: '_ack', label: 'Ack status', sort: () => '', cell: () => <span className="pill n"><i />Acknowledged</span>, csv: () => 'Acknowledged' },
  date('sent_to_warehouse_at', 'Sent to wh.'),
  text('warehouse_routing_raw', 'Routing note'),
  date('stock_check_completed_at', 'Stock check'),
  { ...text<NlngOrder>('stock_check_raw', 'Stock notes'), cell: o => <Text v={stockText(o.stock_check_raw)} /> },
  date('spm_po_sent_at', 'SPM PO sent'),
  { ...text<NlngOrder>('spm_po_number', 'SPM PO'), cell: o => mono(o.spm_po_number) },
  { ...text<NlngOrder>('so_number', 'SO number', true), cell: o => mono(o.so_number) },
  { key: '_so_items', label: 'SO items', sort: o => ((o.so_line_items as SoLine[]) || [])[0]?.item_number || '', cell: o => <SoItems o={o} />, csv: soCsv },
  date('promised_date', 'Promised dispatch'),
  date('so_received_at', 'SO received'),
  date('so_sent_to_warehouse_at', 'SO → wh.'),
  date('flex_dispatch_ready_at', 'Dispatch ready'),
  date('dispatch_instructions_sent_at', 'Dispatch instr.'),
  date('ready_for_dispatch_at', 'To shipping co.'),
  date('dispatched_at', 'Shipping co. rcvd'),
  date('delivered_at', 'Delivered', true),
]
