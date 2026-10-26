import ConfirmBox from '../../lib/ConfirmBox'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, getJson, send, streamEvents } from '../../lib/api'
import {
  type ChevronOrder, type NlngOrder, type SoLine,
  chevronStage, fmtDate, isClosedOrder, money, nlngStage, otdClass, otdDaysLeft, poPromisedDate, stockText,
} from '../../lib/orders'
import { currentUser } from '../../lib/session'
import { Pill } from './columns'

/*
  One order, opened from the table. Three tabs:
    Overview - the facts, every stage's date, line items, SO items, notes from
               the warehouse;
    Notes    - the team's notes on this order, with @mentions;
    Story    - its email thread from the saved copy (no Gmail access), and the
               AI story and chat on request.
*/

type O = ChevronOrder & NlngOrder
type Client = 'chevron' | 'nlng'

const X = <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
const DOC = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6" /></svg>
const SEND = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z" /></svg>

const when = (v?: string | null) => { const f = fmtDate(v); return f ? f.d + (f.t ? ' · ' + f.t : '') : '' }

// ── Overview ────────────────────────────────────────────────────────────────

const CHEVRON_STEPS: [string, string][] = [
  ['notification_received_at', 'PO received'], ['order_submitted_on', 'Submitted'],
  ['sent_to_warehouse_at', 'Sent to warehouse'], ['stock_check_completed_at', 'Stock checked'],
  ['spm_po_sent_at', 'SPM PO to Flexitallic'], ['so_received_at', 'SO received'],
  ['so_sent_to_warehouse_at', 'SO to warehouse'], ['flex_dispatch_ready_at', 'Packed'],
  ['dispatch_instructions_sent_at', 'Dispatch instructions sent'], ['ready_for_dispatch_at', 'Collection arranged'],
  ['dispatched_at', 'With shipping co.'], ['delivery_requested_at', 'Delivery requested'], ['delivered_at', 'Delivered'],
]
const NLNG_STEPS: [string, string][] = [
  ['notification_received_at', 'PO received'], ['sent_to_warehouse_at', 'Sent to warehouse'],
  ['stock_check_completed_at', 'Stock checked'], ['spm_po_sent_at', 'SPM PO to Flexitallic'],
  ['so_received_at', 'SO received'], ['so_sent_to_warehouse_at', 'SO to warehouse'],
  ['flex_dispatch_ready_at', 'Packed'], ['dispatch_instructions_sent_at', 'Dispatch instructions sent'],
  ['ready_for_dispatch_at', 'With shipping co.'], ['dispatched_at', 'Shipped'], ['delivered_at', 'Delivered'],
]

const OTD_TEXT: Record<string, string> = {
  'on-track': 'On track', 'at-risk': 'At risk', overdue: 'Overdue', critical: 'Critical',
  'del-otd': 'Delivered on time', 'del-late': 'Delivered late', 'no-date': 'No delivery date',
}

