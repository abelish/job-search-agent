"""
One-time script to obtain a Gmail OAuth refresh token.

Prerequisites:
  1. Google Cloud Console: create a project, enable the Gmail API.
  2. Create an OAuth 2.0 client ID (type: Desktop app), download the JSON.
  3. Copy client_id and client_secret into .env as GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET.
  4. Run:  python get_gmail_token.py

A browser window will open for Google sign-in. After approving, the refresh
token is printed — paste it into .env as GMAIL_REFRESH_TOKEN.
"""

import os
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

load_dotenv()

client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()

if not client_id:
    client_id = input("GMAIL_CLIENT_ID: ").strip()
if not client_secret:
    client_secret = input("GMAIL_CLIENT_SECRET: ").strip()

if not client_id or not client_secret:
    print("Error: both GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET are required.")
    sys.exit(1)

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

print("Opening browser for Google sign-in...")
flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

print("\n" + "=" * 60)
print("Add this line to your .env file:\n")
print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
print("=" * 60)
