"""Create a dashboard login (public sign-up is disabled).

Usage:
    python create_user.py someone@example.com              # generates a strong password
    python create_user.py someone@example.com MyPassword   # uses the given password
"""
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent / ".env")
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

email = sys.argv[1]
password = sys.argv[2] if len(sys.argv) > 2 else secrets.token_urlsafe(12)
db.auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
print(f"Created login: {email}")
if len(sys.argv) <= 2:
    print(f"Password: {password}")
