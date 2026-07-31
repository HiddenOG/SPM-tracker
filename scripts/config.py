"""Shared email address constants for SPM Tracker scripts.

Any value can be overridden via the matching env var if an address changes.
"""
import os

SPM_SENDER         = os.environ.get("SPM_SENDER",         "specialpiping@gmail.com")
WAREHOUSE_EMAIL    = os.environ.get("WAREHOUSE_EMAIL",    "spmwarehouse22@gmail.com")
NLNG_PO_SENDER     = os.environ.get("NLNG_PO_SENDER",     "enquiry@specialpipingltd.com")
FLEXITALLIC_SENDER = os.environ.get("FLEXITALLIC_SENDER", "salesorder@flexitallic.eu")
