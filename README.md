# SPM Order Tracking System — Stages 1 to 5

This covers the first slice of the full plan: from a Chevron PO notification
landing in Yahoo Mail, all the way to drafting the SPM purchase order sent
to the supplier (e.g. Flexitallic).

## What's built so far

| Stage | Script | What it does | Automated? |
|---|---|---|---|
| 1 | `imap_listener.py` | Watches Yahoo inbox, detects Chevron PO notifications, creates order record | ✅ Fully automatic, runs continuously |
| 2 | `pdf_extractor.py` | Reads the attached PO PDF with Claude, extracts line items + classifies product line | ✅ Fully automatic |
| 3 | `acknowledge.py` | Lists pending acknowledgments, lets you log when you've clicked the real button in Chevron's email | ⚠️ Manual click required (Chevron blocks automation) — but tracking is automatic |
| 4 | `pricing.py` | Matches line items against the digitized price list, flags missing prices | ✅ Mostly automatic — quotation requests still sent manually |
| 5 | `spm_po_draft.py` | Drafts the SPM PO once acknowledged + priced, tracks when it's sent | ✅ Draft is automatic, sending is human-reviewed |

## One-time setup

### 1. Create your Supabase project
Go to [supabase.com](https://supabase.com), create a free project, then:
- Go to **SQL Editor**, paste the contents of `sql/001_schema.sql`, run it.
- Then paste `sql/002_seed_data.sql`, run it (edit the buyer/supplier names first if needed).
- Go to **Settings > API**, copy your Project URL and `service_role` key.

### 2. Set up your Yahoo App Password
Yahoo won't let scripts log in with your normal password. You need an "app password":
- Go to Yahoo Account Security settings → Generate app password
- Use that, not your real password, in the `.env` file

### 3. Get an Anthropic API key
From [console.anthropic.com](https://console.anthropic.com) — used for PDF extraction (Stage 2).

### 4. Configure environment variables
```bash
cp .env.example .env
```
Then open `.env` and fill in all the real values (Supabase URL/key, Yahoo email/app password, Anthropic key).

### 5. Install dependencies
```bash
python -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Running it day to day

### Start the listener (run this continuously in the background)
```bash
python scripts/imap_listener.py
```
This checks your inbox every 2 minutes (configurable via `CHECK_INTERVAL_SECONDS` in `.env`)
and automatically creates new order records the moment a Chevron PO notification arrives.

### Run PDF extraction (run after new orders come in)
```bash
python scripts/pdf_extractor.py
```
This can be run manually whenever, or you can add it to a simple loop/cron later.
It only processes orders that haven't been extracted yet, so it's safe to re-run often.

### Check what's waiting on acknowledgment
```bash
python scripts/acknowledge.py list
```
This shows every PO waiting on the manual "click acknowledge" step, sorted oldest first,
with a 🔴 flag for anything waiting over 24 hours.

### Mark a PO as acknowledged
**After** you've actually clicked the Acknowledge button in the real Chevron email:
```bash
python scripts/acknowledge.py mark 0061440972 "Simeon Moju"
```

### Run pricing
```bash
python scripts/pricing.py
```
Matches line items against your price list. Flags anything not found so you know
to request a quotation from the supplier.

### Draft the SPM PO
```bash
python scripts/spm_po_draft.py draft
```
Generates a text draft for every order that's acknowledged and fully priced.
Review the draft file it saves, then once you've actually sent it to the supplier:
```bash
python scripts/spm_po_draft.py sent 0061440972
```

## Important things to know

- **Nothing here clicks the Chevron acknowledgment button for you.** That's
  intentional — Chevron's email explicitly blocks this from working for
  forwarded copies, so it must be the original recipient, by hand, every time.
- **The price list table starts empty.** You need to manually load your
  current Flexitallic price list PDF into the `price_list` table once
  before pricing will find any matches. (We'll build a loader script for
  this next — for now you can insert rows directly via Supabase's table editor.)
- **Every email is logged in `processed_emails`**, even ones that don't
  match a known buyer — this means nothing is silently ignored and you
  can always check why something wasn't picked up.
- **This is intentionally not connected to a live dashboard yet.** Right
  now you're interacting with it through these scripts in the terminal.
  Once you trust the data is flowing correctly, building a dashboard on
  top of this same Supabase database is straightforward — the hard part
  (getting clean data in) is what this phase solves first.

## What's next (not built yet)

- Price list PDF loader (digitize the Flexitallic price list into the `price_list` table)
- Supplier email parser (Stage 6 — catching Flexitallic's SO confirmation automatically)
- Warehouse status forms (Stage 7-8)
- Chevron booking confirmation parser (Stage 9)
- Dashboard UI for all three departments