function Editable({ label, value, placeholder, onSave }: { label: string; value?: string; placeholder: string; onSave: (v: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => { if (!editing) setDraft(value || '') }, [value, editing])
  const save = async () => {
    setBusy(true); setErr('')
    try { await onSave(draft.trim()); setEditing(false) } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  if (!editing) {
    return (
      <button type="button" className="fact-edit" onClick={() => setEditing(true)} title={`Edit ${label}`}>
        {value ? <span className="mono">{value}</span> : <span className="null">Add {label}</span>}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z" /></svg>
      </button>
    )
  }
  return (
    <span className="fact-form">
      <input autoFocus value={draft} placeholder={placeholder} onChange={e => setDraft(e.target.value)} disabled={busy}
             onKeyDown={e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEditing(false) }} />
      <button type="button" className="btn sm" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
      <button type="button" className="link" onClick={() => setEditing(false)} disabled={busy}>Cancel</button>
      {err && <small className="err-text">{err}</small>}
    </span>
  )
}

function Overview({ client, o, onSaved }: { client: Client; o: O; onSaved: () => void }) {
  const steps = client === 'nlng' ? NLNG_STEPS : CHEVRON_STEPS
  const doneIdx = steps.reduce((last, [k], i) => (o[k] ? i : last), -1)
  const cls = otdClass(o), dl = otdDaysLeft(o)
  const soItems = (o.so_number ? (o.so_line_items as SoLine[] | undefined) : undefined) || []
  const stock = stockText(o.stock_check_raw)
  const routing = String(o.warehouse_routing_raw || '').trim()

  const saveRef = async (v: string) => {
    if (client === 'nlng') await send('PATCH', `/api/nlng_orders/${o.id}/enquiry_number`, { enquiry_number: v || null })
    else await send('PATCH', `/api/orders/${o.id}/req_number`, { req_number: v || null })
    onSaved()
  }

  const facts: [string, ReactNode][] = client === 'nlng' ? [
    ['Value', o.net_value != null ? <span className="mono">{(o.currency || 'USD') + ' ' + Number(o.net_value).toLocaleString()}</span> : '—'],
    ['Buyer', String(o.contact_name || '—')],
    ['ENQ#', <Editable label="ENQ#" value={o.enquiry_number as string} placeholder="e.g. ENQ-12345" onSave={saveRef} />],
    ['Required by', when(o.required_delivery_date) || '—'],
    ['Delivery terms', String(o.delivery_terms || '—')],
    ['Deliver to', String(o.delivery_address || '—')],
    ['SPM PO', o.spm_po_number ? <span className="mono">{o.spm_po_number}</span> : '—'],
    ['SO number', o.so_number ? <span className="mono">{o.so_number}</span> : '—'],
    ['Promised dispatch', when(o.promised_date as string) || '—'],
  ] : [
    ['Value', money(o.po_amount, o.po_currency)],
    ['Buyer', o.buyer_name || '—'],
    ['REQ#', <Editable label="REQ#" value={o.req_number as string} placeholder="e.g. REQ0612726" onSave={saveRef} />],
    ['Required by', when(o.required_delivery_date) || '—'],
    ['PO promised', when(poPromisedDate(o)) || '—'],
    ['Destination', String(o.po_destination || '—')],
    ['Transport', String(o.transportation || '—')],
    ['SPM PO', o.spm_po_number ? <span className="mono">{o.spm_po_number}</span> : '—'],
    ['SO number', o.so_number ? <span className="mono">{o.so_number}</span> : '—'],
  ]

  return (
    <div className="ov">
      <div className={`otd ${cls}`}>
        <b>{OTD_TEXT[cls] || cls}</b>
        {dl !== null && <span>{dl < 0 ? `${-dl} days past the delivery date` : `${dl} days to the delivery date`}</span>}
      </div>

      <dl className="facts">
        {facts.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>

      <section>
        <h3>Progress</h3>
        <ol className="steps">
          {steps.map(([k, label], i) => {
            const v = o[k] as string | undefined
            const state = v ? 'done' : i === doneIdx + 1 && !isClosedOrder(o) ? 'next' : 'todo'
            return (
              <li key={k} className={state}>
                <i aria-hidden="true" />
                <span>{label}</span>
                <time>{v ? when(v) : state === 'next' ? 'Next' : ''}</time>
              </li>
            )
          })}
        </ol>
      </section>

      <section>
        <h3>Line items <span className="count">{(client === 'nlng' ? o.nlng_order_line_items : o.order_line_items)?.length || 0}</span></h3>
        {client === 'nlng' ? (
          (o.nlng_order_line_items || []).length ? (
            <div className="tw-mini"><table className="mini">
              <thead><tr><th>#</th><th>Description</th><th className="r">Qty</th><th>Due</th><th className="r">Amount</th></tr></thead>
              <tbody>{[...(o.nlng_order_line_items || [])].sort((a, b) => Number(a.item_no || 0) - Number(b.item_no || 0)).map((li, i) => (
                <tr key={i}><td className="mono">{li.item_no || i + 1}</td><td>{li.description || '—'}</td>
                  <td className="r mono">{li.quantity ?? ''} {li.uom || ''}</td><td>{when(li.delivery_date)}</td>
                  <td className="r mono">{li.net_amount != null ? Number(li.net_amount).toLocaleString('en-US', { minimumFractionDigits: 2 }) : ''}</td></tr>
              ))}</tbody>
            </table></div>
          ) : <p className="muted">No line items.</p>
        ) : (
          (o.order_line_items || []).length ? (
            <div className="tw-mini"><table className="mini">
              <thead><tr><th>#</th><th>Description</th><th className="r">Qty</th><th>Promised</th><th>Required</th></tr></thead>
              <tbody>{[...(o.order_line_items || [])].sort((a, b) => (a.line_no || 0) - (b.line_no || 0)).map((li, i) => (
                <tr key={i}><td className="mono">{li.line_no || i + 1}</td><td>{li.description || '—'}</td>
                  <td className="r mono">{li.quantity ?? ''}</td><td>{when(li.promised_date)}</td><td>{when(li.required_delivery_date)}</td></tr>
              ))}</tbody>
            </table></div>
          ) : <p className="muted">{o.extracted_description || 'No line items.'}</p>
        )}
      </section>

      {soItems.length > 0 && (
        <section>
          <h3>SO items <span className="count">{soItems.length}</span></h3>
          <div className="tw-mini"><table className="mini">
            <thead><tr><th>Line</th><th>Item</th><th className="r">Qty</th><th>Dispatch</th><th className="r">Value</th></tr></thead>
            <tbody>{soItems.map((li, i) => (
              <tr key={i}><td className="mono">{li.line_no || i + 1}</td><td className="mono">{li.item_number || '—'}</td>
                <td className="r mono">{li.qty ?? ''} {li.uom || ''}</td><td>{when(li.despatch_date)}</td>
                <td className="r mono">{li.extended_price != null ? '$' + Number(li.extended_price).toLocaleString('en-US', { minimumFractionDigits: 2 }) : ''}</td></tr>
            ))}</tbody>
          </table></div>
        </section>
      )}

      {(stock || routing) && (
        <section>
          <h3>From the warehouse</h3>
          {stock && <div className="note"><b>Stock check</b><p>{stock}</p></div>}
          {routing && <div className="note"><b>Routing note</b><p>{routing}</p></div>}
        </section>
      )}
    </div>
  )
}

// ── Notes ───────────────────────────────────────────────────────────────────

type Comment = { id: string; body: string; author_name?: string; author_role?: string; created_at?: string; user_id?: string; is_system_event?: boolean }
const ROLES = ['warehouse', 'procurement', 'admin', 'expeditor', 'accounts']

function ago(iso?: string): string {
  if (!iso) return ''
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  if (m < 1440) return `${Math.floor(m / 60)}h ago`
  return new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
}

function withMentions(text: string): ReactNode[] {
  return text.split(/(@\w+)/g).map((p, i) => p.startsWith('@') ? <span key={i} className="mention">{p}</span> : p)
}

function Notes({ client, id }: { client: Client; id: string }) {
  const [items, setItems] = useState<Comment[] | null>(null)
  const [err, setErr] = useState('')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)
  const [drop, setDrop] = useState<{ at: number; part: string; pick: number } | null>(null)
  const box = useRef<HTMLTextAreaElement>(null)
  const me = currentUser()
  const isAdmin = me?.role === 'admin'

  const load = async () => {
    try {
      const d = await getJson<Comment[] | { data: Comment[] }>(`/api/comments?order_id=${encodeURIComponent(id)}&type=${client}`)
      setItems(Array.isArray(d) ? d : d.data || []); setErr('')
    } catch (e) { setErr((e as Error).message); setItems([]) }
  }
  useEffect(() => { setItems(null); load() }, [id, client]) // eslint-disable-line react-hooks/exhaustive-deps

  const matches = drop ? ROLES.filter(r => r.startsWith(drop.part.toLowerCase())) : []

  const onType = (v: string) => {
    setDraft(v)
    const pos = box.current?.selectionStart ?? v.length
    const before = v.slice(0, pos)
    const at = before.lastIndexOf('@')
    const part = at >= 0 ? before.slice(at + 1) : ''
    setDrop(at >= 0 && !/\s/.test(part) && ROLES.some(r => r.startsWith(part.toLowerCase())) ? { at, part, pick: 0 } : null)
  }
  const pick = (role: string) => {
    if (!drop) return
    const next = draft.slice(0, drop.at) + '@' + role + ' ' + draft.slice(drop.at + 1 + drop.part.length)
    setDraft(next); setDrop(null)
    requestAnimationFrame(() => { const p = drop.at + role.length + 2; box.current?.setSelectionRange(p, p); box.current?.focus() })
  }

  const post = async () => {
    const text = draft.trim()
    if (!text || busy) return
    setBusy(true)
    try { await send('POST', '/api/comments', { order_id: id, type: client, body: text }); setDraft(''); await load() }
    catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  const saveEdit = async () => {
    if (!editing?.text.trim()) return
    try { await send('PATCH', `/api/comments/${encodeURIComponent(editing.id)}`, { body: editing.text.trim() }); setEditing(null); await load() }
    catch (e) { setErr((e as Error).message) }
  }
  // Deleting asks first, in the app's own box (lib/ConfirmBox).
  const [asking, setAsking] = useState<Comment | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [delErr, setDelErr] = useState('')
  const closeAsk = () => { setAsking(null); setDelErr('') }
  const remove = async () => {
    if (!asking) return
    setDeleting(true); setDelErr('')
    try { await send('DELETE', `/api/comments/${encodeURIComponent(asking.id)}`); await load(); setAsking(null) }
    catch (e) { setDelErr((e as Error).message || 'Could not delete it. Try again.') }
    finally { setDeleting(false) }
  }
  const preview = (s: string) => (s.length > 120 ? s.slice(0, 117).trimEnd() + '…' : s)

  return (
    <div className="notes">
      {asking && (
        <ConfirmBox title="Delete this note?" busy={deleting} error={delErr} confirmLabel="Delete note"
                    onConfirm={remove} onCancel={closeAsk}>
          <b>“{preview(asking.body)}”</b> will be deleted for everyone who can see this order. This can't be undone.
        </ConfirmBox>
      )}
      {err && <p className="err-text">{err}</p>}
      {items === null ? <p className="muted">Loading notes…</p>
        : !items.length ? <p className="muted center">No notes yet. Write the first one below.</p>
        : (
          <ol className="note-list">
            {items.map(c => c.is_system_event ? (
              <li key={c.id} className="sys"><span>{c.body}</span><time>{ago(c.created_at)}</time></li>
            ) : (
              <li key={c.id}>
                <div className="note-hd">
                  <b>{c.author_name || 'Unknown'}</b>
                  {c.author_role && <span className="tag">{c.author_role}</span>}
                  <time>{ago(c.created_at)}</time>
                  <span className="sp" />
                  {me?.id && c.user_id === me.id && <button type="button" className="link" onClick={() => setEditing({ id: c.id, text: c.body })}>Edit</button>}
                  {(isAdmin || (me?.id && c.user_id === me.id)) && <button type="button" className="link danger" onClick={() => setAsking(c)}>Delete</button>}
                </div>
                {editing?.id === c.id ? (
                  <div className="note-edit">
                    <textarea value={editing.text} onChange={e => setEditing({ id: c.id, text: e.target.value })} autoFocus />
                    <div><button type="button" className="btn sm" onClick={saveEdit}>Save</button>
                      <button type="button" className="link" onClick={() => setEditing(null)}>Cancel</button></div>
                  </div>
                ) : <p>{withMentions(c.body)}</p>}
              </li>
            ))}
          </ol>
        )}

      <div className="composer-mini">
        {drop && matches.length > 0 && (
          <div className="mention-drop" role="listbox">
            {matches.map((r, i) => (
              <button key={r} type="button" role="option" aria-selected={i === drop.pick} className={i === drop.pick ? 'on' : ''}
                      onMouseDown={e => { e.preventDefault(); pick(r) }}>@{r}</button>
            ))}
          </div>
        )}
        <textarea ref={box} rows={2} value={draft} disabled={busy} placeholder="Write a note… type @ to mention a team"
                  onChange={e => onType(e.target.value)}
                  onKeyDown={e => {
                    if (drop && matches.length) {
                      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                        e.preventDefault()
                        setDrop(d => d && { ...d, pick: Math.max(0, Math.min(matches.length - 1, d.pick + (e.key === 'ArrowDown' ? 1 : -1))) })
                        return
                      }
                      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(matches[drop.pick]); return }
                      if (e.key === 'Escape') { setDrop(null); return }
                    }
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); post() }
                  }} />
        <button type="button" className="btn sm" onClick={post} disabled={busy || !draft.trim()}>{SEND}{busy ? 'Posting…' : 'Post'}</button>
      </div>
    </div>
  )
}

