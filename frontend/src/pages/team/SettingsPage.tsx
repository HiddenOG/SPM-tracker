import { useEffect, useState } from 'react'
import { type Theme, getTheme, initials, realUser, setTheme, signOut } from '../../lib/session'
import './team.css'

/*
  Settings: the old page's Appearance choice (Light / Auto / Dark, kept in the
  same spm_theme setting), plus who you are signed in as.
*/

const THEMES: [Theme, string, string][] = [
  ['light', 'Light', 'Always light'], ['auto', 'Auto', 'Follow this device'], ['dark', 'Dark', 'Always dark'],
]

export default function SettingsPage() {
  const me = realUser()
  const [theme, setT] = useState<Theme>(getTheme)
  useEffect(() => {
    const on = () => setT(getTheme())
    window.addEventListener('spm-theme', on)
    return () => window.removeEventListener('spm-theme', on)
  }, [])

  return (
    <div className="page settings">
      <div className="ph"><div><h1>Settings</h1><p>How SPMprocure360 looks, and who you are signed in as</p></div></div>

      <section className="card set-card">
        <div className="card-hd"><h2>Appearance</h2></div>
        <div className="set-row">
          <div><b>Theme</b><small>Choose how SPMprocure360 looks</small></div>
          <div className="theme-pick" role="radiogroup" aria-label="Theme">
            {THEMES.map(([v, label, hint]) => (
              <button key={v} type="button" role="radio" aria-checked={theme === v} className={theme === v ? 'on' : ''} onClick={() => setTheme(v)} title={hint}>
                <span className={`swatch ${v}`} aria-hidden="true" />{label}
              </button>
            ))}
          </div>
        </div>
      </section>

      <section className="card set-card">
        <div className="card-hd"><h2>Your account</h2></div>
        <div className="set-row">
          <div className="set-who">
            <span className="tm-av" style={{ background: 'var(--accent)' }}>{initials(me)}</span>
            <div><b>{me?.name || '—'}</b><small>{me?.email || ''}</small></div>
            {me?.role && <span className={`rbadge ${me.role}`}>{me.role}</span>}
          </div>
          <button type="button" className="btn ghost" onClick={signOut}>Sign out</button>
        </div>
      </section>
    </div>
  )
}
