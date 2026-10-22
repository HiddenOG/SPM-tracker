"""
gmail_auth.py — sign in to Gmail once, so SPM Ask can use the Gmail API.

    python scripts/gmail_auth.py

Needs google_client.json in the project folder: the "Desktop app" OAuth client
downloaded from Google Cloud (APIs & Services -> Google Auth Platform ->
Clients). It opens Google's sign-in in the browser; sign in as the mailbox
Ask reads (GMAIL_EMAIL in .env) and allow read-only access. The result goes
into .env:

    GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET, GMAIL_OAUTH_REFRESH_TOKEN

Copy those three to the Railway web service's Variables as well.

Read-only (gmail.readonly): the token can search and read mail, never send,
delete or change anything. Revoke it any time at
https://myaccount.google.com/permissions

If the app's publishing status was left at "Testing", Google ends the grant
after 7 days - publish it (Google Auth Platform -> Audience) and run this once
more.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_FILE = os.path.join(ROOT, "google_client.json")
ENV_FILE = os.path.join(ROOT, ".env")
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _client() -> tuple[str, str]:
    if not os.path.isfile(CLIENT_FILE):
        sys.exit(f"google_client.json not found in {ROOT}.\n"
                 "Download it from Google Cloud (Clients -> your Desktop app -> Download JSON), "
                 "rename it google_client.json and put it there.")
    with open(CLIENT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    c = data.get("installed") or data.get("web") or {}
    if not c.get("client_id") or not c.get("client_secret"):
        sys.exit("google_client.json has no client_id/client_secret - is it the OAuth client file?")
    if "installed" not in data:
        print("Note: this is not a 'Desktop app' client. If Google rejects the sign-in with a "
              "redirect_uri error, create a Desktop app client instead.")
    return c["client_id"], c["client_secret"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sign_in(client_id: str) -> tuple[str, str, str]:
    """Open Google's sign-in and wait for it to come back. -> (code, verifier, redirect)."""
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "login_hint": os.environ.get("GMAIL_EMAIL", ""),
    })
    got: dict = {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404); self.end_headers(); return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in q and q.get("state", [""])[0] == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((
                "<!doctype html><meta charset=utf-8><title>SPM Ask</title>"
                "<body style='font:16px system-ui;background:#151515;color:#ececE8;display:grid;place-items:center;height:90vh'>"
                f"<div><h2>{'Gmail connected' if ok else 'Sign-in did not finish'}</h2>"
                f"<p>{'You can close this tab and go back to the terminal.' if ok else 'Go back to the terminal for details.'}</p></div>"
            ).encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Back)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("\nOpening Google's sign-in in your browser.")
    print("Sign in as", os.environ.get("GMAIL_EMAIL") or "the mailbox Ask reads", "and allow read-only access.")
    print("If the browser doesn't open, paste this address into it:\n\n" + url + "\n")
    print("(Google may say the app isn't verified - choose Advanced, then Go to SPM Ask.)\n")
    webbrowser.open(url)
    print("Waiting for the sign-in to finish (up to 5 minutes)...", flush=True)
    for _ in range(300):
        if got:
            break
        threading.Event().wait(1)
    server.shutdown()
    if not got:
        sys.exit("Timed out waiting for the sign-in. Run this again.")
    if "error" in got:
        sys.exit(f"Google said: {got['error']}. Run this again and choose Allow.")
    if got.get("state") != state:
        sys.exit("The sign-in reply didn't match this request. Run this again.")
    return got["code"], verifier, redirect


def _save_env(values: dict) -> None:
    text = open(ENV_FILE, encoding="utf-8").read() if os.path.isfile(ENV_FILE) else ""
    for k, v in values.items():
        line = f"{k}={v}"
        if re.search(rf"(?m)^{k}=.*$", text):
            text = re.sub(rf"(?m)^{k}=.*$", lambda _m: line, text)
        else:
            text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        pass
    client_id, client_secret = _client()
    code, verifier, redirect = _sign_in(client_id)

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect,
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Google wouldn't swap the sign-in for a token: {r.text[:300]}")
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit("Google sent no refresh token. Remove 'SPM Ask' at "
                 "https://myaccount.google.com/permissions and run this again.")
    if SCOPE not in tok.get("scope", ""):
        sys.exit("Read access to Gmail wasn't granted - tick the Gmail box on Google's screen and run this again.")

    who = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    if who.status_code != 200:
        sys.exit(f"Signed in, but Gmail wouldn't answer: {who.text[:300]}\n"
                 "Is the Gmail API enabled for this Google Cloud project?")
    prof = who.json()
    mailbox = prof.get("emailAddress", "")
    want = (os.environ.get("GMAIL_EMAIL") or "").strip().lower()
    if want and mailbox.lower() != want:
        sys.exit(f"That signed in as {mailbox}, but Ask reads {want}. Run this again and pick {want}.")

    _save_env({"GMAIL_OAUTH_CLIENT_ID": client_id, "GMAIL_OAUTH_CLIENT_SECRET": client_secret,
               "GMAIL_OAUTH_REFRESH_TOKEN": refresh})
    print(f"\nConnected to {mailbox} ({prof.get('messagesTotal', 0):,} messages), read-only.")
    print("Saved GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET and GMAIL_OAUTH_REFRESH_TOKEN to .env.")
    print("Copy those three into the Railway web service's Variables too.")


if __name__ == "__main__":
    main()
