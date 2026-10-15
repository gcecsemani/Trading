"""
login_zerodha.py
================
One-time daily Zerodha login helper.

Run this script ONCE each morning before starting the trading bot.
It generates a fresh access token and saves it to .env automatically.

Usage:
    python login_zerodha.py

Steps:
    1. Script opens the Zerodha login page in your browser.
    2. Log in with your Zerodha credentials + 2FA.
    3. After success you are redirected to a URL like:
           https://127.0.0.1/?request_token=XXXXXXXX&action=login&status=success
    4. Copy the FULL URL (or just the request_token value) and paste it when asked.
    5. Access token is written to .env → you can now run  python main.py
"""

import os
import re
import sys
import webbrowser

from dotenv import load_dotenv, set_key
from kiteconnect import KiteConnect

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

load_dotenv(ENV_FILE)

API_KEY    = os.getenv("ZERODHA_API_KEY",    "").strip()
API_SECRET = os.getenv("ZERODHA_API_SECRET", "").strip()


def _banner(text: str) -> None:
    print("\n" + "═" * 60)
    print(f"  {text}")
    print("═" * 60)


def main() -> None:
    _banner("ZERODHA DAILY LOGIN HELPER")

    # ── Validate .env ─────────────────────────────────────────────────────────
    if not API_KEY or API_KEY == "your_api_key_here":
        print("\n❌  ZERODHA_API_KEY is not set in .env")
        print("    1. Go to https://developers.kite.trade/")
        print("    2. Create an app and copy the API key")
        print("    3. Paste it as  ZERODHA_API_KEY=<value>  in .env")
        sys.exit(1)

    if not API_SECRET or API_SECRET == "your_api_secret_here":
        print("\n❌  ZERODHA_API_SECRET is not set in .env")
        sys.exit(1)

    # ── Build login URL ───────────────────────────────────────────────────────
    kite      = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    print("\n📌 Step 1 – Open this URL in your browser (auto-opening now):\n")
    print(f"   {login_url}\n")

    try:
        webbrowser.open(login_url)
        print("   (Browser opened automatically)")
    except Exception:
        print("   ⚠️  Could not open browser automatically – copy the URL above")

    print("\n📌 Step 2 – Log in with your Zerodha credentials + TOTP / PIN")
    print("\n📌 Step 3 – After login you will be redirected to a URL like:")
    print("   https://127.0.0.1/?request_token=XXXXXXXXXXXXXXXX&action=login&status=success")
    print("\n📌 Step 4 – Copy that FULL URL (or just the request_token value) and paste below.")

    raw = input("\n🔑  Paste URL or request_token here: ").strip()
    if not raw:
        print("❌  Nothing entered – aborting.")
        sys.exit(1)

    # Extract token from full URL if user pasted the whole redirect URL
    match = re.search(r"request_token=([A-Za-z0-9]+)", raw)
    request_token = match.group(1) if match else raw

    print(f"\n   Using request_token: {request_token[:6]}…{request_token[-4:]}")

    # ── Exchange for access token ─────────────────────────────────────────────
    try:
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    except Exception as exc:
        print(f"\n❌  Session generation failed: {exc}")
        print("    Make sure ZERODHA_API_SECRET is correct and the token wasn't already used.")
        sys.exit(1)

    access_token = session_data["access_token"]
    user_id      = session_data.get("user_id", "")
    user_name    = session_data.get("user_name", "")

    # ── Save to .env ──────────────────────────────────────────────────────────
    set_key(ENV_FILE, "ZERODHA_ACCESS_TOKEN", access_token)
    if user_id:
        set_key(ENV_FILE, "ZERODHA_USER_ID", user_id)

    _banner("LOGIN SUCCESSFUL")
    print(f"  User     : {user_name} ({user_id})")
    print(f"  Token    : {access_token[:8]}…{access_token[-6:]}")
    print(f"  Saved to : {ENV_FILE}")
    print("\n🚀  You can now start the bot:\n       python main.py\n")


if __name__ == "__main__":
    main()
