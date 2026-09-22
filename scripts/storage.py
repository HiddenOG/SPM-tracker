"""
storage.py — Supabase Storage helpers for PDF uploads.

Bucket: spm-pdfs  (public, created once in Supabase dashboard)
Layout:
  po/{po_number}/{filename}   — Chevron PO PDFs
  ack/{po_number}/{filename}  — Flexitallic ack PDFs
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BUCKET = "spm-pdfs"


def _client():
    from db import get_client
    return get_client()


def upload_pdf(local_path: str, folder: str, po_number: str) -> str | None:
    """
    Upload a local PDF to Supabase Storage and return its public URL.

    folder: "po" or "ack"
    Returns None if the file doesn't exist or upload fails.
    """
    p = Path(local_path)
    if not p.exists():
        return None

    storage_path = f"{folder}/{po_number}/{p.name}"
    data = p.read_bytes()

    try:
        _client().storage.from_(BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
    except Exception as e:
        # Some versions of storage-py don't support upsert in file_options;
        # fall back to explicit update (overwrite).
        try:
            _client().storage.from_(BUCKET).update(
                path=storage_path,
                file=data,
                file_options={"content-type": "application/pdf"},
            )
        except Exception as e2:
            print(f"  Storage upload failed for {p.name}: {e2}")
            return None

    return _client().storage.from_(BUCKET).get_public_url(storage_path)
