import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useOrders } from '../lib/data'
import { otdClass, otdDaysLeft } from '../lib/orders'
import { currentUser } from '../lib/session'

/*
  The alerts bell: the same sections as the old app's - mentions, new POs,
  delivery requests, deadlines that are close or gone, and email problems.
  Items open the classic page that deals with them until those pages move.
*/

type Item = { key: string; title: string; sub: string; tone: 'info' | 'warn' | 'crit'; href?: string; to?: string }

// /api/alerts ids are "chev-<order id>" or "nlng-<order id>".
function orderLink(alertId: string, tab?: string): string {
  const nl = alertId.startsWith('nlng-')
  const id = alertId.replace(/^(chev|nlng)-/, '')
  return `/orders?${nl ? 'client=nlng&' : ''}id=${encodeURIComponent(id)}${tab ? `&tab=${tab}` : ''}`
}
type Section = { label: string; items: Item[] }

export default function Bell() {
  const { chevron, nlng, alerts, mentions, markMentionsRead } = useOrders()
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  const sections = useMemo<Section[]>(() => {
    const role = currentUser()?.role || 'you'
    const crit: Item[] = [], risk: Item[] = []
    const add = (po: string | undefined, buyer: string | undefined, o: Parameters<typeof otdClass>[0], k: string) => {
      const c = otdClass(o), dl = otdDaysLeft(o)
      if (c === 'critical' || c === 'overdue') {
        const sub = dl === null ? 'Past deadline' : dl < 0 ? `Overdue by ${-dl} days` : `${dl} days left — critical`
        crit.push({ key: k, title: po || '—', sub: sub + (buyer ? ` · ${buyer}` : ''), tone: 'crit', to: buyer === 'NLNG' ? '/delays?client=nlng' : '/delays' })
      } else if (c === 'at-risk') {
        risk.push({ key: k, title: po || '—', sub: `${dl} days remaining` + (buyer ? ` · ${buyer}` : ''), tone: 'warn', to: buyer === 'NLNG' ? '/delays?client=nlng' : '/delays' })
      }
    }
    chevron.forEach(o => add(o.buyer_po_number, o.buyer_name, o, 'c' + o.id))
    nlng.forEach(o => add(o.po_number, 'NLNG', o, 'n' + o.id))

    const out: Section[] = [
      { label: 'Mentions', items: mentions.map((m, i) => ({
        key: 'm' + (m.id || i), title: `@${role} · ${m.author_name || ''}`,
        sub: (m.body || '').slice(0, 60) + ((m.body || '').length > 60 ? '…' : ''), tone: 'info',
        to: m.order_id ? orderLink('chev-' + m.order_id, 'notes') : m.nlng_order_id ? orderLink('nlng-' + m.nlng_order_id, 'notes') : '/orders' })) },
      { label: 'New POs', items: (alerts?.new_pos || []).map(a => ({
        key: 'p' + a.id, title: a.po || '—', sub: `${a.buyer || ''} · just arrived`, tone: 'info', to: orderLink(a.id) })) },
      { label: 'Delivery requests', items: (alerts?.delivery_requests || []).map(a => ({
        key: 'd' + a.id, title: a.po || '—', sub: 'Delivery requested', tone: 'warn', to: orderLink(a.id) })) },
      { label: 'Delivery deadlines', items: [...crit, ...risk] },
      { label: 'Email issues', items: (alerts?.suspicious_emails || []).map((e, i) => ({
        key: 'e' + i, title: e.subject || '(no subject)', sub: `${e.sender || ''} · ${e.result || 'unknown'}`, tone: 'warn' })) },
    ]
    return out.filter(s => s.items.length)
  }, [chevron, nlng, alerts, mentions])

  const total = sections.reduce((n, s) => n + s.items.length, 0)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); window.removeEventListener('keydown', onKey) }
  }, [open])

  const toggle = () => {
    setOpen(o => {
      if (!o && mentions.length) markMentionsRead()
      return !o
    })
  }

  return (
    <div className="bell" ref={box}>
      <button className="bar-btn" type="button" aria-label={`Alerts${total ? ` (${total})` : ''}`} aria-expanded={open} onClick={toggle}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {total > 0 && <span className="pip">{total > 9 ? '9+' : total}</span>}
      </button>
      {open && (
        <div className="bell-panel" role="dialog" aria-label="Alerts">
          <div className="bell-hd">Alerts{total > 0 && <span>{total}</span>}</div>
          <div className="bell-body">
            {sections.length === 0 && <p className="bell-empty">No alerts — all orders on track.</p>}
            {sections.map(s => (
              <div key={s.label} className="bell-sec">
                <div className="bell-lbl">{s.label}</div>
                {s.items.map(it => {
                  const inner = <><b>{it.title}</b><small>{it.sub}</small></>
                  return it.to
                    ? <Link key={it.key} className={`bell-it ${it.tone}`} to={it.to} onClick={() => setOpen(false)}>{inner}</Link>
                    : it.href
                      ? <a key={it.key} className={`bell-it ${it.tone}`} href={it.href}>{inner}</a>
                      : <div key={it.key} className={`bell-it ${it.tone}`}>{inner}</div>
                })}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
