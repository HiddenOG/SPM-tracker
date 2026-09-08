"""
email_utils.py — Shared email-parsing helpers.

These three functions were copied identically across imap_listener,
gmail_ack_listener, warehouse_reply_parser, and supplier_po_parser.
They live here so there is exactly one place to update them.
"""

import re
from email.header import decode_header

# Characters no filesystem will accept in a name, plus control bytes.
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "attachment", max_len: int = 120) -> str:
    """
    Turn an email attachment filename into something safe to use as a path.

    Attachment filenames are written by whoever sent the email, so they are
    not trustworthy. Three things go wrong if they're used as-is:

      * Path escape — a name containing '../' walks out of the folder it was
        meant to be written into.
      * Rejected characters — ':' or '|' (and control bytes) raise OSError on
        Windows, and NUL raises everywhere.
      * Over-long names — filesystems cap a single component around 255 bytes.

    The last two matter beyond tidiness: they raise mid-batch, and because a
    listener only saves its IMAP cursor *after* the batch finishes, the same
    message is refetched and fails identically on every later poll — the
    pipeline stops with no error surfaced anywhere.

    Strips any directory part, replaces reserved characters, and truncates
    while keeping the extension.
    """
    name = decode_mime_words(name or "")
    # Drop any directory component — handles both separators, so
    # '../../x.pdf' and '..\\..\\x.pdf' both reduce to 'x.pdf'.
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE_FILENAME_RE.sub("_", name).strip()
    # '.' and '..' survive the above but aren't usable filenames.
    if not name.strip("._"):
        return fallback
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def decode_mime_words(s: str) -> str:
    """Decode an email header value that may contain MIME-encoded words."""
    if not s:
        return ""
    result = ""
    for part, encoding in decode_header(s):
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="replace")
        else:
            result += part
    return result


def get_email_body_text(msg) -> str:
    """Extract the best plain-text body from a message.

    Prefers text/plain. Falls back to text/html in a single pass so the
    first HTML part is captured without a second iteration.
    """
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if ct == "text/plain":
                return payload.decode(errors="replace")
            if ct == "text/html" and not html_fallback:
                html_fallback = payload.decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
    return html_fallback


def is_already_processed(message_id: str) -> bool:
    """Return True if this message_id already has a row in processed_emails."""
    from db import get_client
    result = (
        get_client()
        .table("processed_emails")
        .select("id")
        .eq("message_id", message_id)
        .execute()
    )
    return len(result.data) > 0
