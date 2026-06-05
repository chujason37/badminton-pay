#!/usr/bin/env python3
"""
One-time Gmail OAuth setup.

Run this locally to authorise the app to read your Gmail inbox:

    pip install google-auth-oauthlib
    python gmail_setup.py

Prerequisites:
  1. Go to https://console.cloud.google.com/
  2. Create a project (or use an existing one)
  3. Enable the Gmail API (APIs & Services → Library → Gmail API → Enable)
  4. Create OAuth credentials:
       APIs & Services → Credentials → Create Credentials → OAuth client ID
       Application type: Desktop app
  5. Download the JSON file and save it as "credentials.json" in this directory.

After this script completes, copy the printed JSON into the GMAIL_TOKEN_JSON
environment variable on Railway (or wherever you deploy).
"""
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_FILE = Path("credentials.json")
TOKEN_FILE = Path("gmail_token.json")

if not CREDS_FILE.exists():
    print(f"ERROR: {CREDS_FILE} not found.")
    print("Download your OAuth client credentials from Google Cloud Console and")
    print("save them as credentials.json in this directory.")
    sys.exit(1)

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("ERROR: google-auth-oauthlib not installed.")
    print("Run: pip install google-auth-oauthlib")
    sys.exit(1)

print("Opening browser for Google OAuth authorisation...")
flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
creds = flow.run_local_server(port=0)

token_json = creds.to_json()
TOKEN_FILE.write_text(token_json)

print(f"\nToken saved to {TOKEN_FILE}")
print("\n" + "=" * 60)
print("Copy the following as GMAIL_TOKEN_JSON in Railway:")
print("=" * 60)
print(token_json)
print("=" * 60)
print(f"\nOr run: cat {TOKEN_FILE}")
