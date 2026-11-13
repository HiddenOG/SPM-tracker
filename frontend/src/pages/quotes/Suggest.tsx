import { useMemo, useState } from 'react'

/*
  Suggestions for a quote field from what was typed in earlier quotes: the
  signed-in person's own entries first, then the rest of the team's. Typing
  narrows the list; it never fills a field by itself.
*/

export type Option = { v: string; mine: boolean }
export type Suggestions = Record<string, Option[]>

// The quote fields that offer what was used before.
export const SUGGEST_FIELDS = ['prepared_by', 'recipient_name', 'recipient_dept', 'recipient_company', 'recipient_email',
  'recipient_tel', 'subject', 'lead_time', 'delivery_dest', 'country_import', 'manufacturer', 'weight'] as const

const SHOWN = 8
const key = (s: string) => s.trim().replace(/\s+/g, ' ').toLowerCase()

/**
  Each field's past values, best first: the person's own (most recent first),
  then the team's (most used first). quotes is newest first, as the API sends it.
*/
export function buildSuggestions(quotes: Record<string, unknown>[], me: { name?: string; email?: string } | null): Suggestions {
  const mine = (q: Record<string, unknown>) => {
    const by = key(String(q.created_by || ''))
    return !!by && (by === key(me?.name || '') || by === key(me?.email || ''))
  }
  const out: Suggestions = {}
  for (const f of SUGGEST_FIELDS) {
    const seen = new Map<string, { v: string; mine: boolean; uses: number; at: number }>()
    quotes.forEach((q, at) => {
      const v = String(q[f] ?? '').trim()
      if (!v) return
      const k = key(v)
      const hit = seen.get(k)
      if (hit) { hit.uses++; if (mine(q)) hit.mine = true }
      else seen.set(k, { v, mine: mine(q), uses: 1, at })
    })
    out[f] = [...seen.values()]
      .sort((a, b) => (a.mine !== b.mine ? (a.mine ? -1 : 1) : a.mine ? a.at - b.at : b.uses - a.uses || a.at - b.at))
      .map(({ v, mine }) => ({ v, mine }))
  }
  return out
}

type Props = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> & {
  value: string
  onChange: (v: string) => void
  options: Option[]
  onPick?: (v: string) => void   // a suggestion was chosen (not typed)
}

/** A quote field with a list of earlier entries under it. */
export function SuggestInput({ value, onChange, options, onPick, className, ...rest }: Props) {
  const [open, setOpen] = useState(false)
  const [at, setAt] = useState(-1)
  const list = useMemo(() => {
    const q = key(value)
    return options.filter(o => key(o.v) !== q && (!q || key(o.v).includes(q))).slice(0, SHOWN)
  }, [value, options])
  const show = open && list.length > 0
  const pick = (v: string) => { onChange(v); onPick?.(v); setOpen(false); setAt(-1) }
  return (
    <div className="sg">
      <input {...rest} className={className || 'qf-inp'} value={value} autoComplete="off"
             role="combobox" aria-expanded={show} aria-autocomplete="list"
             onChange={e => { onChange(e.target.value); setOpen(true); setAt(-1) }}
             onFocus={() => setOpen(true)}
             onBlur={() => { setOpen(false); setAt(-1) }}
             onKeyDown={e => {
               if (!show) { if (e.key === 'ArrowDown' && list.length) { setOpen(true); e.preventDefault() } return }
               if (e.key === 'ArrowDown') { setAt(i => (i + 1) % list.length); e.preventDefault() }
               else if (e.key === 'ArrowUp') { setAt(i => (i <= 0 ? list.length - 1 : i - 1)); e.preventDefault() }
               else if (e.key === 'Enter' && at >= 0) { pick(list[at].v); e.preventDefault() }
               else if (e.key === 'Escape') { setOpen(false); setAt(-1) }
             }} />
      {show && (
        <ul className="sg-list" role="listbox">
          {list.map((o, i) => (
            <li key={o.v} role="option" aria-selected={i === at} className={i === at ? 'on' : undefined}
                onMouseDown={e => { e.preventDefault(); pick(o.v) }} onMouseEnter={() => setAt(i)}>
              <span>{o.v}</span><em>{o.mine ? 'yours' : 'team'}</em>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
