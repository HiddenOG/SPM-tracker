import { send } from '../../lib/api'
import './pricebook.css'

/*
  The gasket price lists, as the quote form shows them under each line.
  The server reads each description into its fields and looks them up
  (scripts/price_book.py); a price or weight here only ever comes from a list.
  Edge cases carry a second AI look (ai_check) that confirms or doubts the match.
*/

export type Match = {
  source: string; page?: string; label: string; rating?: string
  price: number | null; currency: string; poa: boolean; weight_kg: number | null
  note?: string | null; quote_usd?: number | null
}
export type Spec = Record<string, unknown>
export type Result = {
  spec: Spec; status: 'found' | 'not_listed'; matches: Match[]; reason?: string; notes?: string[]
  ai_check?: { verdict: 'ok' | 'doubt'; comment: string }
}

export async function checkLines(lines: string[]): Promise<(Result | null)[]> {
  const r = await send<{ results: (Result | null)[] }>('POST', '/api/price-book/check', { lines })
  return r.results
}

const SYM: Record<string, string> = { GBP: '£', USD: '$', NGN: '₦' }
export const listPrice = (m: Match) =>
  m.poa ? 'Price on application' : m.price == null ? '—' : (SYM[m.currency] || m.currency + ' ') + m.price.toFixed(2)
export const kg = (v: number | null | undefined) => (v == null ? '—' : `${v} kg`)

// How the reader understood a line, as short chips: "Spiral wound", "8\"", "Class 150"...
const KIND: Record<string, string> = {
  spiral_wound: 'Spiral wound', ring_joint: 'Ring joint', isolation_kit: 'Isolation kit', frg: 'FRG',
  sheet: 'Sheet', tape: 'Tape', packing: 'Packing', paste: 'Paste', unspecified: 'Type not stated', other: 'Other item',
}
const RING: Record<string, string> = { carbon_steel: 'CS', '304': '304', '316': '316' }
const ASME = new Set(['150', '300', '400', '600', '900', '1500', '2500'])  // other numbers are API psi
export function chips(s: Spec): string[] {
  const out: string[] = []
  const v = (k: string) => (s[k] == null || s[k] === 'unknown' || s[k] === 'none' ? '' : String(s[k]))
  if (v('kind')) out.push(KIND[v('kind')] || v('kind'))
  if (v('product_name')) out.push(v('product_name') === 'other' ? 'Unlisted product' : v('product_name'))
  if (v('thickness_mm')) out.push(`${v('thickness_mm')} mm thick`)
  if (v('width_mm')) out.push(s.kind === 'packing' ? `${v('width_mm')} mm section` : `${v('width_mm')} mm wide`)
  if (v('length_m')) out.push(`${v('length_m')} m`)
  if (v('sheet_size')) out.push(`${v('sheet_size')} mm sheet`)
  if (v('style')) out.push(v('style'))
  if (v('rtj_style') && !v('ring_no')) out.push(`Style ${v('rtj_style')}`)
  if (v('ring_no')) out.push(v('ring_no'))
  if (v('kit_type')) out.push(`Isoflex ${v('kit_type')}`)
  if (v('size_in')) out.push(`${v('size_in')}"`)
  if (v('size_dn')) out.push(`DN${v('size_dn')}`)
  for (const r of (s.ratings as string[]) || [])
    out.push(/^PN/i.test(r) ? r : ASME.has(r) ? `Class ${r}` : `${r} psi`)
  if (v('grade')) out.push(v('grade') === 'other' ? 'Other alloy' : v('grade') === '304_or_316' ? '304 or 316 SS' : v('grade') + ' SS')
  if (v('rtj_material')) out.push(v('rtj_material') === 'soft_iron' ? 'Soft iron' : v('rtj_material'))
  if (v('filler')) out.push(v('filler') === 'graphite' ? 'Graphite filler' : v('filler') === 'ptfe' ? 'PTFE filler' : 'Other filler')
  if (v('outer_ring') && RING[v('outer_ring')]) out.push(`${RING[v('outer_ring')]} outer ring`)
  if (v('inner_ring') && RING[v('inner_ring')]) out.push(`${RING[v('inner_ring')]} inner ring`)
  if (v('rtj_shape')) out.push(v('rtj_shape'))
  return out
}

/** The second look at an edge case: a doubt stands out, a confirmation is quiet. */
function AiCheck({ check }: { check?: Result['ai_check'] }) {
  if (!check) return null
  return check.verdict === 'doubt'
    ? <div className="pb-doubt"><span aria-hidden="true">⚠</span> Second check: {check.comment}</div>
    : <div className="pb-note">Second check: {check.comment}</div>
}

/** How the line was read, so a misreading shows before its price is trusted. */
function ReadAs({ spec }: { spec: Spec }) {
  const c = chips(spec)
  return c.length ? <div className="pb-read">Read as: {c.join(' · ')}</div> : null
}

/** One quote line's answer, under its description. */
export function PriceTag({ result, onUse }: { result: Result | 'loading' | null; onUse?: (m: Match) => void }) {
  if (result === 'loading') return <div className="pb-tag busy">Checking the price lists…</div>
  if (!result) return null
  if (result.status !== 'found') {
    return (
      <div className="pb-tag miss">
        <span className="pb-mark" aria-hidden="true">✗</span>{result.reason || 'Not on the price lists'} — ask Flexitallic
        <ReadAs spec={result.spec} />
        <AiCheck check={result.ai_check} />
      </div>
    )
  }
  return (
    <div className="pb-tag hit">
      <ReadAs spec={result.spec} />
      {result.matches.map((m, i) => (
        <div key={i} className="pb-hit">
          <span className="pb-mark" aria-hidden="true">✓</span>
          <span className="pb-what">{m.label}{m.rating ? ` · class ${m.rating}` : ''}</span>
          <b className="mono">{listPrice(m)}</b>
          <span className="mono pb-kg">{kg(m.weight_kg)}</span>
          {onUse && (
            <button type="button" className="link" disabled={m.quote_usd == null} onClick={() => onUse(m)}
                    title={m.quote_usd == null ? 'Waiting for the markup formula' : `Use $${m.quote_usd.toFixed(2)} as the unit price`}>Use</button>
          )}
          {m.note && <span className="pb-note">{m.note}</span>}
        </div>
      ))}
      {!!result.notes?.length && <div className="pb-note">{result.notes.join(' · ')}</div>}
      <AiCheck check={result.ai_check} />
    </div>
  )
}
