import { useEffect, useRef, useState } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { useAskChats } from '../../lib/askChats'
import { canOpen } from '../../lib/pages'
import { currentUser } from '../../lib/session'
import './ask.css'

/*
  Every saved Ask chat on one page (like Claude's "Chats" page): newest first,
  who started each and how long ago. The menu only keeps the recent ones.
*/

// "Just now", "9 hours ago", "Yesterday", "4 days ago", then the date.
export function ago(iso: string): string {
  const t = new Date(iso).getTime()
  if (isNaN(t)) return ''
  const mins = Math.round((Date.now() - t) / 60000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins} ${mins === 1 ? 'minute' : 'minutes'} ago`
  const hrs = Math.round(mins / 60)
  const d = new Date(t), y = new Date(); y.setDate(y.getDate() - 1)
  if (hrs < 24 && d.toDateString() === new Date().toDateString()) return `${hrs} ${hrs === 1 ? 'hour' : 'hours'} ago`
  if (d.toDateString() === y.toDateString()) return 'Yesterday'
  const days = Math.max(2, Math.round(hrs / 24))
  if (days < 30) return `${days} days ago`
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' })
}

const I = {
  find: <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>,
  plus: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
}

export default function AllChats() {
  if (!canOpen(currentUser()?.role, 'ask')) return <Navigate to="/dashboard" replace />
  return <List />
}

function List() {
  const { chats, error } = useAskChats()
  const go = useNavigate()
  const [q, setQ] = useState('')
  const [searching, setSearching] = useState(false)
  const box = useRef<HTMLInputElement>(null)
  useEffect(() => { if (searching) box.current?.focus() }, [searching])

  const needle = q.trim().toLowerCase()
  const rows = (chats || []).filter(c => !needle
    || c.title.toLowerCase().includes(needle) || (c.created_by_name || '').toLowerCase().includes(needle))

  return (
    <section className="all-chats">
      <div className="ac-head">
        <h1>Chats</h1>
        <div className="ac-tools">
          {searching ? (
            <input ref={box} type="search" value={q} placeholder="Search chats or who started them" aria-label="Search chats"
                   onChange={e => setQ(e.target.value)}
                   onKeyDown={e => { if (e.key === 'Escape') { setQ(''); setSearching(false) } }}
                   onBlur={() => { if (!q) setSearching(false) }} />
          ) : (
            <button className="ac-icon" type="button" aria-label="Search chats" title="Search chats" onClick={() => setSearching(true)}>{I.find}</button>
          )}
          <button className="ac-new" type="button" onClick={() => go('/ask')}>{I.plus}New</button>
        </div>
      </div>
      {chats && !error && <p className="ac-count">{chats.length} {chats.length === 1 ? 'chat' : 'chats'} · shared with everyone who uses Ask</p>}

      {error ? <p className="ac-note">{error}</p>
        : chats === null ? <p className="ac-note">Loading chats…</p>
        : !chats.length ? <p className="ac-note">No chats yet. <Link to="/ask">Ask the first question</Link> and it will be kept here.</p>
        : !rows.length ? <p className="ac-note">No chat matches “{q.trim()}”.</p>
        : (
          <ul className="ac-list">
            {rows.map(c => (
              <li key={c.id}>
                <Link to={`/ask/${c.id}`}>
                  <span className="t">{c.title}</span>
                  <span className="m">{c.created_by_name && <>{c.created_by_name} · </>}{ago(c.updated_at)}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}
