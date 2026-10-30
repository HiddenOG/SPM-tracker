import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { type Col, MAX_ADDED, REMOVABLE, isAdded, labelOf } from './quoteColumns'

/*
  One line-item heading with its ▾ menu: rename, move, add a column either
  side, remove. The menu sits on the page body, as the table's scroll box
  would clip it, and closes on a click outside, Escape or a scroll.
*/

type Action = 'left' | 'right' | 'add-left' | 'add-right' | 'add-price' | 'remove'

export default function ColumnHead({ col, index, count, added, canPrice = false, suffix = '', startRenaming = false, onRenamed, onRename, onAction }: {
  col: Col
  index: number          // position among the columns, for Move left/right
  count: number
  added: number          // added columns on the quote, for the cap
  canPrice?: boolean     // no second price yet, and room for its two columns
  suffix?: string        // the currency on money headings: " ($)"
  startRenaming?: boolean  // a column just added opens with its name ready to type
  onRenamed?: () => void
  onRename: (label: string) => void
  onAction: (a: Action) => void
}) {
  const [open, setOpen] = useState(false)
  const [renaming, setRenaming] = useState(startRenaming)
  const [draft, setDraft] = useState(startRenaming ? labelOf(col) : '')
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLInputElement>(null)

  useLayoutEffect(() => {
    if (!open || !btn.current) return
    const r = btn.current.getBoundingClientRect()
    // Keep the menu on screen when the heading is near the right edge.
    setPos({ top: r.bottom + 6, left: Math.max(8, Math.min(r.right - 200, window.innerWidth - 208)) })
  }, [open])

  useEffect(() => {
    if (!open) return
    const out = (e: MouseEvent) => {
      if (!menu.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) setOpen(false)
    }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); btn.current?.focus() } }
    const shut = () => setOpen(false)
    document.addEventListener('mousedown', out)
    window.addEventListener('keydown', key, true)
    window.addEventListener('scroll', shut, true)
    window.addEventListener('resize', shut)
    return () => {
      document.removeEventListener('mousedown', out)
      window.removeEventListener('keydown', key, true)
      window.removeEventListener('scroll', shut, true)
      window.removeEventListener('resize', shut)
    }
  }, [open])

  useEffect(() => { if (renaming) input.current?.select() }, [renaming])

  const startRename = () => { setOpen(false); setDraft(labelOf(col)); setRenaming(true) }
  const commit = () => { setRenaming(false); onRename(draft.trim().slice(0, 40)); onRenamed?.() }
  const act = (a: Action) => { setOpen(false); onAction(a) }
  const full = added >= MAX_ADDED
  const removable = isAdded(col.key) || REMOVABLE.includes(col.key)

  if (renaming) {
    return (
      <input ref={input} className="qf-col-name" value={draft} maxLength={40} aria-label="Column name"
             onChange={e => setDraft(e.target.value)} onBlur={commit}
             onKeyDown={e => {
               if (e.key === 'Enter') { e.preventDefault(); commit() }
               if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setRenaming(false); onRenamed?.() }
             }} />
    )
  }

  return (
    <span className="qf-col">
      <span className="qf-col-lbl" onDoubleClick={startRename} title="Double-click to rename">{labelOf(col)}{suffix}</span>
      <button ref={btn} type="button" className={`qf-col-btn${open ? ' on' : ''}`} aria-haspopup="menu" aria-expanded={open}
              aria-label={`Column options for ${labelOf(col)}`} onClick={() => setOpen(o => !o)}>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </button>
      {open && pos && createPortal(
        <div ref={menu} className="qf-col-menu" role="menu" style={{ top: pos.top, left: pos.left }}>
          <button type="button" role="menuitem" onClick={startRename}>Rename</button>
          <button type="button" role="menuitem" disabled={index === 0} onClick={() => act('left')}>Move left</button>
          <button type="button" role="menuitem" disabled={index === count - 1} onClick={() => act('right')}>Move right</button>
          <hr />
          <button type="button" role="menuitem" disabled={full} onClick={() => act('add-left')}>Add column to the left</button>
          <button type="button" role="menuitem" disabled={full} onClick={() => act('add-right')}>Add column to the right</button>
          {canPrice && (
            <button type="button" role="menuitem" onClick={() => act('add-price')}
                    title="A second unit price and its total, side by side - e.g. FCA and DDP">Add a second price here</button>
          )}
          {full && <p className="qf-col-note">Up to {MAX_ADDED} added columns fit on the PDF.</p>}
          {removable && <><hr /><button type="button" role="menuitem" className="danger" onClick={() => act('remove')}>{col.kind ? 'Remove second price' : 'Remove column'}</button></>}
        </div>,
        document.body,
      )}
    </span>
  )
}

export type { Action }
