import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { ApiError } from '../../lib/api'
import { type ChatRow, useAskChats } from '../../lib/askChats'

/*
  The saved Ask chats, listed in the side menu under the pages while Ask is
  open - like Claude's sidebar: the pages stay at the top, the chats scroll
  below them, set smaller and quieter than the page buttons.
*/
const I = {
  plus: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
  find: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>,
  more: <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.7" /><circle cx="12" cy="12" r="1.7" /><circle cx="19" cy="12" r="1.7" /></svg>,
}

/* "Delete this chat?" in the app's own small box. Put on the page body, not in
   the menu, so the menu's edge (or its slide-in on a phone) can't clip it. */
function ConfirmDelete({ title, busy, error, onYes, onNo }: {
  title: string; busy: boolean; error: string; onYes: () => void; onNo: () => void
}) {
  const no = useRef<HTMLButtonElement>(null)
  useEffect(() => { no.current?.focus() }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onNo() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onNo])
  return createPortal(
    <div className="cell-overlay" onMouseDown={e => { if (e.target === e.currentTarget && !busy) onNo() }}>
      <div className="cell-box confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="del-hd" aria-describedby="del-body">
        <h2 id="del-hd">Delete this chat?</h2>
        <p id="del-body">
          <b>“{title}”</b> will be deleted for everyone on the team, not just you. This can't be undone.
        </p>
        {error && <p className="err-text" role="alert">{error}</p>}
        <div className="cell-btns">
          <button ref={no} className="btn ghost" type="button" onClick={onNo} disabled={busy}>Cancel</button>
          <button className="btn danger" type="button" onClick={onYes} disabled={busy}>{busy ? 'Deleting…' : 'Delete chat'}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Row({ chat, active }: { chat: ChatRow; active: boolean }) {
  const { rename, remove } = useAskChats()
  const [menu, setMenu] = useState(false)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(chat.title)
  const [err, setErr] = useState('')
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => { setName(chat.title) }, [chat.title])
  useEffect(() => {
    if (!menu) return
    const off = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setMenu(false) }
    document.addEventListener('mousedown', off)
    return () => document.removeEventListener('mousedown', off)
  }, [menu])

  const doRename = async () => {
    const t = name.trim()
    setEditing(false)
    if (!t || t === chat.title) { setName(chat.title); return }
    try { await rename(chat.id, t); setErr('') }
    catch (e) { setName(chat.title); setErr(e instanceof ApiError ? e.message : 'Could not rename it.') }
  }
  const [asking, setAsking] = useState(false)       // the "are you sure" box is open
  const [deleting, setDeleting] = useState(false)
  const [delErr, setDelErr] = useState('')
  const closeAsk = () => { setAsking(false); setDelErr('') }
  const doRemove = async () => {
    setDeleting(true); setDelErr('')
    try { await remove(chat.id) }                    // the row goes away with it
    catch (e) { setDelErr(e instanceof ApiError ? e.message : 'Could not delete it. Try again.'); setDeleting(false) }
  }
  const confirmBox = asking && (
    <ConfirmDelete title={chat.title} busy={deleting} error={delErr} onYes={doRemove} onNo={closeAsk} />
  )

  if (editing) {
    return (
      <div className="chat-row editing">
        <input autoFocus value={name} maxLength={120} aria-label="Chat name"
               onChange={e => setName(e.target.value)} onBlur={doRename}
               onKeyDown={e => { if (e.key === 'Enter') doRename(); if (e.key === 'Escape') { setName(chat.title); setEditing(false) } }} />
      </div>
    )
  }
  return (
    <div className={`chat-row${active ? ' on' : ''}${menu ? ' menu-open' : ''}`} ref={box}>
      <Link to={`/ask/${chat.id}`} aria-current={active ? 'page' : undefined}
            title={chat.created_by_name ? `${chat.title} - started by ${chat.created_by_name}` : chat.title}>
        <i aria-hidden="true" /><span>{chat.title}</span>
      </Link>
      {chat.can_edit && (
        <button className="more" type="button" aria-label={`Options for ${chat.title}`} aria-haspopup="menu"
                aria-expanded={menu} onClick={() => setMenu(m => !m)}>{I.more}</button>
      )}
      {menu && (
        <div className="row-menu" role="menu">
          <button type="button" role="menuitem" onClick={() => { setMenu(false); setEditing(true) }}>Rename</button>
          <button type="button" role="menuitem" className="danger" onClick={() => { setMenu(false); setAsking(true) }}>Delete</button>
        </div>
      )}
      {err && <p className="row-err" role="alert">{err}</p>}
      {confirmBox}
    </div>
  )
}

// The menu keeps to the most recent chats; "View all" opens the full list.
const RECENT = 25

export default function ChatList({ activeId, allOpen }: { activeId: string | null; allOpen?: boolean }) {
  const { chats, error } = useAskChats()
  const [q, setQ] = useState('')
  const needle = q.trim().toLowerCase()
  const matches = (chats || []).filter(c => !needle
    || c.title.toLowerCase().includes(needle) || (c.created_by_name || '').toLowerCase().includes(needle))
  const shown = needle ? matches : matches.slice(0, RECENT)

  return (
    <div className="rail-chats" aria-label="Chats">
      <div className="rc-head">
        <span>Chats</span>
        <Link to="/ask" className="rc-new" title="New chat" aria-label="New chat">{I.plus}</Link>
      </div>
      {(chats?.length || 0) > 8 && (
        <label className="rc-find">
          {I.find}
          <input type="search" value={q} placeholder="Search chats" aria-label="Search chats" onChange={e => setQ(e.target.value)} />
        </label>
      )}
      <div className="rc-list">
        {error ? <p className="rc-note">{error}</p>
          : chats === null ? <p className="rc-note">Loading chats…</p>
          : !chats.length ? <p className="rc-note">No chats yet. Questions asked here are kept for the whole team.</p>
          : !shown.length ? <p className="rc-note">No chat matches “{q.trim()}”.</p>
          : shown.map(c => <Row key={c.id} chat={c} active={c.id === activeId} />)}
        {!!chats?.length && (
          <Link to="/ask/all" className={`rc-all${allOpen ? ' on' : ''}`}>View all{chats.length > RECENT ? ` ${chats.length} chats` : ''}</Link>
        )}
      </div>
    </div>
  )
}
