"""
ask_memory.py - SPM Ask's memory beyond one chat.

  * Notes (table ask_memory): things that stay true - a person's usual price
    formula, a client's standing preference. Ask saves them with remember,
    drops them with forget, and reads the person's own and the team's at the
    start of every question.
  * Past chats (ask_messages): searched by words, for "what did we price for
    Seplat last week?".

Every function is quiet on failure: a missing table (the migration not run
yet) or a database hiccup must never stop a question being answered.
"""
from __future__ import annotations

import re

from db import get_client

MAX_NOTES = 40            # loaded into each question, newest first
NOTE_CHARS = 400


def _uid(user: dict | None) -> str:
    return str((user or {}).get("sub") or "")


def notes_for(user: dict | None) -> list[dict]:
    """The person's own notes and the team's, newest first."""
    try:
        db = get_client()
        mine = db.table("ask_memory").select("id,about,note,user_name,created_at") \
            .eq("about", "person").eq("user_id", _uid(user)).order("created_at", desc=True).limit(MAX_NOTES).execute().data or []
        team = db.table("ask_memory").select("id,about,note,user_name,created_at") \
            .eq("about", "team").order("created_at", desc=True).limit(MAX_NOTES).execute().data or []
        rows = (list(mine) if isinstance(mine, list) else []) + (list(team) if isinstance(team, list) else [])
        return [r for r in rows if isinstance(r, dict) and r.get("id") and r.get("note")][:MAX_NOTES]
    except Exception as exc:
        print(f"  [warn] ask_memory.notes_for: {exc}")
        return []


def recall_text(user: dict | None) -> str:
    """The notes as they go into a question - each with its short id, so Ask
    can forget one by it. Empty when there are none."""
    rows = notes_for(user)
    if not rows:
        return ""
    lines = [f"- [{r['id'][:8]}] ({'about you' if r['about'] == 'person' else 'team'}) {r['note']}" for r in rows]
    return "What you remember (from earlier chats):\n" + "\n".join(lines)


def remember(user: dict | None, note: str, about: str = "me", chat_id: str | None = None) -> str:
    note = " ".join(str(note or "").split())[:NOTE_CHARS]
    if not note:
        return "Nothing to remember was given."
    who = (user or {}).get("name") or (user or {}).get("email") or ""
    try:
        db = get_client()
        kind = "team" if str(about).lower() == "team" else "person"
        # The same note twice is one note.
        same = db.table("ask_memory").select("id").eq("about", kind).eq("note", note)
        same = (same.eq("user_id", _uid(user)) if kind == "person" else same).limit(1).execute().data
        if same:
            return f"Already remembered [{same[0]['id'][:8]}]: {note}"
        row = db.table("ask_memory").insert({"about": kind, "user_id": _uid(user), "user_name": who,
                                             "note": note, "chat_id": chat_id}).execute().data[0]
        return f"Remembered [{row['id'][:8]}] ({'about this person' if kind == 'person' else 'for the team'}): {note}"
    except Exception as exc:
        print(f"  [warn] ask_memory.remember: {exc}")
        return "That couldn't be saved - memory isn't set up yet (migrations/create_ask_memory.sql). Tell the person."


def forget(user: dict | None, note_id: str) -> str:
    """Delete a note by its id or the first 8 characters of it. A person may
    drop their own notes and the team's; not someone else's personal ones."""
    nid = re.sub(r"[^0-9a-f-]", "", str(note_id or "").lower())
    if len(nid) < 6:
        return "Give the note's id, as shown in brackets."
    try:
        db = get_client()
        rows = [r for r in notes_for(user) if r["id"].startswith(nid)]
        if not rows:
            return f"No note [{nid}] among this person's or the team's notes."
        db.table("ask_memory").delete().eq("id", rows[0]["id"]).execute()
        return f"Forgot [{rows[0]['id'][:8]}]: {rows[0]['note']}"
    except Exception as exc:
        print(f"  [warn] ask_memory.forget: {exc}")
        return "That couldn't be deleted just now."


def search_chats(text: str, since: str | None = None, limit: int = 12) -> str:
    """Earlier Ask chats whose questions or answers contain these words."""
    words = [w for w in re.findall(r"[\w#./-]+", str(text or "")) if len(w) > 1][:6]
    if not words:
        return "Give a few words to look for."
    try:
        db = get_client()
        q = db.table("ask_messages").select("chat_id,role,content,asked_by,created_at")
        for w in words:
            q = q.ilike("content", f"%{w}%")
        if since:
            q = q.gte("created_at", since)
        rows = q.order("created_at", desc=True).limit(limit).execute().data or []
        if not rows:
            return f"No earlier chat mentions {' '.join(words)}."
        titles = {c["id"]: c["title"] for c in db.table("ask_chats").select("id,title")
                  .in_("id", list({r["chat_id"] for r in rows})).execute().data or []}
        out = []
        for r in rows:
            body = " ".join(r["content"].split())
            at = body.lower().find(words[0].lower())
            snip = body[max(0, at - 200): at + 600] if at >= 0 else body[:800]
            out.append(f"{r['created_at'][:16]} · chat \"{titles.get(r['chat_id'], '')}\" · "
                       f"{'asked by ' + (r.get('asked_by') or '?') if r['role'] == 'user' else 'Ask answered'}:\n  {snip}")
        return "\n\n".join(out)
    except Exception as exc:
        print(f"  [warn] ask_memory.search_chats: {exc}")
        return "Earlier chats couldn't be searched just now."
