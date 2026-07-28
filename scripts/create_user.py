"""
create_user.py — Create or update a user account in SPM Tracker.

Usage:
    python scripts/create_user.py --list
    python scripts/create_user.py --email jane@spm.com --name "Jane Doe" --role warehouse --password secret123
    python scripts/create_user.py --email jane@spm.com --password newpass   # reset password only
    python scripts/create_user.py --email jane@spm.com --deactivate
    python scripts/create_user.py --email jane@spm.com --activate
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import bcrypt
from db import get_client

VALID_ROLES = ("admin", "procurement", "warehouse", "expeditor", "accounts")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage SPM Tracker user accounts")
    parser.add_argument("--email",      help="User email address")
    parser.add_argument("--name",       default="", help="Full name")
    parser.add_argument("--role",       choices=VALID_ROLES, help="User role")
    parser.add_argument("--password",   help="Password to set")
    parser.add_argument("--deactivate", action="store_true", help="Deactivate the account")
    parser.add_argument("--activate",   action="store_true", help="Reactivate the account")
    parser.add_argument("--list",       action="store_true", help="List all users")
    args = parser.parse_args()

    client = get_client()

    if args.list:
        result = client.table("users").select(
            "email,full_name,role,is_active,last_login_at"
        ).order("created_at").execute()
        print(f"\n{'Email':<35} {'Role':<15} {'Name':<25} {'Active':<8} Last login")
        print("-" * 105)
        for u in result.data or []:
            login = (u.get("last_login_at") or "never")[:19]
            print(
                f"{u['email']:<35} {u['role']:<15} {(u.get('full_name') or ''):<25}"
                f" {'yes' if u['is_active'] else 'NO':<8} {login}"
            )
        print()
        return

    if not args.email:
        parser.error("--email is required (or use --list)")

    email = args.email.lower().strip()
    existing = client.table("users").select("id,email,role,is_active").eq("email", email).execute()

    if existing.data:
        user = existing.data[0]
        update: dict = {}
        if args.password:
            update["password_hash"] = bcrypt.hashpw(args.password.encode(), bcrypt.gensalt(rounds=12)).decode()
        if args.role:
            update["role"] = args.role
        if args.name:
            update["full_name"] = args.name
        if args.deactivate:
            update["is_active"] = False
        if args.activate:
            update["is_active"] = True
        if not update:
            print(f"Nothing to update for {email}. Pass --password, --role, --name, --activate, or --deactivate.")
            return
        client.table("users").update(update).eq("id", user["id"]).execute()
        print(f"✅  Updated {email}: {', '.join(update.keys())}")
    else:
        if not args.role:
            parser.error("--role is required when creating a new user")
        if not args.password:
            parser.error("--password is required when creating a new user")
        pw_hash = bcrypt.hashpw(args.password.encode(), bcrypt.gensalt(rounds=12)).decode()
        client.table("users").insert({
            "email":         email,
            "username":      email,
            "full_name":     args.name or None,
            "password_hash": pw_hash,
            "role":          args.role,
            "is_active":     True,
        }).execute()
        print(f"✅  Created user: {email} ({args.role})")


if __name__ == "__main__":
    main()