// ── Story ───────────────────────────────────────────────────────────────────

type Mail = { message_id?: string; direction?: string; from_address?: string; to_address?: string; subject?: string; body_text?: string; received_at?: string }

function plain(s: string): string {
  try { return (new DOMParser().parseFromString(s, 'text/html').body.textContent || '').replace(/\s+/g, ' ').trim() }
  catch { return s.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim() }
}

function storyAge(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const days = Math.floor((Date.now() - d.getTime()) / 86400000)
  if (days <= 0) return 'Written today'
  if (days === 1) return 'Written yesterday'
  return `Written ${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} (${days} days ago)`
}

function Story({ client, id }: { client: Client; id: string }) {
  const [mails, setMails] = useState<Mail[] | null>(null)
  const [err, setErr] = useState('')
  const [openIdx, setOpenIdx] = useState<Set<number>>(new Set())
  const [ai, setAi] = useState<{ state: 'idle' | 'loading' | 'done' | 'error'; text: string; status: string; at: string }>({ state: 'idle', text: '', status: '', at: '' })
  const [chat, setChat] = useState<{ role: 'user' | 'assistant'; content: string }[]>([])
  const [q, setQ] = useState('')
  const [asking, setAsking] = useState(false)

  useEffect(() => {
    setMails(null); setAi({ state: 'idle', text: '', status: '', at: '' }); setChat([])
    getJson<Mail[]>(`/api/emails/stored?order_id=${encodeURIComponent(id)}&type=${client}`)
      .then(m => setMails(m || []), e => { setErr((e as Error).message); setMails([]) })
  }, [id, client])

  const writeStory = async (fresh: boolean) => {
    setAi({ state: 'loading', text: '', status: 'Reading the thread…', at: '' }); setChat([])
    let text = ''
    try {
      await streamEvents<{ saved_at?: string; status?: string; c?: string }>(
        `/api/emails/summarize?order_id=${encodeURIComponent(id)}&type=${client}${fresh ? '&fresh=1' : ''}`, undefined,
        ev => {
          if (ev.saved_at) setAi(a => ({ ...a, at: ev.saved_at! }))
          else if (ev.status) setAi(a => ({ ...a, status: ev.status! }))
          else if (ev.c) { text += ev.c; setAi(a => ({ ...a, text })) }
        })
      setAi(a => ({ ...a, state: text ? 'done' : 'error', status: text ? '' : 'No story came back. Try again in a moment.' }))
    } catch (e) {
      setAi(a => ({ ...a, state: 'error', status: e instanceof ApiError ? e.message : 'Could not write the story.' }))
    }
  }

  const ask = async () => {
    const question = q.trim()
    if (!question || asking) return
    const history = [{ role: 'assistant' as const, content: ai.text }, ...chat, { role: 'user' as const, content: question }]
    setChat(c => [...c, { role: 'user', content: question }, { role: 'assistant', content: '' }]); setQ(''); setAsking(true)
    let reply = ''
    try {
      await streamEvents<{ c?: string }>('/api/emails/chat', { order_id: id, type: client, messages: history }, ev => {
        if (!ev.c) return
        if (ev.c === '__RATE_LIMIT__') { reply = '__RATE_LIMIT__'; return }
        reply += ev.c
        setChat(c => [...c.slice(0, -1), { role: 'assistant', content: reply }])
      })
    } catch { reply = '' }
    if (!reply || reply === '__RATE_LIMIT__') {
      const msg = reply ? 'Too many questions at once - wait about 30 seconds and try again.' : 'No answer came back. Try again in a moment.'
      setChat(c => [...c.slice(0, -2), { role: 'user', content: question }, { role: 'assistant', content: msg }])
    }
    setAsking(false)
  }

  const sorted = [...(mails || [])].sort((a, b) => (a.received_at || '').localeCompare(b.received_at || ''))

  return (
    <div className="story">
      <div className="ai-card">
        {ai.state === 'idle' ? (
          <div className="ai-cta">
            <div><b>AI story</b><span>A plain summary of what happened on this order, written from its emails.</span></div>
            <button type="button" className="btn sm" onClick={() => writeStory(false)} disabled={!mails?.length}>Show the story</button>
          </div>
        ) : (
          <>
            <div className="ai-hd"><b>AI story</b>
              {ai.at && <small>{storyAge(ai.at)}</small>}
              <span className="sp" />
              {ai.state !== 'loading' && <button type="button" className="link" onClick={() => writeStory(true)} title="Write it again from the latest emails">Rewrite</button>}
            </div>
            {ai.text && <p className="ai-text">{ai.text}</p>}
            {!ai.text && ai.state === 'loading' && (
              <div className="thinking-sm"><span className="orbs" aria-hidden="true"><i /><i /><i /></span>{ai.status}</div>
            )}
            {ai.state === 'error' && <p className="err-text">{ai.status}</p>}
            {ai.state === 'done' && (
              <div className="ai-chat">
                {chat.map((m, i) => <p key={i} className={m.role}>{m.content || <span className="muted">Thinking…</span>}</p>)}
                <div className="ai-ask">
                  <input value={q} onChange={e => setQ(e.target.value)} placeholder="Ask about this order…" disabled={asking}
                         onKeyDown={e => { if (e.key === 'Enter') ask() }} />
                  <button type="button" className="btn sm" onClick={ask} disabled={asking || !q.trim()}>Ask</button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <h3>Emails <span className="count">{mails?.length ?? ''}</span></h3>
      {err && <p className="err-text">{err}</p>}
      {mails === null ? <p className="muted">Loading emails…</p>
        : !sorted.length ? <p className="muted">No emails saved for this order yet.</p>
        : (
          <ol className="mails">
            {sorted.map((m, i) => {
              const body = plain(m.body_text || '')
              const isOpen = openIdx.has(i)
              const dir = (m.direction || 'in').toLowerCase()
              return (
                <li key={m.message_id || i} className={dir === 'out' ? 'out' : ''}>
                  <button type="button" className="mail-hd" aria-expanded={isOpen} disabled={!body}
                          onClick={() => setOpenIdx(s => { const n = new Set(s); n.has(i) ? n.delete(i) : n.add(i); return n })}>
                    <span className="mail-top">
                      <b>{(m.from_address || '—').replace(/\s*<[^>]*>\s*$/, '')}</b>
                      <span className={`tag${dir === 'out' ? ' out' : ''}`}>{dir === 'out' ? 'Sent' : 'Received'}</span>
                      <time>{when(m.received_at)}</time>
                    </span>
                    <span className="mail-subj">{m.subject || '(no subject)'}</span>
                    {!isOpen && body && <span className="mail-prev">{body.slice(0, 140)}</span>}
                  </button>
                  {isOpen && <p className="mail-body">{body}</p>}
                </li>
              )
            })}
          </ol>
        )}
    </div>
  )
}

// ── Message the team ────────────────────────────────────────────────────────

function Compose({ client, o, onClose }: { client: Client; o: O; onClose: () => void }) {
  const po = (client === 'nlng' ? o.po_number : o.buyer_po_number) || ''
  const fields: string[] = client === 'nlng'
    ? [`NLNG PO: ${po}`, o.contact_name && `Buyer: ${o.contact_name}`, o.required_delivery_date && `Required By: ${fmtDate(o.required_delivery_date)?.d}`,
       o.net_value && `Value: ${o.currency || 'USD'} ${Number(o.net_value).toLocaleString()}`].filter(Boolean) as string[]
    : [`PO Number: ${po}`, o.buyer_name && `Buyer: ${o.buyer_name}`, o.extracted_description && `Description: ${o.extracted_description}`,
       o.required_delivery_date && `Required By: ${fmtDate(o.required_delivery_date)?.d}`, o.po_destination && `Destination: ${o.po_destination}`,
       o.po_amount && `Amount: ${money(o.po_amount, o.po_currency)}`].filter(Boolean) as string[]
  const [to, setTo] = useState<Set<string>>(new Set(['warehouse']))
  const [subject, setSubject] = useState(`Availability check — ${client === 'nlng' ? 'NLNG ' : ''}PO ${po}`)
  const [body, setBody] = useState(fields.join('\n') + '\n\nPlease confirm stock availability for the above order.')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [sent, setSent] = useState(false)

  const submit = async () => {
    if (!to.size || !subject.trim() || !body.trim()) { setErr('Pick who it goes to, and write a subject and a message.'); return }
    setBusy(true); setErr('')
    try {
      await Promise.all([...to].map(role => send('POST', '/api/messages', {
        to_role: role, subject: subject.trim(), body: body.trim(), message_type: 'availability_check',
        order_id: o.id, order_client: client, po_pdf_url: o.pdf_url || undefined,
      })))
      setSent(true)
      setTimeout(onClose, 1100)
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <div className="compose" role="dialog" aria-label="Send a message">
      <div className="compose-hd"><b>Send a message</b><button type="button" className="icon-btn" onClick={onClose} aria-label="Close">{X}</button></div>
      <div className="compose-to">
        <span>To</span>
        {ROLES.map(r => (
          <button key={r} type="button" className={`chip${to.has(r) ? ' on' : ''}`} aria-pressed={to.has(r)}
                  onClick={() => setTo(s => { const n = new Set(s); n.has(r) ? n.delete(r) : n.add(r); return n })}>{r}</button>
        ))}
      </div>
      <input value={subject} onChange={e => setSubject(e.target.value)} aria-label="Subject" />
      <textarea rows={8} value={body} onChange={e => setBody(e.target.value)} aria-label="Message" />
      {o.pdf_url ? <p className="muted sm">{DOC} The PO PDF goes with it.</p> : null}
      {err && <p className="err-text">{err}</p>}
      <div className="compose-ft">
        <button type="button" className="link" onClick={onClose}>Cancel</button>
        <button type="button" className="btn" onClick={submit} disabled={busy || sent}>{SEND}{sent ? 'Sent' : busy ? 'Sending…' : 'Send'}</button>
      </div>
    </div>
  )
}

// ── The panel ───────────────────────────────────────────────────────────────

export default function OrderPanel({ client, order, loaded, tab, onTab, onClose, onSaved }: {
  client: Client; order?: O; loaded: boolean; tab: string
  onTab: (t: string) => void; onClose: () => void; onSaved: () => void
}) {
  const [composing, setComposing] = useState(false)
  const panel = useRef<HTMLElement>(null)

  // Closing plays the slide out first (orders.css .leaving), then the page
  // removes the panel. Asked to move less, it just goes.
  const [leaving, setLeaving] = useState(false)
  const shut = () => {
    if (leaving) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { onClose(); return }
    setLeaving(true)
    setTimeout(onClose, 340)
  }
  const shutRef = useRef(shut)
  shutRef.current = shut

  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && !composing) shutRef.current() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [composing])
  useEffect(() => { panel.current?.focus() }, [order?.id])
  useEffect(() => { setComposing(false) }, [order?.id])

  const po = order ? (client === 'nlng' ? order.po_number : order.buyer_po_number) : ''
  const stage = order ? (client === 'nlng' ? nlngStage(order) : chevronStage(order)) : null
  const tabs: [string, string][] = [['overview', 'Overview'], ['notes', 'Notes'], ['story', 'Story']]
  const desc = order ? (client === 'nlng' ? order.nlng_order_line_items?.[0]?.description : order.order_line_items?.[0]?.description || order.extracted_description) : ''

  return (
    <>
      <div className={`scrim-p${leaving ? ' leaving' : ''}`} onClick={shut} />
      <aside className={`panel${leaving ? ' leaving' : ''}`} ref={panel} tabIndex={-1} aria-label={`Order ${po || ''}`}>
        <header className="panel-hd">
          <div className="panel-title">
            <span className="tags">
              <span className={`tag${client === 'nlng' ? ' nlng' : ''}`}>{client === 'nlng' ? 'NLNG' : 'Chevron'}</span>
              {stage && <Pill s={stage} />}
            </span>
            <h2 className="mono">{po || (loaded ? 'Order not found' : 'Loading…')}</h2>
            {desc && <p>{desc}</p>}
          </div>
          <button type="button" className="icon-btn" onClick={shut} aria-label="Close">{X}</button>
        </header>

        {order && (
          <>
            <div className="panel-acts">
              {([['PO PDF', order.pdf_url], ['Ack PDF', client === 'chevron' ? order.ack_pdf_url : null], ['SO PDF', order.so_pdf_url]] as [string, unknown][])
                .filter(([, url]) => typeof url === 'string' && url)
                .map(([label, url]) => (
                  <a key={label} className="btn ghost sm" href={url as string} target="_blank" rel="noopener noreferrer">{DOC}{label}</a>
                ))}
              <span className="sp" />
              <button type="button" className="btn sm" onClick={() => setComposing(true)}>{SEND}Message</button>
            </div>
            <div className="panel-tabs" role="tablist">
              {tabs.map(([k, l]) => (
                <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => onTab(k)}>{l}</button>
              ))}
            </div>
            <div className="panel-body">
              {tab === 'story' ? <Story client={client} id={String(order.id)} />
                : tab === 'overview' ? <Overview client={client} o={order} onSaved={onSaved} />
                : <Notes client={client} id={String(order.id)} />}
            </div>
            {composing && <Compose client={client} o={order} onClose={() => setComposing(false)} />}
          </>
        )}
        {!order && loaded && <p className="card-empty">This order isn’t in the list any more.</p>}
      </aside>
    </>
  )
}
