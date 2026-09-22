"""
backfill_flexitallic_po_values.py - add the PURCHASE ORDER side to the
Flexitallic history, and link both documents.

scratch_flexitallic_history was built from Flexitallic's sales acknowledgements,
so it only held what Flexitallic said they would charge. SPM's own purchase
order is a different document with a different total - of 164 orders where both
are known, exactly one matched. This script reads SPM's outgoing PO emails to
Flexitallic, extracts the PO total and line items, uploads both PDFs to
Supabase Storage, and fills in the missing columns.

It also corrects customer attribution. Two problems were found:
  * a reference naming two parties ("S.P.M.-C.N.L.-3096-AVEON-...") was filed
    under whichever appeared first, hiding an Aveon order under Chevron;
  * several real customers were never coded for at all - Indorama, Vagan Oil
    and Gas, Waltex Ventures, Starfinix, Ella, T.A.M.

    python scripts/backfill_flexitallic_po_values.py                # dry run
    python scripts/backfill_flexitallic_po_values.py --apply
    python scripts/backfill_flexitallic_po_values.py --apply --no-pdfs
"""

import os
import re
import sys
import json
import email as email_mod
import tempfile
import time
from pathlib import Path
from datetime import date, datetime

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

import pdfplumber
from db import get_client
from email_utils import decode_mime_words
from supplier_po_parser import connect_to_gmail
from config import SPM_SENDER

FLEX_DOMAIN = "flexitallic.eu"

