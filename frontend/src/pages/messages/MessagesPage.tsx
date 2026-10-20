import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { getJson, send, upload } from '../../lib/api'
import { useOrders } from '../../lib/data'
import { currentUser } from '../../lib/session'
import './messages.css'

/*
  Messages: the team's inbox, a port of web/messages.html.

  Received is everything sent to your role; Sent is what you sent. Opening a
  conversation marks it read on the server. A reply goes back to the role of
  the last person who wrote to you, keeps the conversation's order link, and
  says "Re:".
*/

type Thread = {
  thread_id: string; subject?: string; message_count: number; unread_count: number
  latest_at?: string; latest_from?: string; latest_to_role?: string; latest_body?: string
}
type Msg = {
  id: string; thread_id?: string; subject?: string; body?: string; created_at?: string
  from_user_id?: string; from_name?: string; from_role?: string; to_role?: string
  order_id?: string | null; order_client?: string | null; po_pdf_url?: string | null
  attachment_url?: string | null; attachment_name?: string | null
}

const ROLES = ['admin', 'procurement', 'warehouse', 'expeditor', 'accounts']
const MAX_FILE = 10 * 1024 * 1024
const PALETTE = ['#7c3aed', '#1d4ed8', '#0369a1', '#047857', '#b45309', '#9f1239', '#0e7490', '#4338ca']

function avColor(s: string) {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff
  return PALETTE[h % PALETTE.length]
}
const initials = (s: string) => s.split(' ').map(w => w[0] || '').slice(0, 2).join('').toUpperCase() || '?'

function relTime(iso?: string) {
  if (!iso) return ''
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 2) return 'Just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d}d ago`
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}

const CLIP = <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>

/** "Attach file" plus the chosen file's pill, with the old 10 MB limit. */
function Attach({ file, onFile }: { file: File | null; onFile: (f: File | null) => void }) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <div className="attach-row">
      <button type="button" className="attach-btn" onClick={() => input.current?.click()}>{CLIP}Attach file</button>
      <input ref={input} type="file" hidden accept=".pdf,.doc,.docx,.xls,.xlsx,.csv,.png,.jpg,.jpeg,.txt"
             onChange={e => {
               const f = e.target.files?.[0]
               if (!f) return
               if (f.size > MAX_FILE) { alert('File is too large — maximum is 10 MB.'); e.target.value = ''; return }
               onFile(f)
             }} />
      {file && (
        <span className="attach-pill"><span>{file.name}</span>
          <button type="button" aria-label="Remove" onClick={() => { onFile(null); if (input.current) input.current.value = '' }}>×</button>
        </span>
      )}
    </div>
  )
}

async function uploadIfAny(file: File | null): Promise<{ url?: string; name?: string }> {
  if (!file) return {}
  const r = await upload<{ url: string; name: string } | { data: { url: string; name: string } }>('/api/messages/upload', file)
  const d = 'data' in r ? r.data : r
  return { url: d.url, name: d.name }
}

