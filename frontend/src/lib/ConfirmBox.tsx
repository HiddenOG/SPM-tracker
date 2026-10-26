import { type ReactNode, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

/*
  "Are you sure?" in the app's own box (ui.css .confirm-box), in place of the
  browser's confirm(). Put on the page body so a panel or menu can't clip it.

  Cancel is focused first, so a stray Enter never deletes anything. Escape or
  a click outside cancels - and Escape stops there, so it doesn't also close
  the order panel (or drawer) behind the box.
*/
export default function ConfirmBox({ title, children, confirmLabel = 'Delete', busyLabel = 'Deleting…',
  busy = false, error = '', onConfirm, onCancel }: {
  title: string
  children: ReactNode
  confirmLabel?: string
  busyLabel?: string
  busy?: boolean
  error?: string
  onConfirm: () => void
  onCancel: () => void
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  useEffect(() => { cancel.current?.focus() }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()                       // the box takes this Escape, nothing behind it
      if (!busy) onCancel()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [busy, onCancel])

  return createPortal(
    <div className="cell-overlay" onMouseDown={e => { if (e.target === e.currentTarget && !busy) onCancel() }}>
      <div className="cell-box confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="cb-hd" aria-describedby="cb-body">
        <h2 id="cb-hd">{title}</h2>
        <p id="cb-body">{children}</p>
        {error && <p className="err-text" role="alert">{error}</p>}
        <div className="cell-btns">
          <button ref={cancel} className="btn ghost" type="button" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn danger" type="button" onClick={onConfirm} disabled={busy}>{busy ? busyLabel : confirmLabel}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
