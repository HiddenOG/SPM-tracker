/*
  The login is shared with the old app. It keeps the bearer header in
  localStorage under spm_auth and the user under spm_user; both apps run on the
  same site, so reading them here means nobody signs in twice while pages move
  over.
*/
export type User = { id?: string; name?: string; email?: string; role?: string }

function read(key: string): string {
  try { return localStorage.getItem(key) || '' } catch { return '' }
}

export function authHeader(): string {
  return read('spm_auth')
}

/** The signed-in person as they really are, whatever is being previewed. */
export function realUser(): User | null {
  try { return JSON.parse(read('spm_user') || 'null') } catch { return null }
}

/*
  "Preview as this role" (the Team page): an admin sees the menu and pages as
  another role would. Like the old app's, it only changes what this screen
  shows - the server still checks the real login on every request. Kept in
  sessionStorage, so it ends with the tab.
*/
export type Preview = { role: string; name: string }
export function previewing(): Preview | null {
  if (realUser()?.role !== 'admin') return null
  try { return JSON.parse(sessionStorage.getItem('spm_preview') || 'null') } catch { return null }
}
export function startPreview(p: Preview): void {
  try { sessionStorage.setItem('spm_preview', JSON.stringify(p)) } catch { /* not kept */ }
}
export function endPreview(): void {
  try { sessionStorage.removeItem('spm_preview') } catch { /* nothing to clear */ }
}

/** The person as the screen should treat them - with a previewed role, if any. */
export function currentUser(): User | null {
  const u = realUser()
  const p = previewing()
  return u && p ? { ...u, role: p.role } : u
}

/**
  Signing in still happens on the old app's login screen. It is sent there as
  /#ask (say), and the old app opens that page after login - which for a page
  that lives here means coming straight back.
*/
export function goToLogin(): void {
  const page = window.location.pathname.replace(/^\/app\/?/, '').split('/')[0]
  window.location.href = page ? `/#${page}` : '/'
}

/**
  The server refused the login (expired or revoked). Forget it before going
  to sign in: the old app launches on any stored login without checking it,
  so leaving a dead one behind would send the person straight back here, and
  round and round.
*/
export function sessionEnded(): void {
  endPreview()
  try {
    localStorage.removeItem('spm_auth')
    localStorage.removeItem('spm_user')
  } catch { /* storage blocked: nothing to clear */ }
  goToLogin()
}

export function signOut(): void {
  endPreview()
  try {
    localStorage.removeItem('spm_auth')
    localStorage.removeItem('spm_user')
  } catch { /* storage blocked: nothing to clear */ }
  window.location.href = '/'
}

/** Apply the old app's theme choice: light, dark, or auto (follow the OS). */
export function applyTheme(): void {
  const t = read('spm_theme') || 'auto'
  const root = document.documentElement
  if (t === 'light' || t === 'dark') root.dataset.theme = t
  else delete root.dataset.theme
}

export type Theme = 'light' | 'dark' | 'auto'
export function getTheme(): Theme {
  const t = read('spm_theme')
  return t === 'light' || t === 'dark' ? t : 'auto'
}
export function setTheme(t: Theme): void {
  try { localStorage.setItem('spm_theme', t) } catch { /* not remembered */ }
  // A short cross-fade (ui.css .theme-fade), unless motion is turned down.
  const root = document.documentElement
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    root.classList.add('theme-fade')
    window.setTimeout(() => root.classList.remove('theme-fade'), 400)
  }
  applyTheme()
  window.dispatchEvent(new Event('spm-theme'))
}

export function initials(u: User | null): string {
  const n = (u?.name || u?.email || '').trim()
  if (!n) return '·'
  const parts = n.split(/[\s@._-]+/).filter(Boolean)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || n[0].toUpperCase()
}
