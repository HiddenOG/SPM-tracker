import { useEffect, useState } from 'react'
import { type Theme, currentUser, getTheme, initials, setTheme, signOut } from '../lib/session'
import Bell from './Bell'
import { usePopover } from '../lib/usePopover'

/*
  SPM's red bar: the menu button, the brand, notifications and the account
  menu. Shared by the main pages and Ask, which each bring their own side panel;
  the button opens or closes whichever panel the page has.
*/
export function Icon({ d, size = 17 }: { d: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

/** True when the side panel is a slide-over drawer rather than a column. */
const NARROW = '(max-width: 960px)'
export function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches)
  useEffect(() => {
    const mq = window.matchMedia(NARROW)
    const on = () => setNarrow(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return narrow
}

/** Remember a panel being folded away on a wide screen. */
export function useFolded(key: string): [boolean, (v: boolean) => void] {
  const [folded, setFolded] = useState(() => {
    try { return localStorage.getItem(key) === '1' } catch { return false }
  })
  const set = (v: boolean) => {
    setFolded(v)
    try { v ? localStorage.setItem(key, '1') : localStorage.removeItem(key) } catch { /* not remembered */ }
  }
  return [folded, set]
}

export default function TopBar({ panelShown, onMenu, panelName = 'menu' }:
  { panelShown: boolean; onMenu: () => void; panelName?: string }) {
  const user = currentUser()
  // The account menu closes on a click anywhere outside it, or on Escape.
  const { open: menu, setOpen: setMenu, ref: acct } = usePopover()
  const [theme, setThemeState] = useState<Theme>(getTheme)

  useEffect(() => {
    const on = () => setThemeState(getTheme())
    window.addEventListener('spm-theme', on)
    return () => window.removeEventListener('spm-theme', on)
  }, [])

  return (
    <header className="bar">
      <button className="bar-btn menu-btn" type="button" aria-label={`${panelShown ? 'Hide' : 'Show'} the ${panelName}`}
              title={`${panelShown ? 'Hide' : 'Show'} the ${panelName}`} aria-expanded={panelShown} onClick={onMenu}>
        <Icon d="M4 7h16M4 12h16M4 17h10" size={19} />
      </button>
      <div className="brand">
        <div className="mark"><Icon d="M12 3 2 7.5v9L12 21l10-4.5v-9zM12 12 2 7.5M12 12v9M12 12l10-4.5" size={16} /></div>
        <b>SPMprocure360</b>
      </div>
      <div className="bar-sp" />
      <Bell />
      <div className="acct" ref={acct}>
        <button className="av" type="button" aria-haspopup="menu" aria-expanded={menu}
                onClick={() => setMenu(m => !m)} title={user?.name || ''}>
          {initials(user)}
        </button>
        {menu && (
          <div className="acct-menu" role="menu">
            <div className="acct-who"><b>{user?.name || 'Signed in'}</b><span>{user?.role || ''}</span></div>
            <div className="acct-row">
              <span>Theme</span>
              <div className="seg-mini" role="radiogroup" aria-label="Theme">
                {(['light', 'dark', 'auto'] as Theme[]).map(t => (
                  <button key={t} type="button" role="radio" aria-checked={theme === t} className={theme === t ? 'on' : ''} onClick={() => setTheme(t)}>
                    {t[0].toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>
            </div>
            <button className="acct-out" type="button" role="menuitem" onClick={signOut}>Sign out</button>
          </div>
        )}
      </div>
    </header>
  )
}
