"""
email_utils.py — Shared email-parsing helpers.

These three functions were copied identically across imap_listener,
gmail_ack_listener, warehouse_reply_parser, and supplier_po_parser.
They live here so there is exactly one place to update them.
"""

from email.header import decode_header


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