function Compose({ onClose, onSent }: { onClose: () => void; onSent: () => void }) {
  const [to, setTo] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  const go = async () => {
    if (!to) return setErr('Please select a recipient.')
    if (!subject.trim()) return setErr('Subject cannot be empty.')
    if (!body.trim()) return setErr('Message cannot be empty.')
    setErr('')
    try {
      setBusy(file ? 'Uploading file…' : 'Sending…')
      const a = await uploadIfAny(file)
      setBusy('Sending…')
      await send('POST', '/api/messages', {
        to_role: to, subject: subject.trim(), body: body.trim(), message_type: 'general',
        attachment_url: a.url, attachment_name: a.name,
      })
      onSent()
    } catch (e) { setErr((e as Error).message || 'Could not send message.') } finally { setBusy('') }
  }

  return (
    <div className="compose-pane">
      <label><span>To</span>
        <select value={to} onChange={e => setTo(e.target.value)} autoFocus>
          <option value="">— Select recipient role —</option>
          {ROLES.map(r => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}
        </select>
      </label>
      <label><span>Subject</span><input value={subject} onChange={e => setSubject(e.target.value)} placeholder="Message subject…" /></label>
      <label><span>Message</span><textarea rows={5} value={body} onChange={e => setBody(e.target.value)} placeholder="Write your message…" /></label>
      <Attach file={file} onFile={setFile} />
      <div className="pane-acts">
        <button type="button" className="btn" onClick={go} disabled={!!busy}>{busy || 'Send'}</button>
        <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
      </div>
      {err && <p className="err-text">{err}</p>}
    </div>
  )
}

export default function MessagesPage() {
  const me = currentUser()
  const role = me?.role || ''
  const { refreshUnread } = useOrders()
  const [tab, setTab] = useState<'received' | 'sent'>('received')
  const [threads, setThreads] = useState<Thread[] | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [msgs, setMsgs] = useState<Msg[] | null>(null)
  const [threadErr, setThreadErr] = useState(false)
  const [composing, setComposing] = useState(false)
  const [replying, setReplying] = useState(false)
  const [reply, setReply] = useState('')
  const [replyFile, setReplyFile] = useState<File | null>(null)
  const [replyBusy, setReplyBusy] = useState('')
  const [replyErr, setReplyErr] = useState('')
  const body = useRef<HTMLDivElement>(null)

  const loadList = useCallback(async (which = tab) => {
    try {
      const d = await getJson<Thread[] | { data: Thread[] }>(which === 'sent' ? '/api/messages/sent' : '/api/messages?role=' + encodeURIComponent(role))
      setThreads(Array.isArray(d) ? d : d.data || [])
    } catch { setThreads(t => t || []) }
  }, [tab, role])

  useEffect(() => { loadList(tab) }, [tab, loadList])

  const switchTab = (t: 'received' | 'sent') => {
    setTab(t); setComposing(false); setOpenId(null); setMsgs(null); setThreads(null)
  }

  const openThread = useCallback(async (id: string) => {
    setOpenId(id); setMsgs(null); setThreadErr(false); setReplying(false); setReplyErr('')
    try {
      const d = await getJson<Msg[] | { data: Msg[] }>('/api/messages/thread/' + encodeURIComponent(id))
      setMsgs(Array.isArray(d) ? d : d.data || [])
      // Opening it marked it read on the server; clear it here too.
      setThreads(ts => ts && ts.map(t => (t.thread_id === id ? { ...t, unread_count: 0 } : t)))
      refreshUnread()
    } catch { setThreadErr(true); setMsgs([]) }
  }, [refreshUnread])

  useEffect(() => { if (body.current) body.current.scrollTop = body.current.scrollHeight }, [msgs])

  const isSent = tab === 'sent'
  const unread = isSent ? 0 : (threads || []).reduce((s, t) => s + (t.unread_count || 0), 0)
  // The reply goes to the latest message you did not write yourself.
  const received = (msgs || []).filter(m => m.from_user_id !== me?.id)
  const replyTo = received.length ? received[received.length - 1] : (msgs || [])[(msgs || []).length - 1]
  const orderMsg = (msgs || []).find(m => m.order_id)

  const sendReply = async () => {
    if (!replyTo || !openId) return
    if (!reply.trim()) return setReplyErr('Reply cannot be empty.')
    if (!replyTo.from_role) return setReplyErr('Cannot determine who to reply to.')
    setReplyErr('')
    try {
      setReplyBusy(replyFile ? 'Uploading…' : 'Sending…')
      const a = await uploadIfAny(replyFile)
      setReplyBusy('Sending…')
      const subj = (replyTo.subject || '')
      await send('POST', '/api/messages', {
        to_role: replyTo.from_role, subject: /^Re:/i.test(subj) ? subj : 'Re: ' + subj, body: reply.trim(),
        message_type: 'stock_response', order_id: replyTo.order_id || null, order_client: replyTo.order_client || null,
        thread_id: openId, attachment_url: a.url, attachment_name: a.name,
      })
      setReply(''); setReplyFile(null); setReplying(false)
      await openThread(openId)
      loadList()
    } catch (e) { setReplyErr((e as Error).message || 'Could not send reply.') } finally { setReplyBusy('') }
  }

  return (
    <div className={`page messages${openId ? ' reading' : ''}`}>
      <div className="ph">
        <div>
          <h1>Inbox</h1>
          <p>Showing messages for {role} role</p>
        </div>
      </div>

      <div className="msg-layout">
        <section className="card list-card">
          <div className="list-hd">
            <div className="tabs" role="tablist">
              <button type="button" role="tab" aria-selected={!isSent} className={!isSent ? 'on' : ''} onClick={() => switchTab('received')}>Received</button>
              <button type="button" role="tab" aria-selected={isSent} className={isSent ? 'on' : ''} onClick={() => switchTab('sent')}>Sent</button>
            </div>
            <button type="button" className="btn sm" onClick={() => setComposing(c => !c)} aria-expanded={composing}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z" /></svg>
              Compose
            </button>
          </div>
          {composing && <Compose onClose={() => setComposing(false)} onSent={() => { setComposing(false); switchTab('sent') }} />}
          <div className="list-count">
            {threads && `${threads.length} thread${threads.length !== 1 ? 's' : ''}${unread ? ` · ${unread} unread` : ''}`}
          </div>
          <ul className="threads">
            {threads === null && <li className="empty-li">Loading…</li>}
            {threads && !threads.length && <li className="empty-li">{isSent ? 'No sent messages yet' : 'No messages yet'}</li>}
            {threads?.map(t => {
              const name = isSent ? `To: ${t.latest_to_role || '—'}` : (t.latest_from || '—')
              const hasUnread = !isSent && t.unread_count > 0
              return (
                <li key={t.thread_id}>
                  <button type="button" className={`t-row${hasUnread ? ' unread' : ''}${openId === t.thread_id ? ' on' : ''}`} onClick={() => openThread(t.thread_id)}>
                    <span className="av" style={{ background: avColor(name + t.thread_id) }}>{initials(name)}</span>
                    <span className="t-main">
                      <span className="t-from">{name}{t.message_count > 1 && <b className="t-n">{t.message_count}</b>}</span>
                      <span className="t-subj">{t.subject || '(no subject)'}</span>
                      <span className="t-prev">{(t.latest_body || '').slice(0, 80)}</span>
                    </span>
                    <time>{relTime(t.latest_at)}</time>
                  </button>
                </li>
              )
            })}
          </ul>
        </section>

        <section className="card read-card">
          {!openId ? <p className="read-empty">Select a conversation to read it</p> : (
            <>
              <div className="read-hd">
                <button type="button" className="icon-btn back" onClick={() => { setOpenId(null); setMsgs(null) }} aria-label="Back to the list">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="m15 18-6-6 6-6" /></svg>
                </button>
                <div className="read-title">
                  <h2>{msgs?.[0]?.subject || (msgs ? '(no subject)' : '')}</h2>
                  <p>{msgs ? `${msgs.length} message${msgs.length !== 1 ? 's' : ''}` : 'Loading…'}
                    {orderMsg && <> · <Link to={`/orders?${orderMsg.order_client === 'nlng' ? 'client=nlng&' : ''}id=${encodeURIComponent(orderMsg.order_id!)}`}>Open the order →</Link></>}
                  </p>
                </div>
              </div>
              <div className="read-body" ref={body}>
                {threadErr && <p className="err-text center">Could not load conversation.</p>}
                {msgs && !msgs.length && !threadErr && <p className="muted center">No messages</p>}
                {msgs?.map(m => {
                  const mine = m.from_user_id === me?.id
                  const from = mine ? 'You' : (m.from_name || 'Unknown')
                  return (
                    <article key={m.id} className={`bubble${mine ? ' mine' : ''}`}>
                      <header>
                        <span className="av sm" style={{ background: avColor(from + (m.from_user_id || '')) }}>{initials(from)}</span>
                        <b>{from}</b>
                        {m.from_role && <span className={`rbadge ${ROLES.includes(m.from_role) ? m.from_role : ''}`}>{m.from_role}</span>}
                        <time>{relTime(m.created_at)}</time>
                      </header>
                      <p>{m.body || ''}</p>
                      {(m.attachment_url || m.po_pdf_url) && (
                        <div className="bubble-files">
                          {m.attachment_url && <a href={m.attachment_url} target="_blank" rel="noopener noreferrer">{CLIP}{m.attachment_name || 'Attachment'}</a>}
                          {m.po_pdf_url && <a href={m.po_pdf_url} target="_blank" rel="noopener noreferrer">{CLIP}PO PDF</a>}
                        </div>
                      )}
                    </article>
                  )
                })}
              </div>
              {!isSent && msgs && msgs.length > 0 && (
                replying ? (
                  <div className="reply-pane">
                    <span className="reply-to">Replying to <b>{replyTo?.from_role || '—'}</b></span>
                    <textarea rows={4} value={reply} onChange={e => setReply(e.target.value)} placeholder="Write your reply…" autoFocus />
                    <Attach file={replyFile} onFile={setReplyFile} />
                    <div className="pane-acts">
                      <button type="button" className="btn" onClick={sendReply} disabled={!!replyBusy}>{replyBusy || 'Send reply'}</button>
                      <button type="button" className="btn ghost" onClick={() => { setReplying(false); setReplyFile(null) }}>Cancel</button>
                    </div>
                    {replyErr && <p className="err-text">{replyErr}</p>}
                  </div>
                ) : (
                  <div className="reply-bar">
                    <button type="button" className="btn ghost sm" onClick={() => { setReply(''); setReplyErr(''); setReplyFile(null); setReplying(true) }}>
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 17-5-5 5-5M20 18v-2a4 4 0 0 0-4-4H4" /></svg>
                      Reply
                    </button>
                  </div>
                )
              )}
            </>
          )}
        </section>
      </div>
    </div>
  )
}
