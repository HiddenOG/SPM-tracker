import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getJson, send } from '../../lib/api'
import { realUser, startPreview } from '../../lib/session'
import './team.css'

/*
  Team (admin only): who has access to SPMprocure360. A port of the Team page
  in web/script.js - add a member, reset a password, switch an account off or
  on, remove a member, and preview the app as another role.
  The server enforces every rule here too (require_admin, no deleting admins
  or yourself, seven active accounts per department).
*/

type Member = { id: string; email: string; full_name?: string | null; role: string; is_active: boolean; last_login_at?: string | null }

const ROLES: [string, string][] = [
  ['admin', 'Admin — full access'], ['procurement', 'Procurement — orders & reports'], ['warehouse', 'Warehouse — stock & dispatch'],
  ['expeditor', 'Expeditor — orders & delays'], ['accounts', 'Accounts — dashboard & reports'],
]
// Department logins share one mailbox each (mirrors DEPT_EMAILS on the server).
const DEPT_EMAILS: Record<string, string> = {
  procurement: 'specialpiping@gmail.com', warehouse: 'spmwarehouse22@gmail.com',
  accounts: 'accounts@specialpipingltd.com', expeditor: 'etsano@specialpipingltd.com',
}
const PALETTE = ['#7c3aed', '#1d4ed8', '#0369a1', '#047857', '#b45309', '#9f1239', '#0e7490', '#4338ca']
function avColor(s: string) {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff
  return PALETTE[h % PALETTE.length]
}
function initials(name: string) {
  const p = name.trim().split(/\s+/)
  return (p.length >= 2 ? p[0][0] + p[p.length - 1][0] : name.slice(0, 2)).toUpperCase()
}
function relTime(iso?: string | null) {
  if (!iso) return 'Never'
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 2) return 'Just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d}d ago`
  if (d < 30) return `${Math.floor(d / 7)}w ago`
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: '2-digit' })
}

function Modal({ title, onClose, children, width = 420 }: { title: React.ReactNode; onClose: () => void; children: React.ReactNode; width?: number }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <div className="cell-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="cell-box tm-box" role="dialog" style={{ maxWidth: width }}>
        <div className="cell-hd"><span className="cell-col">{title}</span><button type="button" className="cell-close" onClick={onClose} aria-label="Close">×</button></div>
        {children}
      </div>
    </div>
  )
}

