/*
  The side menu. While pages move over one at a time, each entry is either a
  route in this app or a link back into the old one at "/#page".

  ROLE_PAGES is a copy of the one in web/script.js; keep the two in step. The
  server still enforces access on every request; this only decides what is shown.
*/
export type NavItem = {
  key: string
  label: string
  icon: string            // SVG path data, 24x24, stroked
  to?: string             // a route here; without one it opens in the classic app
  section?: string        // heading shown above this item
}

// 'suppliers' is hidden while its listener is paused; see web/script.js.
// 'products' was removed from every role: Inventory is Stock only.
const ROLE_PAGES: Record<string, string[]> = {
  admin:       ['dashboard', 'ask', 'orders', 'delays', 'reports', 'quotes', 'purchase-orders', 'stock', 'settings', 'team', 'messages'],
  procurement: ['dashboard', 'ask', 'orders', 'delays', 'reports', 'quotes', 'purchase-orders', 'stock', 'settings', 'messages'],
  expeditor:   ['dashboard', 'ask', 'orders', 'delays', 'stock', 'settings', 'messages'],
  warehouse:   ['dashboard', 'orders', 'delays', 'stock', 'settings', 'messages'],
  accounts:    ['dashboard', 'ask', 'orders', 'delays', 'reports', 'stock', 'settings', 'messages'],
}

export const NAV: NavItem[] = [
  { key: 'dashboard', label: 'Dashboard', to: '/dashboard', section: 'Workspace',
    icon: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z' },
  { key: 'ask', label: 'Ask', to: '/ask',
    icon: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-3.5-3.5M8.5 11h5M11 8.5v5' },
  { key: 'orders', label: 'Orders', to: '/orders',
    icon: 'M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 3h6v4H9zM9 12h6M9 16h4' },
  { key: 'suppliers', label: 'Suppliers',
    icon: 'M5 17H3a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v3M9 11h14v10H9z' },
  { key: 'stock', label: 'Stock', to: '/stock',
    icon: 'M3 6h18M3 12h18M3 18h18' },
  { key: 'delays', label: 'Delays', to: '/delays',
    icon: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 3' },
  { key: 'messages', label: 'Messages', to: '/messages', section: 'Communication',
    icon: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' },
  { key: 'reports', label: 'Reports', to: '/reports', section: 'Reporting',
    icon: 'M18 20V10M12 20V4M6 20v-6' },
  { key: 'quotes', label: 'Quotes', to: '/quotes',
    icon: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8' },
  { key: 'purchase-orders', label: 'Purchase orders', to: '/purchase-orders',
    icon: 'M6 2h9l5 5v15a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM15 2v5h5M9 13h6M9 17h4' },
  { key: 'settings', label: 'Settings', to: '/settings', section: 'Account',
    icon: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1' },
  { key: 'team', label: 'Team', to: '/team', section: 'Admin',
    icon: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75' },
]

/** Whether a role's menu has this page (the server checks again on every request). */
export function canOpen(role: string | undefined, key: string): boolean {
  return (ROLE_PAGES[role || ''] || ROLE_PAGES.procurement).includes(key)
}

/** The menu for a role, with each section heading kept on its first visible item. */
export function navFor(role: string | undefined): NavItem[] {
  const allowed = new Set(ROLE_PAGES[role || ''] || ROLE_PAGES.procurement)
  let section: string | undefined
  const out: NavItem[] = []
  for (const n of NAV) {
    if (n.section) section = n.section
    if (!allowed.has(n.key)) continue
    out.push({ ...n, section })
    section = undefined
  }
  return out
}
