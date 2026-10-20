import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { getJson } from './api'
import type { ChevronOrder, NlngOrder } from './orders'
import { currentUser } from './session'

/*
  The orders every page reads, loaded once for the whole app and refreshed
  every 30 seconds (the old app's rhythm), plus what the alerts bell needs.
  Pages read it with useOrders() instead of each fetching their own copy.
*/

const POLL_MS = 30000

type Alerts = {
  new_pos: { id: string; po?: string; buyer?: string }[]
  delivery_requests: { id: string; po?: string }[]
  suspicious_emails: { subject?: string; sender?: string; result?: string }[]
}
export type Mention = { id?: string; body?: string; author_name?: string; order_id?: string; nlng_order_id?: string; created_at?: string }

type Data = {
  chevron: ChevronOrder[]
  nlng: NlngOrder[]
  loaded: boolean
  error: string
  lastSync: Date | null
  alerts: Alerts | null
  mentions: Mention[]
  markMentionsRead: () => void
  unread: number               // messages to this role not yet read (the old msg-badge)
  refreshUnread: () => void
  reload: () => void
}

const Ctx = createContext<Data | null>(null)

function mentionKey(): string {
  const u = currentUser() as { id?: string } | null
  return 'mention_seen_' + (u?.id || '')
}

export function DataProvider({ children }: { children: ReactNode }) {
  const [chevron, setChevron] = useState<ChevronOrder[]>([])
  const [nlng, setNlng] = useState<NlngOrder[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const [alerts, setAlerts] = useState<Alerts | null>(null)
  const [mentions, setMentions] = useState<Mention[]>([])
  const [unread, setUnread] = useState(0)
  const busy = useRef(false)

  const refreshUnread = useCallback(() => {
    const role = currentUser()?.role || ''
    getJson<{ count: number }>('/api/messages/unread_count?role=' + encodeURIComponent(role))
      .then(d => setUnread(d.count || 0), () => {})
  }, [])

  const reload = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      const [c, n] = await Promise.all([
        getJson<ChevronOrder[]>('/api/orders'),
        getJson<NlngOrder[]>('/api/nlng_orders'),
      ])
      setChevron(c); setNlng(n); setLastSync(new Date()); setError('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      busy.current = false
      setLoaded(true)
    }
    // The bell's extras are best-effort: a failure here never blanks a page.
    getJson<Alerts>('/api/alerts').then(setAlerts, () => {})
    let since = ''
    try { since = localStorage.getItem(mentionKey()) || '' } catch { /* none */ }
    getJson<Mention[] | { data: Mention[] }>('/api/notifications/mentions' + (since ? '?since=' + encodeURIComponent(since) : ''))
      .then(d => setMentions(Array.isArray(d) ? d : d.data || []), () => {})
    refreshUnread()
  }, [refreshUnread])

  useEffect(() => {
    reload()
    const t = setInterval(reload, POLL_MS)
    return () => clearInterval(t)
  }, [reload])

  const markMentionsRead = useCallback(() => {
    try { localStorage.setItem(mentionKey(), new Date().toISOString()) } catch { /* not remembered */ }
    setMentions([])
  }, [])

  return (
    <Ctx.Provider value={{ chevron, nlng, loaded, error, lastSync, alerts, mentions, markMentionsRead, unread, refreshUnread, reload }}>
      {children}
    </Ctx.Provider>
  )
}

export function useOrders(): Data {
  const d = useContext(Ctx)
  if (!d) throw new Error('useOrders outside DataProvider')
  return d
}
