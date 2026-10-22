import { Fragment, type ReactNode } from 'react'

/*
  Turning a finished answer into paragraphs. The first sentence is the answer
  and gets the weight; blocks where every line is a bullet become lists; [n]
  becomes a link to source card n.
*/

const BULLET = /^\s*([-•*]|\d+[.)])\s+/

// **label** becomes bold (Sonnet labels list items that way); citations inside work too.
function inline(s: string, onCite: (n: number) => void, key: string): ReactNode {
  const out: ReactNode[] = []
  let last = 0
  for (const m of s.matchAll(/\*\*(.+?)\*\*/g)) {
    const i = m.index ?? 0
    if (i > last) out.push(<Fragment key={`${key}-t${i}`}>{cites(s.slice(last, i), onCite, `${key}-t${i}`)}</Fragment>)
    out.push(<strong key={`${key}-b${i}`}>{cites(m[1], onCite, `${key}-b${i}`)}</strong>)
    last = i + m[0].length
  }
  if (last < s.length) out.push(<Fragment key={`${key}-t${last}`}>{cites(s.slice(last), onCite, `${key}-t${last}`)}</Fragment>)
  return out
}

function cites(s: string, onCite: (n: number) => void, key: string): ReactNode {
  const out: ReactNode[] = []
  let last = 0
  for (const m of s.matchAll(/\[(\d{1,2})\]/g)) {
    const i = m.index ?? 0
    if (i > last) out.push(s.slice(last, i))
    const n = Number(m[1])
    out.push(
      <a key={`${key}-c${i}`} className="cite" href={`#src-${n}`} aria-label={`Source ${n}`}
         onClick={e => { e.preventDefault(); onCite(n) }}>{n}</a>,
    )
    last = i + m[0].length
  }
  if (last < s.length) out.push(s.slice(last))
  return out
}

function lines(ls: string[], onCite: (n: number) => void, key: string): ReactNode {
  return ls.map((l, i) => (
    <Fragment key={`${key}-${i}`}>{i > 0 && <br />}{inline(l, onCite, `${key}-${i}`)}</Fragment>
  ))
}

export function formatAnswer(text: string, onCite: (n: number) => void): ReactNode[] {
  const out: ReactNode[] = []
  let leadDone = false
  text.trim().split(/\n\s*\n/).forEach((block, bi) => {
    const ls = block.split('\n').filter(l => l.trim())
    if (!ls.length) return
    const k = `b${bi}`
    if (ls.every(l => BULLET.test(l))) {
      out.push(<ul key={k}>{ls.map((l, i) => <li key={i}>{inline(l.replace(BULLET, ''), onCite, `${k}-${i}`)}</li>)}</ul>)
      return
    }
    if (!leadDone) {
      leadDone = true
      const m = ls.join(' ').match(/^(.+?[.!?])(\s+|$)([\s\S]*)$/)
      if (m && m[1].length < 260) {
        out.push(<p key={k} className="lead">{inline(m[1], onCite, `${k}-l`)}</p>)
        if (m[3].trim()) out.push(<p key={`${k}-r`}>{inline(m[3].trim(), onCite, `${k}-r`)}</p>)
      } else {
        out.push(<p key={k} className="lead">{lines(ls, onCite, k)}</p>)
      }
      return
    }
    out.push(<p key={k}>{lines(ls, onCite, k)}</p>)
  })
  return out
}
