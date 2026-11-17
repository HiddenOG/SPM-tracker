import { useEffect, useRef, useState } from 'react'
import { getJson, send } from '../../lib/api'

/*
  SPM's selling-price formula, under Ask's text box (admin and procurement
  only). The chip just says "Formula"; clicking opens a formula bar to type
  it like a spreadsheet cell - the list price (GBP or USD) and the weight (kg)
  with numbers, + - × ÷ and brackets. Save keeps it for this person only: Ask's
  gasket prices and the quote form's price check then use it. Reset goes back
  to the default in scripts/price_book.py.
*/

type Src = 'flex_2022' | 'spm_2026'
type Formulas = Record<Src, string>
type Check = { error: string | null; example?: { what: string; price: number; kg: number; usd: number | null; default_usd: number } }

const LISTS: { v: Src; label: string; cur: string; sym: string }[] = [
  { v: 'flex_2022', label: 'Flexitallic 2022', cur: 'GBP', sym: '£' },
  { v: 'spm_2026', label: 'SPM 2026', cur: 'USD', sym: '$' },
]
const same = (a: string, b: string) => a.replace(/\s+/g, '') === b.replace(/\s+/g, '')

export function FormulaChip() {
  const [defaults, setDefaults] = useState<Formulas | null>(null)
  const [saved, setSaved] = useState<Formulas | null>(null)
  const [draft, setDraft] = useState<Formulas | null>(null)
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<Src>('flex_2022')
  const [check, setCheck] = useState<Check | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const box = useRef<HTMLDivElement>(null)
  const bar = useRef<HTMLInputElement>(null)

  useEffect(() => {
    getJson<{ defaults: Formulas; mine: Partial<Formulas> }>('/api/price-book/formula')
      .then(r => { setDefaults(r.defaults); const f = { ...r.defaults, ...r.mine }; setSaved(f); setDraft(f) })
      .catch(() => { /* no price access or not reachable: no chip */ })
  }, [])

  // Close on a click outside or Escape; an unsaved edit is dropped.
  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) close() }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc) }
  })

  // Check the formula as it's typed (a short pause first), with its price for an example line.
  const text = draft?.[tab] ?? ''
  useEffect(() => {
    if (!open || !text) return
    const t = setTimeout(() => {
      // send() turns an answer carrying "error" into a thrown error: that is the formula's problem.
      send<Check>('POST', '/api/price-book/formula/check', { source: tab, text })
        .then(setCheck).catch(e => setCheck({ error: (e as Error).message }))
    }, 350)
    return () => clearTimeout(t)
  }, [open, tab, text])

  if (!defaults || !saved || !draft) return null
  const custom = (f: Formulas) => LISTS.some(l => !same(f[l.v], defaults[l.v]))
  const dirty = LISTS.some(l => !same(draft[l.v], saved[l.v]))

  function close() { setOpen(false); setDraft(saved); setMsg(null); setCheck(null) }
  async function save(next: Formulas) {
    if (!defaults) return
    setBusy(true); setMsg(null)
    try {
      const r = await send<{ mine: Partial<Formulas> }>('PUT', '/api/price-book/formula', { formula: next })
      const f = { ...defaults, ...r.mine }
      setSaved(f); setDraft(f)
      setMsg({ ok: true, text: custom(f) ? 'Saved - your prices use this formula.' : 'Back to the default formula.' })
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const list = LISTS.find(l => l.v === tab)!
  const ex = check?.example

  return (
    <div className="fx" ref={box}>
      <button type="button" className={`fx-chip${custom(saved) ? ' mine' : ''}`} aria-expanded={open}
              title="Selling-price formula for gasket prices"
              onClick={() => (open ? close() : (setOpen(true), setTimeout(() => bar.current?.focus(), 60)))}>
        <span className="fx-f" aria-hidden="true">ƒ</span>Formula
        {custom(saved) && <span className="fx-tag">yours</span>}
      </button>
      {open && (
        <div className="fx-panel" role="dialog" aria-label="Selling-price formula">
          <div className="fx-head">
            <b>Selling-price formula</b>
            <div className="seg" role="radiogroup" aria-label="Price list">
              {LISTS.map(l => (
                <button key={l.v} type="button" role="radio" aria-checked={tab === l.v}
                        className={tab === l.v ? 'on' : ''} onClick={() => { setTab(l.v); setCheck(null) }}>{l.label}</button>
              ))}
            </div>
          </div>
          <div className={`fx-bar${check?.error ? ' bad' : ''}`}>
            <span className="fx-f" aria-hidden="true">ƒx</span>
            <input ref={bar} className="mono" value={text} spellCheck={false} aria-label={`${list.label} formula`}
                   onChange={e => { const v = e.target.value; setDraft(d => d && { ...d, [tab]: v }); setMsg(null) }}
                   onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); if (dirty && !check?.error) save(draft) } }} />
          </div>
          <p className="fx-hint">
            <b>{list.cur}</b> is the list price, <b>kg</b> the weight. Use numbers, + − × ÷ (or * / x) and brackets.
            {!same(text, defaults[tab]) && <> Default: <span className="mono">{defaults[tab]}</span></>}
          </p>
          <p className={check?.error ? 'fx-err' : 'fx-eg'} aria-live="polite">
            {check?.error
              ? check.error
              : ex && <>e.g. {ex.what} ({list.sym}{ex.price}, {ex.kg} kg) → <b>${ex.usd?.toFixed(2) ?? '—'}</b>
                  {ex.usd !== ex.default_usd && <> (default ${ex.default_usd.toFixed(2)})</>}</>}
          </p>
          <div className="fx-foot">
            {msg && <span className={msg.ok ? 'fx-ok' : 'fx-err'}>{msg.text}</span>}
            <span className="sp" />
            <button type="button" className="btn ghost" disabled={busy || !custom(draft)}
                    onClick={() => save({ ...defaults })}>Reset to default</button>
            <button type="button" className="btn primary" disabled={busy || !dirty || !!check?.error} onClick={() => save(draft)}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </div>
          <p className="fx-note">Only your own prices change - in Ask and the quote form's price check.</p>
        </div>
      )}
    </div>
  )
}
