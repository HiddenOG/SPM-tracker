import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { clean, pasteHtml } from './rich'

/*
  The rich-text pieces the quotation and purchase-order forms share, as they
  shared _ceClean and the format bar in web/script.js.
*/

// '' is Default: no colour, so the text follows the theme (light in dark mode,
// dark in light mode, black on paper). It used to be a near-black, which
// vanished in dark mode.
const FMT_COLOURS: [string, string][] = [['', 'Default'], ['#CC0000', 'Red'], ['#0055BB', 'Blue'], ['#CC6600', 'Orange'], ['#006620', 'Green']]
const MARK = '#010203'   // a colour no one picks, used to find and unwrap a Default

/** Take the colour off the selected text, then save the cell. */
function plainColour() {
  document.execCommand('foreColor', false, MARK)
  const sel = window.getSelection()
  let node: Node | null = sel?.anchorNode ?? null
  while (node && node.nodeType !== 1) node = node.parentNode
  const cell = (node as Element | null)?.closest?.('.qf-ce')
  if (!cell) return
  cell.querySelectorAll(`font[color="${MARK}"], span[style*="rgb(1, 2, 3)"]`).forEach(el => el.replaceWith(...el.childNodes))
  cell.dispatchEvent(new Event('input', { bubbles: true }))
}

/** A rich-text cell: uncontrolled, so typing never jumps the cursor. Paste is cleaned. */
export function Rich({ html, onChange, placeholder, className }: { html: string; onChange: (h: string) => void; placeholder: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  // Saved text is cleaned as it's shown too, so lines saved before neutral
  // colours were dropped (near-black from emails) read in dark mode at once.
  useLayoutEffect(() => { if (ref.current) ref.current.innerHTML = clean(html) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div ref={ref} className={`qf-ce ${className || ''}`} contentEditable suppressContentEditableWarning data-ph={placeholder}
         onInput={e => onChange(e.currentTarget.innerHTML)}
         onPaste={e => {
           e.preventDefault()
           const html = pasteHtml(e)
           let ok = false
           try { ok = document.execCommand('insertHTML', false, html) } catch { ok = false }
           if (!ok) { try { document.execCommand('insertText', false, e.clipboardData.getData('text/plain') || '') } catch { /* nothing to do */ } }
           onChange(e.currentTarget.innerHTML)
         }} />
  )
}

/** The floating bold / italic / colour bar over a selection inside a rich cell. */
export function FormatBar() {
  const bar = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  useEffect(() => {
    const onSel = () => {
      const sel = window.getSelection()
      if (!sel || sel.isCollapsed || !sel.toString().trim()) return setPos(null)
      let node: Node | null = sel.anchorNode
      while (node && node.nodeType !== 1) node = node.parentNode
      if (!(node as Element | null)?.closest?.('.qf-ce')) return setPos(null)
      const rect = sel.getRangeAt(0).getBoundingClientRect()
      const h = bar.current?.offsetHeight || 34, w = bar.current?.offsetWidth || 220
      let top = rect.top - h - 8
      if (top < 8) top = rect.bottom + 6
      setPos({ top, left: Math.min(rect.left, window.innerWidth - w - 8) })
    }
    document.addEventListener('selectionchange', onSel)
    return () => document.removeEventListener('selectionchange', onSel)
  }, [])
  // execCommand fires an input event on the cell, which saves the change.
  const keep = (e: React.MouseEvent) => e.preventDefault()
  return (
    <div ref={bar} className="fmt-bar" style={pos ? { top: pos.top, left: pos.left } : { visibility: 'hidden', top: -999, left: -999 }}>
      <button type="button" onMouseDown={keep} onClick={() => document.execCommand('bold')} title="Bold"><b>B</b></button>
      <button type="button" onMouseDown={keep} onClick={() => document.execCommand('italic')} title="Italic"><i>I</i></button>
      <span className="sep" />
      {FMT_COLOURS.map(([c, name]) => (
        <button key={name} type="button" className={`clr${c ? '' : ' default'}`} style={c ? { background: c } : undefined}
                title={c ? name : 'Default colour (follows light or dark mode)'} aria-label={c ? name : 'Default colour'}
                onMouseDown={keep} onClick={() => (c ? document.execCommand('foreColor', false, c) : plainColour())} />
      ))}
    </div>
  )
}
