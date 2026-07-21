"""
One-time script to create users in the SPM Tracker platform.

Usage:
    python scripts/create_user.py

Roles: admin | procurement | warehouse | expeditor
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

import bcrypt
from db import get_client

ROLES = ['admin', 'procurement', 'warehouse', 'expeditor', 'accounts']

print("═" * 40)
print("  SPM Tracker — Create User")
print("═" * 40)

full_name = input("Full name:       ").strip()
email     = input("Email:           ").strip().lower()
username  = input("Username:        ").strip()
role      = input(f"Role {ROLES}: ").strip().lower()
password  = input("Password:        ").strip()

if role not in ROLES:
    print(f"❌  Invalid role. Choose from: {ROLES}")
    sys.exit(1)

if not email or not password or not username:
    print("❌  Email, username, and password are required.")
    sys.exit(1)

password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

try:
    result = get_client().table("users").upsert({
        "username":      username,
        "email":         email,
        "password_hash": password_hash,
        "role":          role,
        "full_name":     full_name,
        "is_active":     True,
    }, on_conflict="email").execute()

    print(f"\n✅  User created:")
    print(f"    Name:  {full_name}")
    print(f"    Email: {email}")
    print(f"    Role:  {role}")
except Exception as e:
    print(f"❌  Failed: {e}")
    sys.exit(1)
