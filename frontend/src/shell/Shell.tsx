import { type CSSProperties, Fragment, useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { currentUser, endPreview, previewing } from '../lib/session'
import { type NavItem, navFor } from '../lib/pages'
import TopBar, { Icon, useFolded, useNarrow } from './TopBar'
import { useOrders } from '../lib/data'
import { AskChatsProvider } from '../lib/askChats'
import ChatList from '../pages/ask/ChatList'
import './shell.css'

export default function Shell() {
  const user = currentUser()
  const [open, setOpen] = useState(false)           // the drawer, on a narrow screen
  const [folded, setFolded] = useFolded('spm_rail_mini') // icons only, on a wide screen
  const loc = useLocation()

  // Close the drawer whenever the page changes.
  useEffect(() => { setOpen(false) }, [loc.pathname])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const items = navFor(user?.role)
  const { unread } = useOrders()
  const nav = useNavigate()
  const preview = previewing()
  const exitPreview = () => { endPreview(); nav('/team') }
  const narrow = useNarrow()
  const toggle = () => narrow ? setOpen(o => !o) : setFolded(!folded)
  const mini = folded && !narrow                    // a drawer always shows its labels
  // On Ask, the saved chats are listed under the pages.
  const onAsk = /^\/ask(\/|$)/.test(loc.pathname)
  const chatId = loc.pathname.match(/^\/ask\/([^/]+)/)?.[1] || null

  // On Ask the menu is cut to its first group of pages, so the chats sit high
  // up; "More" slides the rest open and pushes the chats down. Leaving Ask
  // puts the full menu back.
  const [more, setMore] = useState(false)
  useEffect(() => { if (!onAsk) setMore(false) }, [onAsk])
  const split = onAsk && !mini
  const firstGroup = items.findIndex((n, i) => i > 0 && n.section)
  // The first group keeps its heading, so the menu's top is the same as on
  // every other page - Ask only adds to it.
  const head = split && firstGroup > 0 ? items.slice(0, firstGroup) : items
  const rest = split && firstGroup > 0 ? items.slice(firstGroup) : []
  const hiddenUnread = !more && rest.some(n => n.key === 'messages') ? unread : 0

  const link = (n: NavItem, i = 0) => (
    <Fragment key={n.key}>
      {n.section && <div className="nav-sec" style={{ '--i': i } as CSSProperties}><span>{n.section}</span></div>}
      {n.to ? (
        <NavLink to={n.to} className={({ isActive }) => isActive ? 'on' : ''} title={mini ? n.label : undefined}
                 style={{ '--i': i } as CSSProperties}>
          <Icon d={n.icon} /><span>{n.label}</span>
          {n.key === 'messages' && unread > 0 && <b className="nav-badge" aria-label={`${unread} unread`}>{unread > 99 ? '99+' : unread}</b>}
        </NavLink>
      ) : (
        <a href={`/#${n.key}`} title={`${n.label} - classic view`}>
          <Icon d={n.icon} /><span>{n.label}</span>
          <svg className="ext" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8" /></svg>
        </a>
      )}
    </Fragment>
  )

  return (
    <AskChatsProvider active={onAsk}>
    <div className={`shell${open ? ' open' : ''}${mini ? ' mini' : ''}${onAsk && !mini ? ' with-chats' : ''}`}>
      <TopBar panelShown={narrow ? open : !mini} onMenu={toggle} />

      <aside className="rail" aria-label="Pages">
        <nav className="nav">
          {head.map(n => link(n))}
          {rest.length > 0 && <>
            {/* The rest of the pages, folded away; they slide open (and shut)
                rather than jumping, and can't be tabbed to while shut. */}
            <div className={`nav-rest${more ? ' open' : ''}`} {...(more ? {} : { inert: '' })}>
              <div>{rest.map((n, i) => link(n, i))}</div>
            </div>
            <button className={`nav-more${more ? ' open' : ''}`} type="button" aria-expanded={more} onClick={() => setMore(m => !m)}>
              <Icon d="m9 6 6 6-6 6" /><span>{more ? 'Less' : 'More'}</span>
              {hiddenUnread > 0 && <b className="nav-badge" aria-label={`${hiddenUnread} unread messages`}>{hiddenUnread > 99 ? '99+' : hiddenUnread}</b>}
            </button>
          </>}
        </nav>
        {onAsk && !mini && <ChatList activeId={chatId === 'all' ? null : chatId} allOpen={chatId === 'all'} />}
        {items.some(n => !n.to) && (
          <p className="rail-note">Pages marked <span aria-hidden="true">↗</span> open in the classic view while they're being rebuilt.</p>
        )}
      </aside>
      <div className="scrim" onClick={() => setOpen(false)} />

      <main className="main">
        {preview && (
          <div className="preview-bar" role="status">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
            <span>Previewing as <b>{preview.name}</b> ({preview.role}) — the menu and pages as they see them</span>
            <button type="button" onClick={exitPreview}>Exit preview</button>
          </div>
        )}
        <Outlet />
      </main>
    </div>
    </AskChatsProvider>
  )
}
