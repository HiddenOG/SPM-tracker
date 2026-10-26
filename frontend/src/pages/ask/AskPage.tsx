import { useEffect, useRef, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { ApiError, getJson, streamEvents } from '../../lib/api'
import { useAskChats } from '../../lib/askChats'
import { canOpen } from '../../lib/pages'
import { currentUser } from '../../lib/session'
import { formatAnswer } from './format'
import './ask.css'

/*
  Ask: questions answered from the whole inbox and the tracker.

  POST /api/ask streams events: which saved chat this exchange belongs to,
  then a step as each search or read starts and finishes, the answer a few
  words at a time, then the emails it cited. While the words stream they are
  shown plain; once done they are formatted.

  /ask is a new chat and /ask/<id> reopens a saved one; while Ask is open the
  side menu lists the saved chats under the pages (ChatList, in the Shell).
*/

type Step = { id: string; label: string; state: 'run' | 'done' | 'error'; detail?: string; secs?: number }
type Source = { ref: number; id: string; date: string; from: string; subject: string; snippet?: string }
type Event =
  | ({ t: 'step' } & Step)
  | { t: 'text'; d: string }
  | { t: 'reset' }
  | { t: 'sources'; items: Source[] }
  | { t: 'done'; secs: number; rounds: number }
  | { t: 'error'; message: string }
  | { t: 'chat'; id: string }
  | { t: 'saved'; ok: boolean; message: string }

type Turn = {
  key: number
  question: string
  asker?: string
  at: string
  steps: Step[]
  text: string
  sources: Source[]
  status: 'working' | 'done' | 'stopped'
  secs?: number
  error?: string
  unsaved?: string      // why this exchange did not make it into the history
}

type SavedMessage = {
  role: 'user' | 'assistant'
  content: string
  sources?: Source[] | null
  steps?: Step[] | null
  secs?: number | string | null
  asked_by?: string | null
  created_at: string
}
type SavedChat = { id: string; title: string; created_by_name?: string | null; can_edit?: boolean; messages: SavedMessage[] }

// A saved chat back into the exchanges the page shows.
function toTurns(msgs: SavedMessage[], nextKey: () => number): Turn[] {
  const out: Turn[] = []
  for (const m of msgs) {
    if (m.role === 'user') {
      out.push({ key: nextKey(), question: m.content, asker: m.asked_by || undefined, at: stamp(m.created_at),
                 steps: [], text: '', sources: [], status: 'stopped' })
    } else {
      const t = out[out.length - 1]
      if (!t || t.text) continue                     // an answer with no question: skip it
      t.text = m.content
      t.steps = m.steps || []
      t.sources = m.sources || []
      t.secs = m.secs == null ? undefined : Number(m.secs)
      t.status = 'done'
    }
  }
  for (const t of out) if (t.status === 'stopped') t.error = 'No answer was kept for this question.'
  return out
}

type Scope = 'both' | 'mail' | 'tracker'
const SCOPES: { v: Scope; label: string }[] = [
  { v: 'both', label: 'Email + tracker' },
  { v: 'mail', label: 'Email' },
  { v: 'tracker', label: 'Tracker' },
]

// Gmail's web links use the message id in hexadecimal.
function gmailLink(id: string): string {
  try { return 'https://mail.google.com/mail/#all/' + BigInt(id).toString(16) }
  catch { return 'https://mail.google.com/mail/' }
}

// The time an exchange happened; with the date too, once it is not today's.
function stamp(iso?: string): string {
  const d = iso ? new Date(iso) : new Date()
  if (isNaN(d.getTime())) return ''
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return d.toDateString() === new Date().toDateString()
    ? hm : `${d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} ${hm}`
}

function senderName(from: string): string {
  return from.replace(/\s*<[^>]*>\s*$/, '').replace(/^"|"$/g, '') || from
}

function summary(steps: Step[]): string {
  let s = 0, r = 0, t = 0
  for (const st of steps) {
    if (/^Searching the inbox/.test(st.label)) s++
    else if (/^Reading/.test(st.label)) r++
    else t++
  }
  const parts: string[] = []
  if (s) parts.push('Searched the inbox' + (s > 1 ? ` ${s} times` : ''))
  if (r) parts.push(`read ${r} ${r === 1 ? 'email' : 'emails'}`)
  if (t) parts.push(s || r ? 'checked the tracker' : 'Checked the tracker')
  return parts.join(' · ') || 'Answered'
}

// What the thinking line says: the step under way, in plain words, or just "Thinking".
function thinkingLabel(steps: Step[]): string {
  const running = [...steps].reverse().find(s => s.state === 'run')
  if (!running) return steps.length ? 'Putting the answer together' : 'Thinking'
  if (/^Searching the inbox/.test(running.label)) return 'Searching the inbox'
  if (/^Reading/.test(running.label)) return 'Reading the emails'
  return 'Checking the tracker'
}

const I = {
  check: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 5 5 9-10" /></svg>,
  cross: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>,
  chev: <svg className="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="m6 9 6 6 6-6" /></svg>,
  search: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>,
  mail: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>,
  copy: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></svg>,
  plus: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
  send: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>,
  stop: <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>,
}

function Answer({ turn }: { turn: Turn }) {
  const [open, setOpen] = useState(true)
  const [copied, setCopied] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  // The trace folds away once the answer is in; the steps are still a click away.
  useEffect(() => { if (turn.status !== 'working') setOpen(false) }, [turn.status])

  const cite = (n: number) => {
    const card = box.current?.querySelector<HTMLElement>(`[data-ref="${n}"]`)
    if (!card) return
    card.scrollIntoView({ behavior: 'smooth', block: 'center' })
    card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash')
  }
  const copy = () => {
    navigator.clipboard?.writeText(turn.text).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 1500)
    }, () => {})
  }

  const working = turn.status === 'working'
  return (
    <div className="a" ref={box}>
      <div className="a-head"><div className="mark-sm">SPM</div><b>Ask</b><span className="mono">{turn.at}</span></div>

      {turn.steps.length > 0 && (
        <div className="trace">
          <button className="trace-t" type="button" aria-expanded={open} onClick={() => setOpen(o => !o)}>
            {I.search}
            <span className="sum">{working ? 'Working…' : turn.status === 'stopped' ? 'Stopped' : summary(turn.steps)}</span>
            {turn.secs != null && <span className="mono t">{turn.secs}s</span>}
            {I.chev}
          </button>
          {open && (
            <ol className="steps">
              {turn.steps.map(s => (
                <li key={s.id}>
                  <span className={`st ${s.state === 'error' ? 'err' : s.state}`}>
                    {s.state === 'done' ? I.check : s.state === 'error' ? I.cross : null}
                  </span>
                  <span className="what">{s.label}<small>{s.state === 'run' ? 'working…' : s.detail || ''}</small></span>
                  <span className="t mono">{s.secs != null ? `${s.secs}s` : ''}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      <div className="ans">
        {turn.status === 'done'
          ? formatAnswer(turn.text, cite)
          : turn.text
            ? <p className="live">{turn.text}</p>
            : working && (
              <div className="thinking" role="status">
                <span className="orbs" aria-hidden="true"><i /><i /><i /></span>
                <span className="shimmer">{thinkingLabel(turn.steps)}</span>
              </div>
            )}
      </div>

      {turn.error && <div className="err"><b>Couldn’t answer.</b> {turn.error}</div>}
      {turn.unsaved && <div className="unsaved" role="status"><b>Not kept in the history.</b> {turn.unsaved}</div>}

      {turn.sources.length > 0 && (
        <div className="sources-wrap">
          <div className="label">{I.mail}Sources · {turn.sources.length} {turn.sources.length === 1 ? 'email' : 'emails'}</div>
          <div className="sources">
            {turn.sources.map(s => (
              <a key={s.ref} className="src" data-ref={s.ref} href={gmailLink(s.id)} target="_blank" rel="noopener noreferrer">
                <span className="top"><span className="num">{s.ref}</span><span className="from">{senderName(s.from || '')}</span></span>
                <span className="meta mono">{s.date}</span>
                <span className="subj">{s.subject || '(no subject)'}</span>
                {s.snippet && <span className="snip">{s.snippet}</span>}
              </a>
            ))}
          </div>
        </div>
      )}

      {turn.status === 'done' && (
        <div className="foot">
          <span className="mono">{turn.secs}s</span>
          <button type="button" onClick={copy}>{I.copy}{copied ? 'Copied' : 'Copy answer'}</button>
        </div>
      )}
    </div>
  )
}

export default function AskPage() {
  const user = currentUser()
  if (!canOpen(user?.role, 'ask')) return <Navigate to="/dashboard" replace />
  return <Ask />
}

function Ask() {
  const user = currentUser()
  const { chatId } = useParams()
  const go = useNavigate()
  const [turns, setTurns] = useState<Turn[]>([])
  const [chat, setChat] = useState<{ id: string; title: string; by?: string } | null>(null)
  const [opening, setOpening] = useState('')        // '' or what is wrong with the chat asked for
  const { chats, reload: loadChats } = useAskChats()
  const [draft, setDraft] = useState('')
  const [scope, setScope] = useState<Scope>('both')
  const [busy, setBusy] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  const nextKey = useRef(0)
  const held = useRef<string | null>(null)          // the chat on screen, so its own URL change doesn't reload it

  useEffect(() => {
    if (window.innerWidth > 700) input.current?.focus()
    return () => abort.current?.abort()
  }, [])

  // The chat on screen deleted from the menu: back to a clean page.
  useEffect(() => {
    const on = (e: globalThis.Event) => {
      if ((e as CustomEvent<string>).detail !== held.current) return
      abort.current?.abort()
      held.current = null
      setTurns([]); setChat(null); setOpening('')
      go('/ask', { replace: true })
    }
    window.addEventListener('spm-chat-deleted', on)
    return () => window.removeEventListener('spm-chat-deleted', on)
  }, [go])

  // A rename in the menu shows in the heading too.
  useEffect(() => {
    const row = chats?.find(c => c.id === chat?.id)
    if (row && chat && row.title !== chat.title) setChat({ ...chat, title: row.title })
  }, [chats, chat])

  // Open the chat in the URL - or a clean page for /ask.
  useEffect(() => {
    if ((chatId || null) === held.current) return
    abort.current?.abort()
    held.current = chatId || null
    setTurns([]); setOpening(''); setChat(null)
    if (!chatId) { input.current?.focus(); return }
    const ctrl = new AbortController()
    setOpening('loading')
    getJson<SavedChat>(`/api/ask/chats/${chatId}`, ctrl.signal)
      .then(c => {
        setChat({ id: c.id, title: c.title, by: c.created_by_name || undefined })
        setTurns(toTurns(c.messages || [], () => nextKey.current++))
        setOpening('')
        requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight))
      })
      .catch(e => {
        if ((e as Error).name === 'AbortError') return
        setOpening(e instanceof ApiError && e.status === 404
          ? 'This chat was deleted. Start a new one, or pick another from the list.'
          : e instanceof ApiError ? e.message : "Couldn't open this chat.")
      })
    return () => ctrl.abort()
  }, [chatId])

  // Grow the box with what's typed, up to about seven lines.
  useEffect(() => {
    const el = input.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 168) + 'px'
  }, [draft])

  // Follow the answer down the page unless the person has scrolled up to read.
  const follow = () => {
    const y = document.documentElement.scrollHeight
    if (y - (window.scrollY + window.innerHeight) < 260) window.scrollTo(0, y)
  }
  useEffect(() => { if (turns.length && !opening) follow() }, [turns, opening])

  const update = (key: number, fn: (t: Turn) => Turn) =>
    setTurns(ts => ts.map(t => t.key === key ? fn(t) : t))

  async function ask(question: string) {
    if (busy || !question || opening === 'loading') return
    setBusy(true)
    // What has been said so far in this chat, so a follow-up makes sense.
    // Unfinished turns count too: a "continue" after one that ran out of
    // searches must still know what was being looked for.
    const history = turns.filter(t => t.status !== 'working')
      .flatMap(t => [{ role: 'user', content: t.question },
                     { role: 'assistant', content: t.text || '(No answer - this question stopped before it finished.)' }])
      .slice(-8)
    const key = nextKey.current++
    setTurns(ts => [...ts, { key, question, asker: user?.name, at: stamp(), steps: [], text: '', sources: [], status: 'working' }])
    setOpening('')
    requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight))

    const ctrl = new AbortController()
    abort.current = ctrl
    const t0 = Date.now()
    let text = ''
    let finished = false

    const onEvent = (ev: Event) => {
      if (ev.t === 'chat') {
        // The server has saved the question: this chat now has an address.
        if (held.current !== ev.id) {
          const first = !held.current
          held.current = ev.id
          if (first) setChat({ id: ev.id, title: question, by: user?.name })
          go(`/ask/${ev.id}`, { replace: !first })
          loadChats()
        }
      } else if (ev.t === 'step') {
        const { t: _t, ...step } = ev
        update(key, t => {
          const i = t.steps.findIndex(s => s.id === step.id)
          const steps = i < 0 ? [...t.steps, step] : t.steps.map((s, j) => j === i ? step : s)
          return { ...t, steps }
        })
      } else if (ev.t === 'text') {
        text += ev.d
        update(key, t => ({ ...t, text }))
      } else if (ev.t === 'reset') {
        text = ''
        update(key, t => ({ ...t, text: '' }))
      } else if (ev.t === 'sources') {
        update(key, t => ({ ...t, sources: ev.items || [] }))
      } else if (ev.t === 'done') {
        finished = true
        update(key, t => ({ ...t, status: 'done', secs: ev.secs }))
      } else if (ev.t === 'error') {
        update(key, t => ({ ...t, error: ev.message }))
      } else if (ev.t === 'saved' && !ev.ok) {
        update(key, t => ({ ...t, unsaved: ev.message }))
      }
    }

    try {
      await streamEvents<Event>('/api/ask', { question, scope, history, chat_id: held.current }, onEvent, ctrl.signal)
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        const msg = e instanceof ApiError ? e.message : "Couldn't reach the server. Check the connection and try again."
        update(key, t => ({ ...t, error: msg }))
      }
    }
    if (!finished) {
      // Ended without a done event: keep whatever was written, unformatted.
      const secs = Math.round((Date.now() - t0) / 100) / 10
      update(key, t => ({
        ...t, status: 'stopped', secs,
        error: t.error || (ctrl.signal.aborted ? undefined
          : t.text ? undefined : 'The connection closed before an answer came back. Try again.'),
      }))
    }
    abort.current = null
    setBusy(false)
    loadChats()                                    // this chat moves to the top
  }

  const submit = (q = draft) => {
    const text = q.trim()
    if (!text || busy) return
    setDraft('')
    ask(text)
  }

  const newChat = () => {
    if (busy) abort.current?.abort()
    go('/ask')
    input.current?.focus()
  }

  const empty = !turns.length
  return (
    <section className="ask">
      <div className="ask-head">
        <div className="ask-title">
          {chat ? <>
            <h1 className="chat-name">{chat.title}</h1>
            {chat.by && <p>Started by {chat.by}</p>}
          </> : <>
            <h1>Ask</h1>
            <p>Questions about any order, client or email, answered from the whole inbox and the tracker.</p>
          </>}
        </div>
        {(chat || turns.length > 0) && (
          <button className="ghost" type="button" onClick={newChat} title="Start a new chat">
            {I.plus}New
          </button>
        )}
      </div>

      <div className="thread" aria-live="polite">
        {opening === 'loading' ? (
          <div className="empty"><div className="thinking" role="status">
            <span className="orbs" aria-hidden="true"><i /><i /><i /></span>
            <span className="shimmer">Opening the chat</span>
          </div></div>
        ) : opening ? (
          <div className="empty"><p>{opening}</p></div>
        ) : empty ? (
          <div className="empty">
            <h2>What do you want to know?</h2>
            <p>Ask about any order, client or email. Every answer lists the emails it came from.</p>
          </div>
        ) : turns.map(t => (
          <div className="turn" key={t.key}>
            {t.asker && t.asker !== user?.name && <span className="q-by">{t.asker}</span>}
            <div className="q">{t.question}</div>
            <Answer turn={t} />
          </div>
        ))}
      </div>

      <div className="dock">
        <form className="composer" autoComplete="off" onSubmit={e => { e.preventDefault(); submit() }}>
          <textarea ref={input} rows={1} value={draft} aria-label="Your question"
                    placeholder={busy ? 'Working on it…' : chat ? 'Ask a follow-up…' : 'Ask about any order, client or email…'}
                    onChange={e => setDraft(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }} />
          <div className="row">
            <div className="seg" role="radiogroup" aria-label="Search in">
              {SCOPES.map(s => (
                <button key={s.v} type="button" role="radio" aria-checked={scope === s.v}
                        className={scope === s.v ? 'on' : ''} onClick={() => setScope(s.v)}>{s.label}</button>
              ))}
            </div>
            <span className="sp" />
            {busy ? (
              <button className="send stop" type="button" aria-label="Stop" onClick={() => abort.current?.abort()}>{I.stop}</button>
            ) : (
              <button className="send" type="submit" aria-label="Ask" disabled={!draft.trim()}>{I.send}</button>
            )}
          </div>
        </form>
        <p className="fine">Answers come from emails and can be wrong. Check the source before acting on a date or amount.</p>
      </div>
    </section>
  )
}