function AddUser({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('')
  const [role, setRole] = useState('')
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async () => {
    // For department roles the email is set by the server; an admin needs one typed.
    if (!role || !pw || (role === 'admin' && !email.trim())) return setErr(role === 'admin' ? 'Email, role, and password are required.' : 'Role and password are required.')
    if (pw.length < 6) return setErr('Password must be at least 6 characters.')
    setBusy(true); setErr('')
    try {
      await send('POST', '/api/users', { full_name: name.trim(), role, password: pw, ...(role === 'admin' ? { email: email.trim() } : {}) })
      onDone()
    } catch (e) { setErr((e as Error).message || 'Could not create user.') } finally { setBusy(false) }
  }
  return (
    <Modal title="Add team member" onClose={onClose}>
      <div className="tm-form">
        <label><span>Full name</span><input autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Jane Doe" /></label>
        <label><span>Role / department</span>
          <select value={role} onChange={e => { setRole(e.target.value); setEmail('') }}>
            <option value="">Select role…</option>
            {ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        {role === 'admin' && <label><span>Email</span><input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="admin@specialpipingltd.com" /></label>}
        {role && role !== 'admin' && <div className="tm-dept"><span>Department email</span><b className="mono">{DEPT_EMAILS[role] || ''}</b></div>}
        <label><span>Password</span><input type="password" value={pw} onChange={e => setPw(e.target.value)} placeholder="Set a password for this account" onKeyDown={e => { if (e.key === 'Enter') go() }} /></label>
        {err && <div className="tm-err" role="alert">{err}</div>}
        <div className="cell-btns">
          <button type="button" className="btn" onClick={go} disabled={busy}>{busy ? 'Creating…' : 'Create account'}</button>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </Modal>
  )
}

function ResetPw({ member, onClose }: { member: Member; onClose: () => void }) {
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [done, setDone] = useState(false)
  const go = async () => {
    if (!pw || pw.length < 6) return setErr('Password must be at least 6 characters.')
    setBusy(true); setErr('')
    try { await send('PATCH', `/api/users/${member.id}`, { password: pw }); setDone(true); setTimeout(onClose, 900) }
    catch (e) { setErr((e as Error).message || 'Could not reset password.') } finally { setBusy(false) }
  }
  return (
    <Modal title={<>Reset password — {member.full_name || member.email}</>} onClose={onClose} width={380}>
      <div className="tm-form">
        <label><span>New password</span><input type="password" autoFocus value={pw} onChange={e => setPw(e.target.value)} placeholder="Enter new password" onKeyDown={e => { if (e.key === 'Enter') go() }} /></label>
        {err && <div className="tm-err" role="alert">{err}</div>}
        <div className="cell-btns">
          <button type="button" className="btn" onClick={go} disabled={busy || done}>{done ? 'Password set ✓' : busy ? 'Saving…' : 'Set password'}</button>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </Modal>
  )
}

function RemoveUser({ member, onClose, onDone }: { member: Member; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async () => {
    setBusy(true); setErr('')
    try { await send('DELETE', `/api/users/${member.id}`); onDone() }
    catch (e) { setErr((e as Error).message || 'Could not remove user.'); setBusy(false) }
  }
  return (
    <Modal title="Remove team member" onClose={onClose} width={360}>
      <p className="ds-text">Remove <strong>{member.full_name || member.email}</strong> from the team? This cannot be undone.</p>
      <div className="cell-btns">
        <button type="button" className="btn danger" onClick={go} disabled={busy}>{busy ? 'Removing…' : 'Remove'}</button>
        <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
      </div>
      {err && <p className="err-text" style={{ marginTop: 8 }}>{err}</p>}
    </Modal>
  )
}

const EYE = <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
const LOCK = <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
const BIN = <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M19 6l-1 14H6L5 6M10 11v6M14 11v6M9 6V4h6v2" /></svg>

export default function TeamPage() {
  const me = realUser()
  const nav = useNavigate()
  const [members, setMembers] = useState<Member[] | null>(null)
  const [err, setErr] = useState('')
  const [adding, setAdding] = useState(false)
  const [resetting, setResetting] = useState<Member | null>(null)
  const [removing, setRemoving] = useState<Member | null>(null)

  const load = useCallback(async () => {
    try { setMembers(await getJson<Member[]>('/api/users')); setErr('') }
    catch (e) { setErr((e as Error).message); setMembers([]) }
  }, [])
  useEffect(() => { if (me?.role === 'admin') load() }, [load, me?.role])

  if (me?.role !== 'admin') {
    return <div className="page team"><div className="ph"><div><h1>Team</h1><p>Only an admin can manage who has access.</p></div></div></div>
  }

  const toggle = async (m: Member, on: boolean) => {
    setMembers(ms => ms && ms.map(x => (x.id === m.id ? { ...x, is_active: on } : x)))
    try { await send('PATCH', `/api/users/${m.id}`, { is_active: on }) }
    catch (e) {
      setMembers(ms => ms && ms.map(x => (x.id === m.id ? { ...x, is_active: !on } : x)))
      setErr('Could not change ' + (m.full_name || m.email) + ': ' + (e as Error).message)
    }
  }
  const preview = (m: Member) => { startPreview({ role: m.role, name: m.full_name || m.email }); nav('/dashboard') }

  return (
    <div className="page team">
      <div className="ph"><div><h1>Team</h1><p>Manage who has access to SPMprocure360</p></div></div>
      {err && <div className="qf-banner bad card" role="alert"><span>{err}</span><button type="button" onClick={() => setErr('')} aria-label="Dismiss">×</button></div>}

      <section className="card">
        <div className="card-hd">
          <h2>Members {members && <span className="count">{members.length}</span>}</h2>
          <button type="button" className="btn sm" onClick={() => setAdding(true)}>+ Add user</button>
        </div>
        {members === null && <p className="card-empty">Loading…</p>}
        {members && !members.length && <p className="card-empty">No users yet</p>}
        <ul className="tm-list">
          {members?.map(u => {
            const name = u.full_name || u.email.split('@')[0]
            const isMe = me?.id === u.id
            return (
              <li key={u.id} className={u.is_active ? '' : 'off'}>
                <span className="tm-av" style={{ background: avColor(u.id || u.email) }}>{initials(name)}</span>
                <span className="tm-info">
                  <b>{u.full_name || name}{isMe && <span className="tm-you">you</span>}</b>
                  <small>{u.email}</small>
                </span>
                <span className={`rbadge ${u.role}`}>{u.role}</span>
                <span className="tm-login" title={u.last_login_at ? new Date(u.last_login_at).toLocaleString('en-GB') : 'Never signed in'}>{relTime(u.last_login_at)}</span>
                <span className="tm-acts">
                  {!isMe && <button type="button" className="icon-btn" title="Preview as this role" aria-label={`Preview as ${name}`} onClick={() => preview(u)}>{EYE}</button>}
                  <button type="button" className="icon-btn" title="Reset password" aria-label={`Reset ${name}'s password`} onClick={() => setResetting(u)}>{LOCK}</button>
                  <label className="tog" title={isMe ? "You can't switch off your own account" : u.is_active ? 'Deactivate' : 'Activate'}>
                    <input type="checkbox" checked={u.is_active} disabled={isMe} onChange={e => toggle(u, e.target.checked)} aria-label={`${name} is active`} />
                    <i />
                  </label>
                  {!isMe && u.role !== 'admin'
                    ? <button type="button" className="icon-btn del" title="Remove user" aria-label={`Remove ${name}`} onClick={() => setRemoving(u)}>{BIN}</button>
                    : <span className="icon-gap" />}
                </span>
              </li>
            )
          })}
        </ul>
      </section>

      {adding && <AddUser onClose={() => setAdding(false)} onDone={() => { setAdding(false); load() }} />}
      {resetting && <ResetPw member={resetting} onClose={() => setResetting(null)} />}
      {removing && <RemoveUser member={removing} onClose={() => setRemoving(null)} onDone={() => { setRemoving(null); load() }} />}
    </div>
  )
}
