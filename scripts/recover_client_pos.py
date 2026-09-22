"""
recover_client_pos.py - restore client POs that Flexitallic's system cut off.

Flexitallic truncates the purchase-order reference on its acknowledgement at
about 40 characters, so a sales order raised against seven client POs arrives
reading "S.P.M.-2046-MOBIL-4501846645" and the table only ever learned about
the first one. 88 of 213 references in scratch_flexitallic_history sat at that
length. SPM's own PURCHASE ORDER email to Flexitallic is not truncated, and its
PDF carries a P.O. No. on every line, so together they are the source of truth
for which client POs an order carried.

Matching an email to a row is the hard part, because SPM's sequence number is
NOT unique: ref 2016 was an NLNG order on 6 Jan 2025 and an unrelated Chevron
order on 23 Jan. Joining on the number alone attaches other orders' POs. So an
email is only accepted for a row when:

  1. it carries the same SPM sequence number,
  2. it was sent before the sales order was acknowledged (the PO precedes the
     SO; a few days' slack covers clock and forwarding skew), and
  3. every client PO already on the row appears in the email. A truncated
     reference is a prefix of the full one, so the real order always passes
     and an unrelated order that reused the number does not.

Rows with no client PO at all cannot use rule 3; they are reported separately
and only applied with --include-unanchored.

Candidate POs are also screened before being added:
  - a PO one digit longer or shorter than one already on the row is a typo,
    not a new order (420003104 beside 4200083104); same-length neighbours are
    real, because buyers number POs in sequence
  - two rows of the same SPM order sharing one email cannot say which sales
    order a new PO belongs to, so those are reported, not applied

The matching functions here are shared with flexitallic_history_listener.py,
which runs the same recovery live for each new acknowledgement.

Only ever ADDS to customer_po_numbers; never removes. Safe to re-run.

    python scripts/recover_client_pos.py                        # dry run
    python scripts/recover_client_pos.py --apply
    python scripts/recover_client_pos.py --apply --include-unanchored
    python scripts/recover_client_pos.py --report=recovered.json
"""

import os
import re
import sys
import json
import email as email_mod
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

from db import get_client
from config import SPM_SENDER
from email_utils import decode_mime_words
from backfill_flexitallic_history import CUSTPO, SPMSEQ, CUSTOMER, _norm_customer
from backfill_flexitallic_po_values import (
    PURCHASE_ORDER_RE, parse_po_pdf, _pdf_bytes, _reconnect, _fetch_with_retry,
)

# An SPM PO goes out before Flexitallic acknowledges it. Allow a little slack
# the other way for forwarding and timezone skew, and ignore emails so old they
# cannot plausibly be the order behind this acknowledgement.
SLACK_AFTER = timedelta(days=3)
MAX_LEAD = timedelta(days=180)


def _day(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)[:19])


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _norm_pos(values) -> set[str]:
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except Exception:
            values = [values]
    return {str(v).upper() for v in (values or [])}


def is_typo_of(po: str, other: str) -> bool:
    """A dropped or doubled digit: 420003104 against 4200083104.

    Numbers of the SAME length that differ by a digit or two are not typos -
    buyers issue POs in sequence, so 4200085124 and 4200085125 are two genuine
    orders raised back to back. An earlier version treated those as typos and
    would have discarded real POs, three of which exist elsewhere in our data.
    """
    return len(po) != len(other) and _edit_distance(po, other) == 1


def drop_typos(pos) -> set[str]:
    """Remove any PO that is a dropped/doubled-digit copy of a longer one.

    When the pair differs in length the canonical form is the longer number:
    NLNG and Mobil POs are ten digits, and the short version is the slip.
    """
    pos = {str(p).upper() for p in pos}
    return {p for p in pos
            if not any(is_typo_of(p, q) and len(q) > len(p) for q in pos)}


