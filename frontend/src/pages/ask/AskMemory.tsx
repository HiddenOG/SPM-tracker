import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { getJson, send } from '../../lib/api'
import ConfirmBox from '../../lib/ConfirmBox'
import { canOpen } from '../../lib/pages'
import { currentUser } from '../../lib/session'
import { ago } from './AllChats'
import './ask.css'

/*
  What Ask remembers from one chat to the next (scripts/ask_memory.py): your
  own notes and the team's. Ask saves them when told something that stays
  true; anyone can see them here and delete one that's wrong or out of date.
*/

type Note = { id: string; about: 'person' | 'team'; note: string; user_name?: string | null; created_at: string }

export default function AskMemory() {
  if (!canOpen(currentUser()?.role, 'ask')) return <Navigate to="/dashboard" replace />
  return <Memory />
}

function Memory() {
  const [data, setData] = useState<{ mine: Note[]; team: Note[] } | null>(null)
  const [error, setError] = useState('')
  const [asking, setAsking] = useState<Note | null>(null)
  const [busy, setBusy] = useState(false)
  const [delErr, setDelErr] = useState('')

  const load = () => getJson<{ mine: Note[]; team: Note[] }>('/api/ask/memory')
    .then(d => { setData(d); setError('') }).catch(e => setError((e as Error).message))
  useEffect(() => { load() }, [])

  const remove = async () => {
    if (!asking) return
    setBusy(true); setDelErr('')
    try { await send('DELETE', `/api/ask/memory/${asking.id}`); setAsking(null); await load() }
    catch (e) { setDelErr((e as Error).message) } finally { setBusy(false) }
  }

  const Section = ({ title, empty, notes }: { title: string; empty: string; notes: Note[] }) => (
    <>
      <h2 className="am-h">{title} <span className="count">{notes.length}</span></h2>
      {!notes.length ? <p className="ac-note">{empty}</p> : (
        <ul className="ac-list am-list">
          {notes.map(n => (
            <li key={n.id}>
              <div className="am-row">
                <span className="t">{n.note}</span>
                <span className="m">{n.about === 'team' && n.user_name ? <>Saved from {n.user_name}’s chat · </> : null}{ago(n.created_at)}</span>
              </div>
              <button type="button" className="link danger" onClick={() => setAsking(n)}>Forget</button>
            </li>
          ))}
        </ul>
      )}
    </>
  )

  return (
    <section className="all-chats">
      {asking && (
        <ConfirmBox title="Forget this?" busy={busy} error={delErr} confirmLabel="Forget it"
                    onConfirm={remove} onCancel={() => { setAsking(null); setDelErr('') }}>
          Ask will stop using “<b>{asking.note}</b>”{asking.about === 'team' ? ' for everyone' : ''}.
        </ConfirmBox>
      )}
      <div className="ac-head"><h1>What Ask remembers</h1></div>
      <p className="ac-count">Ask keeps these from one chat to the next. Tell it “remember …” in a chat to add one, or “forget …” to drop it.</p>
      {error ? <p className="ac-note">{error}</p>
        : data === null ? <p className="ac-note">Loading…</p>
        : (
          <>
            <Section title="About you" notes={data.mine} empty="Nothing yet. Tell Ask how you like things done and it will keep it." />
            <Section title="For the whole team" notes={data.team} empty="Nothing yet." />
          </>
        )}
    </section>
  )
}
