"""
One-time script: re-fetch specific Gmail messages by Message-ID and reprocess them.
Used to recover emails that were parked when ANTHROPIC_API_KEY was missing.
"""
import os
import sys
import email

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from gmail_ack_listener import connect_to_gmail, process_message
from db import get_client

# Message-IDs of the 2 stuck emails
TARGET_MESSAGE_IDS = {
    "<CAOPjdC0Xsk6TTN-RxPbz4GATHWFnoRggybSnx=n=XijjRLhjnA@mail.gmail.com>",  # 0061455088
    "<CAOPjdC24-j9BxNE1B+_oZ6fu3zA70rMtLR_c_Jg3tibCSHpJGQ@mail.gmail.com>",  # 0061456056
}

def main():
    client_db = get_client()
    imap = connect_to_gmail()

    print("Searching Gmail All Mail for target emails...")
    imap.select_folder("[Gmail]/All Mail")

    found = 0
    for mid in TARGET_MESSAGE_IDS:
        # Search by Message-ID header
        uids = imap.search(["HEADER", "MESSAGE-ID", mid.strip("<>")])
        if not uids:
            print(f"  ⚠️  Not found in Gmail: {mid}")
            continue

        uid = uids[0]
        print(f"  Found UID {uid} for {mid} — fetching...")
        msg_data = imap.fetch([uid], ["RFC822"])
        if uid not in msg_data:
            print(f"  ❌ Could not fetch UID {uid}")
            continue

        raw_msg = email.message_from_bytes(msg_data[uid][b"RFC822"])
        subject = raw_msg.get("Subject", "")
        print(f"  Subject: {subject}")
        process_message(client_db, msg_data[uid])
        found += 1
        print(f"  ✅ Processed")

    imap.logout()
    print(f"\nDone. Reprocessed {found}/{len(TARGET_MESSAGE_IDS)} emails.")

if __name__ == "__main__":
    main()