def email_record(uid, msg, internaldate, tmpdir: str) -> dict | None:
    """One SPM PURCHASE ORDER email as a matching candidate, or None.

    Client POs come from the subject AND the PO number column of the attached
    PDF: a subject typed by hand often stops short (ref 2015 names 2 POs, its
    PDF lists 8), the PDF's line items do not.
    """
    subject = re.sub(r"\s+", " ", decode_mime_words(msg.get("Subject", "") or ""))
    if re.match(r"^\s*(re|fw|fwd)\s*:", subject, re.IGNORECASE):
        return None
    if not PURCHASE_ORDER_RE.search(subject):
        return None
    seq = SPMSEQ.search(subject)
    if not seq:
        return None

    subject_pos = {p.upper() for p in CUSTPO.findall(subject)}
    pdf_pos: set[str] = set()
    raw = _pdf_bytes(msg)
    if raw:
        path = os.path.join(tmpdir, f"{uid}.pdf")
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            for line in parse_po_pdf(path).get("po_line_items") or []:
                pdf_pos |= {p.upper() for p in CUSTPO.findall(str(line.get("customer_po") or ""))}
        except Exception as exc:
            print(f"  [warn] PDF unreadable on UID {uid}: {exc}")

    if not (subject_pos | pdf_pos):
        return None
    cu = CUSTOMER.search(subject)
    return {
        "uid": uid,
        "ref": seq.group(1),
        "date": _day(internaldate),
        "subject": subject,
        "customer": _norm_customer(cu.group(1) if cu else None),
        "subject_pos": subject_pos,
        "pdf_pos": pdf_pos,
        "pos": subject_pos | pdf_pos,
    }


