"""Shared email address constants for SPM Tracker scripts.

Any value can be overridden via the matching env var if an address changes.
"""
import os

SPM_SENDER         = os.environ.get("SPM_SENDER",         "specialpiping@gmail.com")
WAREHOUSE_EMAIL    = os.environ.get("WAREHOUSE_EMAIL",    "spmwarehouse22@gmail.com")
NLNG_PO_SENDER     = os.environ.get("NLNG_PO_SENDER",     "enquiry@specialpipingltd.com")
FLEXITALLIC_SENDER = os.environ.get("FLEXITALLIC_SENDER", "salesorder@flexitallic.eu")

# Socket timeout for every IMAP connection, in seconds.
#
# IMAPClient defaults to None, which blocks forever. That matters here because
# worker.py's health check only asks whether a thread is *alive* — and a thread
# blocked on a half-open socket reports alive indefinitely. The listener would
# stop collecting mail with nothing to show for it and never be restarted.
#
# 120s is deliberately generous: a full-mailbox SEARCH or a 500-message
# ENVELOPE fetch can legitimately take tens of seconds on a slow link, and a
# timeout that fires during normal work would be worse than none at all.
IMAP_TIMEOUT = int(os.environ.get("IMAP_TIMEOUT_SECONDS", "120"))