# SPM's PO sequence number, e.g. the 3123 in "S.P.M. - 3123-NLNG-4200094522".
SPMSEQ = re.compile(
    r"S\.?\s?P\.?\s?M\.?[-\s.]*(?:D\.?M\.?[-\s.]*)?(?:[A-Z.\s]{0,12}?[-\s.]+)?(\d{4})\b",
    re.IGNORECASE,
)
# SPM mistypes this subject regularly. Real examples that an exact
# "PURCHASE ORDER" search silently dropped, taking their PO values with them:
#   "PURCHASE ORER-S.P.M. - 3111.-NLNG-4200092776- FLEXITALLIC"   (no D)
#   "PURCHASE ORDR (S.P.M. - C.N.L. - 3122 - 0061470732 ...)"     (no E)
#   "PURCHASE ORDE (S.P.M. - C.N.L.- 0061360182 - FLEXITALLIC)"   (no R)
# Every letter after "OR" is optional, which still refuses "PURCHASE OF
# ITEMS", "PURCHASE REQUEST" and "REQUEST FOR PURCHASE".
PURCHASE_ORDER_RE = re.compile(r"\bPURCHA[SC]E\s+ORD?E?R?\b", re.IGNORECASE)
# OCR of a scanned PO is noisy: "TOTAL] $6,404.66]" is a real example, with
# stray brackets and pipes from the table borders.
OCR_TOTAL_RE = re.compile(
    r"\b(?:NET\s*|NOT\s*)?TOTAL\b[^\d\n]{0,12}([\d,]+[.,]\d{2})",
    re.IGNORECASE,
)
# Customer PO numbers, used to recognise a PO whose subject omits SPM's own
# sequence number (PO 3050 is titled by the Chevron PO alone).
CUSTOMER_PO_RE = re.compile(r"\b(006\d{7}|4200\d{5,6}|4501\d{6})\b")
# The grand total. pdfplumber reads the label as "NET TOTAL" or "NOT TOTAL"
# depending on how the font renders, so both spellings are accepted.
PO_TOTAL = re.compile(r"N[OE]T\s*TOTAL\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE)
PO_TOTAL_LOOSE = re.compile(r"\bTOTAL\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})\s*$", re.IGNORECASE | re.MULTILINE)
PO_CURRENCY = re.compile(r"(GBP|EUR|USD|\$|£|€)")
PO_LINE = re.compile(
    r"^\s*(\d{1,3})\s+(\S+)?\s*.*?\s(\d[\d,]*)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$"
)

# Customers that only ever appear by name.
NAMED = [
    ("INDORAMA",  r"\bINDORAMA\b"),
    ("STARFINIX", r"\bSTARFINIX\b"),
    ("VAGAN",     r"\bVAGAN\b"),
    ("WALTEX",    r"\bWALTEX\b"),
    ("ELLA",      r"\bELLA\b"),
    ("SEPLAT",    r"\bSEPLAT\b"),
    ("CHINA",     r"\bCHINA\b"),
]
# A sales channel, not a customer: only used when nobody else fits.
WEBSITE_RE = r"\bWEBS?T?ITE\b"
# Customers identifiable from the shape of their own PO number.
PO_SHAPE = [
    ("CHEVRON", r"\b006\d{7}\b"),
    ("NLNG",    r"\b4200\d{5,6}\b"),
    ("MOBIL",   r"\b4501\d{6}\b"),
]
NAME_TOKEN = [
    ("CHEVRON", r"C\.?\s?N\.?\s?L\.?(?![A-Z])|\bCHEVRON\b"),
    ("NLNG",    r"N\.?\s?L\.?\s?N\.?\s?G\.?|\bLNG\b"),
    ("MOBIL",   r"\bMOBIL\b|\bEXXON\b"),
]


def classify_customer(text: str) -> str | None:
    """Work out who an order was ultimately for.

    Three things make this harder than a single regex:

    * SPM raises Aveon work under the client it is destined for, so
      "S.P.M.-C.N.L.-3096-AVEON-..." is an AVEON order even though it names
      Chevron first. Aveon therefore wins outright.
    * A reference often names one customer but carries another's PO number
      ("S.P.M.-C.N.L.-1094-4200067385"). The PO NUMBER is the harder evidence,
      so counting numbers settles it.
    * Some orders genuinely cover two customers at once
      ("...C.NL.-N.L.N.G.-0061035578-4200074564..."). Those are reported as
      MIXED rather than forced into one bucket. A first-attempt priority
      ordering silently relabelled an order carrying four Chevron POs and one
      Mobil PO as Mobil, which is how that trap was found.
    """
    hay = text or ""
    if re.search(r"\bAVEON\b|\bPOH\d", hay, re.IGNORECASE):
        return "AVEON"
    for name, pattern in NAMED:
        if re.search(pattern, hay, re.IGNORECASE):
            return name

    counts = {name: len(re.findall(pat, hay, re.IGNORECASE)) for name, pat in PO_SHAPE}
    present = {k: v for k, v in counts.items() if v}
    if present:
        top = max(present.values())
        leaders = [k for k, v in present.items() if v == top]
        if len(leaders) == 1:
            return leaders[0]
        return "MIXED"

    named = [n for n, pat in NAME_TOKEN if re.search(pat, hay, re.IGNORECASE)]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        return "MIXED"
    if re.search(WEBSITE_RE, hay, re.IGNORECASE):
        return "WEBSITE"
    return None


def _pdf_bytes(msg) -> bytes | None:
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if payload and payload[:4] == b"%PDF":
            return payload
    return None


def _ocr_pdf_text(path: str, max_pages: int = 3) -> str:
    """Read a scanned purchase order with Tesseract.

    Some POs are scans with no text layer at all - pdfplumber returns an empty
    string and their value silently drops out of the totals. The app already
    ships Tesseract for the GEP PDFs, so reuse it rather than lose the money.
    Only the first few pages are rendered: the grand total sits on the last
    page of a short document, and OCR is slow.
    """
    try:
        import fitz
        from PIL import Image
        import pytesseract
        from pdf_extractor import _tesseract_cmd

        pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
        doc = fitz.open(path)
        try:
            pages = min(len(doc), max_pages)
            out = []
            for idx in range(pages):
                pix = doc[idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                out.append(pytesseract.image_to_string(img))
        finally:
            doc.close()
        return "\n".join(out)
    except Exception as exc:
        print(f"  [warn] OCR failed for {os.path.basename(path)}: {str(exc)[:70]}")
        return ""


def parse_po_pdf(path: str) -> dict:
    """Pull the grand total and line items off an SPM purchase order."""
    out: dict = {"po_value": None, "po_currency": "USD", "po_line_items": []}
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
    except Exception:
        text = ""

    # A scan has no text layer at all, so fall back to OCR rather than
    # let the purchase order drop out of the totals entirely.
    if len(text.strip()) < 40:
        text = _ocr_pdf_text(path)
        out["source"] = "ocr"
    if not text.strip():
        return out

    is_ocr = out.get("source") == "ocr"
    m = PO_TOTAL.search(text)
    if m:
        out["po_value"] = float(m.group(1).replace(",", ""))
    elif is_ocr:
        # Only a LABELLED total is trusted from OCR. The loose "last money
        # figure" rule below reads a line item as the grand total on a
        # scrambled scan - PO 3052 came back as $1,461.90, a single line,
        # against a real order of $2,423.10. A blank is honest; a wrong
        # number in a report the boss forwards to Flexitallic is not.
        om = OCR_TOTAL_RE.search(text)
        out["po_value"] = float(om.group(1).replace(",", "").replace(" ", "")) if om else None
    else:
        loose = PO_TOTAL_LOOSE.findall(text)
        out["po_value"] = float(loose[-1].replace(",", "")) if loose else None

    cur = PO_CURRENCY.search(text)
    if cur:
        sym = cur.group(1)
        out["po_currency"] = {"$": "USD", "£": "GBP", "€": "EUR"}.get(sym, sym)

    for line in text.splitlines():
        lm = PO_LINE.match(line)
        if lm:
            out["po_line_items"].append({
                "item_no": lm.group(1),
                "customer_po": lm.group(2),
                "qty": float(lm.group(3).replace(",", "")),
                "unit_price": float(lm.group(4).replace(",", "")),
                "total": float(lm.group(5).replace(",", "")),
            })
    return out


def _reconnect():
    """Fresh IMAP session. Gmail drops long-running connections."""
    imap = connect_to_gmail()
    imap.select_folder("[Gmail]/All Mail", readonly=True)
    return imap


def _fetch_with_retry(imap, uids, parts, tries=3):
    """Fetch a chunk, rebuilding the connection if the socket dies.

    A previous run of this script died 100 PDFs in with
    "ssl.SSLEOFError: EOF occurred in violation of protocol" and, because it
    only wrote at the very end, saved nothing at all. Returns (result, imap)
    so the caller keeps whatever connection ended up working.
    """
    for attempt in range(tries):
        try:
            return imap.fetch(uids, parts), imap
        except Exception as exc:
            if attempt == tries - 1:
                print(f"  [warn] fetch failed after {tries} tries: {exc}")
                return {}, imap
            print(f"  [warn] connection lost ({str(exc)[:60]}) - reconnecting")
            try:
                imap.logout()
            except Exception:
                pass
            time.sleep(3)
            imap = _reconnect()
    return {}, imap


def _already_uploaded(folder: str, key: str) -> str | None:
    """Public URL of an already-stored PDF, so a re-run does not re-upload."""
    try:
        client = get_client()
        items = client.storage.from_("spm-pdfs").list(f"{folder}/{key}")
        if items:
            name = items[0].get("name")
            if name:
                return client.storage.from_("spm-pdfs").get_public_url(f"{folder}/{key}/{name}")
    except Exception:
        pass
    return None


def _upload(local_path: str, folder: str, key: str) -> str | None:
    try:
        from storage import upload_pdf
        return upload_pdf(local_path, folder, key)
    except Exception as exc:
        print(f"  [warn] upload failed for {folder}/{key}: {exc}")
        return None


def collect_po_documents(tmpdir: str, want_pdfs: bool) -> dict:
    """Newest purchase order per SPM reference, with its total and PDF.

    Reconnects if Gmail drops the socket, and skips PDFs already in storage so
    an interrupted run resumes instead of repeating its uploads.
    """
    imap = _reconnect()
    found: dict[str, dict] = {}
    try:
        # Search on "PURCHASE" alone - the exact phrase misses the typos - and
        # let PURCHASE_ORDER_RE below decide what is really a purchase order.
        # IMAP matches literal substrings, so each spelling needs its own
        # search. "PURCHACE ORDER (S.P.M. - C.N.L. - 3003 ...)" is real mail.
        uids = set()
        for spelling in ("PURCHASE", "PURCHACE"):
            uids |= set(imap.search(["FROM", SPM_SENDER, "TO", FLEX_DOMAIN,
                                     "SUBJECT", spelling, "SINCE", date(2024, 1, 1)]))
        uids = sorted(uids)
        print(f"  {len(uids)} SPM -> Flexitallic purchase order email(s)")
        BATCH = 10
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            fetched, imap = _fetch_with_retry(imap, chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                data = fetched.get(uid)
                if not data:
                    continue
                msg = email_mod.message_from_bytes(data[b"RFC822"])
                subject = re.sub(r"\s+", " ", decode_mime_words(msg.get("Subject", "") or ""))
                if not PURCHASE_ORDER_RE.search(subject):
                    continue
                sm = SPMSEQ.search(subject)
                if sm:
                    ref = sm.group(1)
                else:
                    # No SPM sequence in the subject - fall back to the
                    # customer's own PO number and resolve it later.
                    cm = CUSTOMER_PO_RE.search(subject)
                    if not cm:
                        continue
                    ref = "CUSTPO:" + cm.group(1)
                stamp = data[b"INTERNALDATE"]
                prev = found.get(ref)
                if prev and prev["po_date"] >= stamp:
                    continue
                raw = _pdf_bytes(msg)
                if not raw:
                    continue
                path = os.path.join(tmpdir, f"PO_{ref}.pdf")
                with open(path, "wb") as fh:
                    fh.write(raw)
                rec = parse_po_pdf(path)
                rec["po_date"] = stamp
                rec["subject"] = subject
                rec["_path"] = path
                if want_pdfs:
                    rec["po_pdf_url"] = _already_uploaded("flex_po", ref) or _upload(path, "flex_po", ref)
                else:
                    rec["po_pdf_url"] = None
                found[ref] = rec
            if (i // BATCH) % 8 == 0:
                print(f"     {min(i + BATCH, len(uids))}/{len(uids)}  ({len(found)} POs)", flush=True)
    finally:
        try:
            imap.logout()
        except Exception:
            pass       # a dead socket must not lose the work already done
    return found


def collect_so_pdfs(so_numbers: set, tmpdir: str) -> dict:
    """Upload each sales acknowledgement PDF and return {so_number: url}."""
    imap = _reconnect()
    urls: dict[str, str] = {}
    try:
        uids = imap.search(["FROM", "salesorder@flexitallic.eu", "SINCE", date(2024, 1, 1)])
        print(f"  {len(uids)} Flexitallic acknowledgement email(s)")
        BATCH = 10
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            fetched, imap = _fetch_with_retry(imap, chunk, ["RFC822"])
            for uid in chunk:
                data = fetched.get(uid)
                if not data:
                    continue
                msg = email_mod.message_from_bytes(data[b"RFC822"])
                subject = decode_mime_words(msg.get("Subject", "") or "")
                sm = re.search(r"\bSO\d{5,}\b", subject)
                if not sm or sm.group(0) not in so_numbers or sm.group(0) in urls:
                    continue
                so = sm.group(0)
                existing = _already_uploaded("flex_so", so)
                if existing:
                    urls[so] = existing
                    continue
                raw = _pdf_bytes(msg)
                if not raw:
                    continue
                path = os.path.join(tmpdir, f"{so}.pdf")
                with open(path, "wb") as fh:
                    fh.write(raw)
                url = _upload(path, "flex_so", so)
                if url:
                    urls[so] = url
            if (i // BATCH) % 8 == 0:
                print(f"     {min(i + BATCH, len(uids))}/{len(uids)}  ({len(urls)} stored)", flush=True)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return urls


def _match_po(row: dict, pos: dict) -> dict | None:
    """Find the purchase order belonging to a sales-order row.

    Normally SPM's sequence number links them. Some PO subjects carry only
    the customer's PO number, so those were parked under a "CUSTPO:" key and
    are matched here against the numbers on the row instead.
    """
    ref = row.get("spm_po_ref")
    if ref and ref in pos:
        return pos[ref]

    numbers = row.get("customer_po_numbers") or []
    if isinstance(numbers, str):
        try:
            numbers = json.loads(numbers)
        except Exception:
            numbers = [numbers]
    for num in numbers:
        hit = pos.get("CUSTPO:" + str(num))
        if hit:
            return hit

    # Last resort: the reference text itself may quote the customer PO.
    for num in CUSTOMER_PO_RE.findall(str(row.get("spm_po_reference") or "")):
        hit = pos.get("CUSTPO:" + num)
        if hit:
            return hit
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    want_pdfs = "--no-pdfs" not in sys.argv
    client = get_client()

    if apply:
        try:
            client.table("scratch_flexitallic_history").select("po_value").limit(1).execute()
        except Exception:
            print("Refusing to write: the po_value column does not exist.")
            print("Apply migrations/scratch_flexitallic_po_values.sql first.")
            sys.exit(1)

    rows = client.table("scratch_flexitallic_history").select("*").execute().data or []
    if not rows:
        sys.exit("scratch_flexitallic_history is empty - run backfill_flexitallic_history.py first")
    print(f"Flexitallic PO enrichment - {'APPLYING' if apply else 'DRY RUN'}"
          f"{'' if want_pdfs else ', values only'}\n")

    tmpdir = tempfile.mkdtemp()
    pos = collect_po_documents(tmpdir, want_pdfs and apply)
    print(f"\n  {len(pos)} distinct purchase orders, "
          f"{sum(1 for v in pos.values() if v['po_value'] is not None)} with a total\n")

    if apply and pos:
        wrote = 0
        for r in rows:
            po = _match_po(r, pos)
            if not po:
                continue
            patch = {"po_value": po["po_value"], "po_currency": po["po_currency"],
                     "po_date": po["po_date"].isoformat(), "po_line_items": po["po_line_items"]}
            if po.get("po_pdf_url"):
                patch["po_pdf_url"] = po["po_pdf_url"]
            client.table("scratch_flexitallic_history").update(patch).eq("id", r["id"]).execute()
            wrote += 1
        print(f"  PO values written to {wrote} row(s) before the sales-order pass")
        print("")

    so_urls = {}
    if want_pdfs and apply:
        so_urls = collect_so_pdfs({r["so_number"] for r in rows}, tmpdir)
        print(f"\n  {len(so_urls)} sales acknowledgement PDF(s) stored\n")

    matched = recust = 0
    updates = []
    for r in rows:
        po = _match_po(r, pos)
        # Classified from the SALES ORDER's own text only. The purchase order
        # email subject was folded in here at first, which let one PO's
        # wording contaminate every sales order sharing its sequence number -
        # three Chevron orders under ref 3096 came out labelled CHINA because
        # the PO email for 3096 happened to mention it.
        hay = " ".join(str(x) for x in
                       [r.get("spm_po_reference"), r.get("source_subject")] if x)
        new_cust = classify_customer(hay)
        patch = {}
        if po:
            matched += 1
            patch.update({
                "po_value": po["po_value"],
                "po_currency": po["po_currency"],
                "po_date": po["po_date"].isoformat(),
                "po_line_items": po["po_line_items"],
            })
            if po.get("po_pdf_url"):
                patch["po_pdf_url"] = po["po_pdf_url"]
        if so_urls.get(r["so_number"]):
            patch["so_pdf_url"] = so_urls[r["so_number"]]
        if new_cust and new_cust != r.get("customer"):
            patch["customer"] = new_cust
            recust += 1
        if patch:
            updates.append((r["id"], r["so_number"], r.get("customer"), new_cust, patch))

    print(f"rows in table                 : {len(rows)}")
    print(f"  matched to a purchase order : {matched}")
    print(f"  customer re-classified      : {recust}")
    print(f"  rows to update              : {len(updates)}")

    if recust:
        print("\n  customer corrections:")
        shown = 0
        for _id, so, old, new, _p in updates:
            if new and new != old:
                print(f"     {so}  {str(old):10} -> {new}")
                shown += 1
                if shown >= 15:
                    print("     ...")
                    break

    if not apply:
        print("\n(dry run - add --apply to write)")
        return

    done = 0
    for _id, _so, _old, _new, patch in updates:
        client.table("scratch_flexitallic_history").update(patch).eq("id", _id).execute()
        done += 1
        if done % 50 == 0:
            print(f"   updated {done}/{len(updates)}")
    print(f"\nupdated {done} row(s)")


if __name__ == "__main__":
    main()