def collect_spm_orders(tmpdir: str) -> dict[str, list[dict]]:
    """Every SPM PURCHASE ORDER email, grouped by SPM sequence number."""
    imap = _reconnect()
    by_ref: dict[str, list[dict]] = defaultdict(list)
    try:
        uids = imap.search(["X-GM-RAW",
                            f'from:{SPM_SENDER} subject:"purchase order" after:2023/06/01'])
        print(f"  {len(uids)} SPM purchase-order email(s) to inspect")
        BATCH = 15
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            fetched, imap = _fetch_with_retry(imap, chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                data = fetched.get(uid)
                if not data:
                    continue
                msg = email_mod.message_from_bytes(data[b"RFC822"])
                rec = email_record(uid, msg, data[b"INTERNALDATE"], tmpdir)
                if rec:
                    by_ref[rec["ref"]].append(rec)
            if (i // BATCH) % 10 == 0:
                print(f"     {min(i + BATCH, len(uids))}/{len(uids)}", flush=True)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return by_ref


def emails_for_ref(imap, ref: str, tmpdir: str) -> tuple[list[dict], object]:
    """SPM purchase-order emails for ONE sequence number - the live path.

    Returns (candidates, imap) so the caller keeps whatever connection survived.
    """
    uids = imap.search(["X-GM-RAW",
                        f'from:{SPM_SENDER} subject:"purchase order" subject:{ref}'])
    out = []
    for i in range(0, len(uids), 10):
        chunk = uids[i:i + 10]
        fetched, imap = _fetch_with_retry(imap, chunk, ["RFC822", "INTERNALDATE"])
        for uid in chunk:
            data = fetched.get(uid)
            if not data:
                continue
            rec = email_record(uid, email_mod.message_from_bytes(data[b"RFC822"]),
                               data[b"INTERNALDATE"], tmpdir)
            # Gmail's subject search is a word match, so "3124" also finds a
            # subject that merely quotes 3124 elsewhere. Keep only true matches.
            if rec and rec["ref"] == ref:
                out.append(rec)
    return out, imap


def plan_row(row: dict, candidates: list[dict], include_unanchored: bool = False) -> dict:
    """Decide which email backs this sales-order row and what it would add.

    Returns {"chosen", "why", "new", "typos"}; "chosen" is None when no email
    can be trusted, with the reason in "why".
    """
    so_date = _day(row["so_date"])
    have = _norm_pos(row.get("customer_po_numbers"))
    in_window = [e for e in candidates
                 if so_date - MAX_LEAD <= e["date"] <= so_date + SLACK_AFTER]

    chosen, why = None, ""
    if have:
        anchored = [e for e in in_window if have <= e["pos"]]
        if anchored:
            # The version sent last before the acknowledgement is the one
            # Flexitallic acted on; an earlier draft may have carried a PO
            # that was later dropped.
            chosen, why = max(anchored, key=lambda e: e["date"]), "anchored"
        else:
            why = "no email contains the POs already on this row"
    else:
        same_customer = [e for e in in_window
                         if row.get("customer") and e["customer"] == row.get("customer")]
        if len(same_customer) == 1:
            chosen = same_customer[0]
            why = "unanchored" if include_unanchored else "unanchored-skipped"
        else:
            why = f"unanchored, {len(same_customer)} same-customer candidates"

    new, typos = set(), []
    if chosen and why != "unanchored-skipped":
        for po in sorted(chosen["pos"] - have):
            near = next((h for h in have if is_typo_of(po, h)), None)
            if near:
                typos.append((po, near))
            else:
                new.add(po)
    return {"chosen": chosen, "why": why, "new": new, "typos": typos}


# Words that are never part of a customer's own reference: SPM's own name and
# sequence, the supplier, the customer's own name, and the stock markers.
_NOISE = re.compile(
    r"S\.?P\.?M\.?|FLEXI?T?A?L?L?I?C?|SPWD|STOCK|INDIASTOCK|VENTURES?|OIL|AND|GAS"
    r"|LIMITED|LTD|ENERGY|WEBS?T?ITE|CHINA"
    # How SPM writes the customer, which is never the customer's own reference.
    r"|C\.?N\.?L\.?|CHEVRON|N\.?L\.?N\.?G\.?|LNG|MOBIL|EXXON|SEPLAT|AVEON|G\.?C\.?A\.?",
    re.IGNORECASE)
# A size or material, not a reference: 8IN, 900#, 150 CLASS, 20MM.
_LOOKS_LIKE_SPEC = re.compile(r"\d\s*(?:IN\b|MM\b|#|CLASS\b)|#", re.IGNORECASE)


def client_ref_from_reference(reference: str | None, customer: str | None,
                              spm_ref: str | None) -> str | None:
    """A small customer's own order reference, taken from the SPM reference.

    Chevron, NLNG, Mobil, Aveon and GCA number their POs in fixed shapes that
    CUSTPO matches. The smaller buyers do not: Waltex quote a date (29082025),
    a website order carries PO-SPMN-25-001, Ella use ELLA & SPM 010. Those are
    the client's reference even though they are not "PO numbers", so the column
    is blank without this.

    Deliberately strict - a blank cell is better than a product description
    sitting in a column headed Customer PO. Returns None unless what is left
    after removing SPM's own wording carries at least two digits and does not
    read as a size.
    """
    text = str(reference or "")
    if not text:
        return None
    if re.search(r"\bSTOCK\b", text, re.IGNORECASE) and not re.search(r"\d{5}", text):
        return None                      # SPM's own stock order: no client, no PO

    # Strip from the LEFT, whole word at a time, rather than deleting these
    # words wherever they appear. A global strip cut the "SPM" out of the
    # middle of "SS/29/01/25/SPM/61136840" and truncated a real reference.
    rest = re.sub(r"^\W*S\.?\s?P\.?\s?M\.?\W*", "", text, flags=re.IGNORECASE)
    while True:
        m = re.match(r"([A-Za-z0-9.]+)\W*", rest)
        if not m:
            break
        word = m.group(1)
        is_noise = (_NOISE.fullmatch(word) is not None
                    or (spm_ref and word.strip(".") == spm_ref)
                    or (customer and word.upper().startswith(customer.upper()[:5])))
        if not is_noise:
            break
        rest = rest[m.end():]
    rest = re.sub(r"\W*FLEXI?T?A?L?L?I?C?\W*$", "", rest, flags=re.IGNORECASE).strip(" -.,&")

    if (len(rest) < 4 or len(re.findall(r"\d", rest)) < 2
            or _LOOKS_LIKE_SPEC.search(rest)):
        return None
    return rest[:40]


def freeform_pass(apply: bool) -> None:
    """Fill Customer PO for rows whose client does not use a known PO format."""
    client = get_client()
    rows = client.table("scratch_flexitallic_history").select(
        "so_number,customer,spm_po_ref,spm_po_reference,customer_po_numbers,order_value"
    ).execute().data
    blank = [r for r in rows if not _norm_pos(r.get("customer_po_numbers"))]
    print(f"  {len(blank)} sales orders with no client PO\n")
    found = []
    for r in sorted(blank, key=lambda r: str(r.get("customer"))):
        ref = client_ref_from_reference(r.get("spm_po_reference"), r.get("customer"),
                                        r.get("spm_po_ref"))
        mark = "->" if ref else "  "
        print(f"  {mark} {r['so_number']} {str(r.get('customer')):10} "
              f"{(r.get('spm_po_reference') or '')[:46]:48} => {ref or '(left blank)'}")
        if ref:
            found.append((r["so_number"], ref))
    print(f"\n  {len(found)} reference(s) recovered, {len(blank) - len(found)} left blank")
    if not apply:
        print("\n  Dry run - nothing written. Re-run with --apply.")
        return
    for so, ref in found:
        client.table("scratch_flexitallic_history").update(
            {"customer_po_numbers": [ref]}).eq("so_number", so).execute()
    print(f"  Applied to {len(found)} row(s).")


def main() -> None:
    apply = "--apply" in sys.argv
    if "--freeform" in sys.argv:
        freeform_pass(apply)
        return
    include_unanchored = "--include-unanchored" in sys.argv
    report_path = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--report=")), None)

    client = get_client()
    rows = client.table("scratch_flexitallic_history").select(
        "so_number,so_date,spm_po_ref,customer,customer_po_numbers").execute().data
    print(f"  {len(rows)} sales orders in scratch_flexitallic_history")

    by_ref = collect_spm_orders(tempfile.mkdtemp())
    print(f"  SPM sequence numbers found in outbound email: {len(by_ref)}")

    plans, typos, unresolved, skipped = [], [], [], []
    # Which rows each email was matched to - two rows of one SPM order sharing
    # an email cannot tell us which sales order a newly found PO sits on.
    claims: dict[int, list[str]] = defaultdict(list)

    for row in rows:
        ref = row.get("spm_po_ref")
        if not ref or ref not in by_ref:
            unresolved.append((row["so_number"], ref, "no SPM purchase-order email for this ref"))
            continue
        plan = plan_row(row, by_ref[ref], include_unanchored)
        chosen, why = plan["chosen"], plan["why"]
        if chosen is None:
            unresolved.append((row["so_number"], ref, why))
            continue
        if why == "unanchored-skipped":
            skipped.append((row["so_number"], ref, sorted(chosen["pos"])))
            continue
        if plan["typos"]:
            typos.append((row["so_number"], ref, plan["typos"]))
        if plan["new"]:
            have = _norm_pos(row.get("customer_po_numbers"))
            claims[chosen["uid"]].append(row["so_number"])
            plans.append({
                "so_number": row["so_number"], "ref": ref, "confidence": why,
                "email_date": chosen["date"].date().isoformat(),
                "subject": chosen["subject"], "have": sorted(have),
                "add": sorted(plan["new"]),
                "result": sorted(drop_typos(have | plan["new"])),
                "from_pdf_only": sorted(plan["new"] - chosen["subject_pos"]),
                "_uid": chosen["uid"],
            })

    shared = {uid for uid, sos in claims.items() if len(sos) > 1}
    ambiguous = [p for p in plans if p["_uid"] in shared]
    to_apply = [p for p in plans if p["_uid"] not in shared]

    print(f"\n{'=' * 78}")
    print(f"  rows gaining client POs          : {len(to_apply)}")
    print(f"  client POs to add                : {sum(len(p['add']) for p in to_apply)}")
    print(f"     of which found only in the PDF: {sum(len(p['from_pdf_only']) for p in to_apply)}")
    print(f"  held back - shared SPM order     : {len(ambiguous)} rows")
    print(f"  held back - typo of an existing PO: {sum(len(t[2]) for t in typos)}")
    print(f"  held back - no client PO on row  : {len(skipped)} (use --include-unanchored)")
    print(f"  could not be matched             : {len(unresolved)}")
    print(f"{'=' * 78}\n")

    for p in to_apply:
        tag = "" if p["confidence"] == "anchored" else "  [unanchored]"
        print(f"  {p['so_number']}  ref {p['ref']}  PO sent {p['email_date']}{tag}")
        print(f"     had : {p['have'] or '-'}")
        print(f"     add : {p['add']}" + (f"   (PDF only: {p['from_pdf_only']})" if p["from_pdf_only"] else ""))
    if ambiguous:
        print("\n  HELD BACK - one SPM order, several sales orders, cannot tell which SO holds the PO:")
        for p in ambiguous:
            print(f"     {p['so_number']}  ref {p['ref']}  would add {p['add']}")
    if typos:
        print("\n  HELD BACK - looks like a typo of a PO already present:")
        for so, ref, pairs in typos:
            for bad, good in pairs:
                print(f"     {so}  ref {ref}  {bad}  ~  {good}")
    if skipped:
        print("\n  HELD BACK - row has no client PO to anchor the match:")
        for so, ref, pos in skipped:
            print(f"     {so}  ref {ref}  single candidate email carries {pos}")

    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump({"apply": to_apply, "ambiguous": ambiguous, "typos": typos,
                       "unanchored_skipped": skipped, "unresolved": unresolved},
                      fh, indent=2, default=str)
        print(f"\n  report written to {report_path}")

    if not apply:
        print("\n  Dry run - nothing written. Re-run with --apply.")
        return

    written = 0
    for p in to_apply:
        client.table("scratch_flexitallic_history").update(
            {"customer_po_numbers": p["result"]}
        ).eq("so_number", p["so_number"]).execute()
        written += 1
    print(f"\n  Applied: {written} row(s) updated.")


if __name__ == "__main__":
    main()
