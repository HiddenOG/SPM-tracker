import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { ApiError, getJson, send } from './api'

/*
  The saved Ask chats, shared by the side menu (which lists them under the
  pages while Ask is open) and the Ask page (which adds to them). Chats are
  shared: everyone who can use Ask sees all of them. The server marks the ones
  this person may rename or delete (can_edit) - their own, or any for an admin.
*/
export type ChatRow = {
  id: string
  title: string
  created_by_name?: string | null
  updated_at: string
  can_edit?: boolean
}

type Store = {
  chats: ChatRow[] | null       // null until first loaded
  error: string
  reload: () => void
  rename: (id: string, title: string) => Promise<void>
  remove: (id: string) => Promise<void>
}

const Ctx = createContext<Store | null>(null)

export function AskChatsProvider({ active, children }: { active: boolean; children: ReactNode }) {
  const [chats, setChats] = useState<ChatRow[] | null>(null)
  const [error, setError] = useState('')

  const reload = useCallback(() => {
    getJson<ChatRow[]>('/api/ask/chats')
      .then(rows => { setChats(rows); setError('') })
      .catch(e => setError(e instanceof ApiError
        ? (e.status === 503 ? `Chat history isn't set up yet. ${e.message}` : e.message)
        : "Couldn't load the chats."))
  }, [])

  // Fetched each time Ask is opened, so chats teammates started since show up;
  // nobody else's page pays for it.
  useEffect(() => { if (active) reload() }, [active, reload])

  const rename = async (id: string, title: string) => {
    await send('PATCH', `/api/ask/chats/${id}`, { title })
    setChats(cs => (cs || []).map(c => c.id === id ? { ...c, title } : c))
  }
  const remove = async (id: string) => {
    await send('DELETE', `/api/ask/chats/${id}`)
    setChats(cs => (cs || []).filter(c => c.id !== id))
    window.dispatchEvent(new CustomEvent('spm-chat-deleted', { detail: id }))
  }

  return <Ctx.Provider value={{ chats, error, reload, rename, remove }}>{children}</Ctx.Provider>
}

export function useAskChats(): Store {
  const s = useContext(Ctx)
  if (!s) throw new Error('useAskChats needs AskChatsProvider')
  return s
}
