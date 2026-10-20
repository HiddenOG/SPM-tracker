/*
  The bar under every table, as the old app's renderPgBar() draws it:
  "← Prev  Page 1 of 2  Next →" on the left, "100 rows  177 records" on the right.
*/
export default function Pager({ page, total, perPage, onPage }: {
  page: number; total: number; perPage: number; onPage: (p: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / perPage))
  return (
    <div className="pager">
      <div className="pg-l">
        <button type="button" className="pg-btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Prev</button>
        <span>Page <strong>{page}</strong> of <strong>{pages}</strong></span>
        <button type="button" className="pg-btn" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next →</button>
      </div>
      <div className="pg-r">
        <span>{perPage} rows</span>
        <span className="pg-count">{total} record{total !== 1 ? 's' : ''}</span>
      </div>
    </div>
  )
}
