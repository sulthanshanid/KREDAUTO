#!/usr/bin/env python3
"""
autoclock_full.py

- Runs in Asia/Kolkata timezone
- Sends punch at 10:02 and 18:02 each day
- Chooses a random image from ./images, encodes to data URL
- Uses provided login endpoint to get token and cookies
- If punch != 200, re-login and retry once
- Persists token and cookies in auth.json
"""

import base64
import datetime
import json
import mimetypes
import os
import random
import signal
import sys
import time
from zoneinfo import ZoneInfo

import requests

# ---------- CONFIG ----------
TZ = ZoneInfo("Asia/Kolkata")
IMAGES_DIR = "images"
AUTH_FILE = "auth.json"

# Endpoints & payloads (use the URLs you provided)
LOGIN_URL = "https://app.kredily.com:443/ws/v1/accounts/api-token-auth/"
PUNCH_URL = "https://app.kredily.com:443/ws/v1/attendance-log/punch/"

# Login headers + body you provided
LOGIN_HEADERS = {
    "Versionname": "2.12.2",
    "Versioncode": "283",
    "Applicationtype": "Android",
    "Applicationid": "d4308674-1a69-4f2a-a6a0-a9e15c4264b8",
    "Language": "en",
    "X-Requested-With": "com.kredily.app",
    "Content-Type": "application/json; charset=UTF-8",
    "Accept-Encoding": "gzip, deflate, br",
    "User-Agent": "okhttp/4.9.1",
}
LOGIN_JSON = {"consent": True, "password": "Shanid@123", "username": "muzu04994@gmail.com"}

# Punch header template (Authorization will be set dynamically)
PUNCH_HEADERS_TEMPLATE = {
    "Versionname": "2.12.2",
    "Versioncode": "283",
    "Applicationtype": "Android",
    "Applicationid": "d4308674-1a69-4f2a-a6a0-a9e15c4264b8",
    "Language": "en",
    "X-Requested-With": "com.kredily.app",
    "Content-Type": "application/json; charset=UTF-8",
    "Accept-Encoding": "gzip, deflate, br",
    "User-Agent": "okhttp/4.9.1",
}

# Punch JSON body template; selfie_image will be filled
PUNCH_JSON_TEMPLATE = {
    "app_version": "2.12.2",
    "auto_clock_punch": False,
    "clock_lat": 33.985805,
    "clock_long": -118.2541117,
    "device_model_id": "CPH2365",
    "device_name": "OPPO",
    "os_version": "android 13",
    "platform": "kredilylite",
    "prev_punch_count": 0,
    "real_time_lat": 0.0,
    "real_time_long": 0.0,
    "selfie_image": None,
}
# ----------------------------

session = requests.Session()
running = True


def now_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def save_auth(token, cookies):
    """Persist token and cookies to disk."""
    try:
        data = {"token": token, "cookies": {}}
        for name, cookie in cookies.items():
            data["cookies"][name] = cookie
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print(f"[{now_str()}] Saved auth to {AUTH_FILE}")
    except Exception as e:
        print(f"[{now_str()}] Failed to save auth: {e}")


def load_auth():
    """Load token and cookies from disk if present. Returns (token, cookies_dict)"""
    if not os.path.isfile(AUTH_FILE):
        return None, {}
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("token")
        cookies = data.get("cookies", {})
        return token, cookies
    except Exception as e:
        print(f"[{now_str()}] Failed to load auth: {e}")
        return None, {}


def set_session_cookies_from_dict(cookies_dict):
    if not cookies_dict:
        return
    jar = requests.cookies.RequestsCookieJar()
    for k, v in cookies_dict.items():
        jar.set(k, v)
    session.cookies.update(jar)


def login_get_token_and_cookies():
    """Login and return (token_string_or_None, cookies_dict_or_empty). Also updates session cookies."""
    try:
        r = session.post(LOGIN_URL, headers=LOGIN_HEADERS, json=LOGIN_JSON, timeout=20)
    except Exception as e:
        print(f"[{now_str()}] Login exception: {e}")
        return None, {}
    if r.status_code not in (200, 201):
        print(f"[{now_str()}] Login returned status {r.status_code}. Body: {r.text[:400]}")
        # still try to parse JSON if any
    # update session cookies (requests will update session.cookies automatically)
    try:
        data = r.json()
    except Exception:
        print(f"[{now_str()}] Login response not JSON: {r.text[:400]}")
        data = {}

    # typical structure includes "token" key
    token = None
    if isinstance(data, dict):
        # direct keys
        token = data.get("token") or data.get("auth_token") or data.get("access") or data.get("key")
        # nested check (1-level)
        if not token:
            for v in data.values():
                if isinstance(v, dict):
                    token = v.get("token") or v.get("auth_token") or v.get("access") or v.get("key")
                    if token:
                        break

    # extract cookies from response.cookies
    cookies_dict = {}
    for c in r.cookies:
        cookies_dict[c.name] = c.value

    # ensure session uses cookies
    set_session_cookies_from_dict(cookies_dict)

    if token:
        print(f"[{now_str()}] Login obtained token (len={len(token)}) and cookies: {list(cookies_dict.keys())}")
    else:
        print(f"[{now_str()}] Login did not return a token in JSON. Cookies: {list(cookies_dict.keys())}")

    return token, cookies_dict


def image_to_data_url(path):
    """Encode image file to a data URL string with guessed mime type."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    mtype, _ = mimetypes.guess_type(path)
    if not mtype:
        # fallback
        mtype = "image/webp"
    with open(path, "rb") as f:
        b = f.read()
    encoded = base64.b64encode(b).decode("utf-8")
    return f"data:{mtype};base64,{encoded}"


def choose_random_image(images_dir=IMAGES_DIR):
    if not os.path.isdir(images_dir):
        raise FileNotFoundError(f"Images directory not found: {images_dir}")
    files = [f for f in os.listdir(images_dir) if os.path.isfile(os.path.join(images_dir, f))]
    if not files:
        raise FileNotFoundError(f"No files in images directory: {images_dir}")
    return os.path.join(images_dir, random.choice(files))


def send_punch(token, image_path):
    """Send the punch. Returns requests.Response or None."""
    headers = PUNCH_HEADERS_TEMPLATE.copy()
    # ensure header "Authorization": "Token <token>"
    if token:
        if token.startswith("Token "):
            headers["Authorization"] = token
        else:
            headers["Authorization"] = f"Token {token}"
    body = PUNCH_JSON_TEMPLATE.copy()
    body["selfie_image"] = image_to_data_url(image_path)
    try:
        r = session.post(PUNCH_URL, headers=headers, json=body, timeout=30)
        return r
    except Exception as e:
        print(f"[{now_str()}] Punch request exception: {e}")
        return None


def next_run_time(now=None):
    if now is None:
        now = datetime.datetime.now(TZ)
    today = now.date()
    t1 = datetime.datetime.combine(today, datetime.time(1, 14, 0), tzinfo=TZ)
    t2 = datetime.datetime.combine(today, datetime.time(1, 15, 0), tzinfo=TZ)
    if now < t1:
        return t1
    if now < t2:
        return t2
    # otherwise tomorrow 10:02
    tomorrow = today + datetime.timedelta(days=1)
    return datetime.datetime.combine(tomorrow, datetime.time(1, 14, 0), tzinfo=TZ)


def handle_sigint(signum, frame):
    global running
    print(f"\n[{now_str()}] Exit signal received, shutting down...")
    running = False


signal.signal(signal.SIGINT, handle_sigint)
signal.signal(signal.SIGTERM, handle_sigint)


def bootstrap_auth():
    """Load auth from disk; if not present or token missing, perform login."""
    token, cookies = load_auth()
    if token:
        print(f"[{now_str()}] Loaded token from {AUTH_FILE} (len={len(token) if token else 0})")
    if cookies:
        print(f"[{now_str()}] Loaded cookies from {AUTH_FILE}: {list(cookies.keys())}")
        set_session_cookies_from_dict(cookies)
    if not token:
        # login now
        token, cookies = login_get_token_and_cookies()
        if token:
            save_auth(token, cookies)
    return token


def main_loop():
    current_token = bootstrap_auth()
    print(f"[{now_str()}] Autoclock started. Timezone={TZ}")

    while running:
        nr = next_run_time()
        now = datetime.datetime.now(TZ)
        wait_seconds = (nr - now).total_seconds()
        print(f"[{now_str()}] Next scheduled punch at {nr.strftime('%Y-%m-%d %H:%M:%S %Z')} (in {int(wait_seconds)}s)")

        # sleep in short chunks so we remain responsive to signals
        while running and datetime.datetime.now(TZ) < nr:
            sleep_for = min(30, max(0.5, (nr - datetime.datetime.now(TZ)).total_seconds()))
            time.sleep(sleep_for)

        if not running:
            break

        # time to punch
        try:
            image_path = choose_random_image(IMAGES_DIR)
        except Exception as e:
            print(f"[{now_str()}] Error selecting image: {e}. Skipping this slot.")
            time.sleep(5)
            continue

        print(f"[{now_str()}] Using image: {image_path}. Preparing to punch...")

        # If no token, try to login once
        if not current_token:
            print(f"[{now_str()}] No token present. Logging in...")
            token, cookies = login_get_token_and_cookies()
            if token:
                current_token = token
                save_auth(token, cookies)
            else:
                print(f"[{now_str()}] Login failed. Will retry next scheduled time.")
                continue

        # 1st attempt
        r = send_punch(current_token, image_path)
        if r is None:
            print(f"[{now_str()}] Punch attempt raised exception. Trying to re-authenticate and retry once...")
            token, cookies = login_get_token_and_cookies()
            if token:
                current_token = token
                save_auth(token, cookies)
                r = send_punch(current_token, image_path)
        else:
            if r.status_code != 201:
                # try re-login and retry once
                print(f"[{now_str()}] Punch returned status {r.status_code}. Body (truncated): {r.text[:500]}")
                print(f"[{now_str()}] Re-authenticating and retrying once...")
                token, cookies = login_get_token_and_cookies()
                if token:
                    current_token = token
                    save_auth(token, cookies)
                    r = send_punch(current_token, image_path)

        # Final result handling
        if r is None:
            print(f"[{now_str()}] Final punch attempt failed with exception.")
        else:
            if r.status_code == 201:
                try:
                    js = r.json()
                    summary = json.dumps(js, ensure_ascii=False)[:800]
                except Exception:
                    summary = r.text[:800]
                print(f"[{now_str()}] Punch SUCCESS. Response: {summary}")
            else:
                print(f"[{now_str()}] Punch FAILED. Status {r.status_code}. Body: {r.text[:1200]}")

        # pause briefly before scheduling next
        time.sleep(2)

    print(f"[{now_str()}] Autoclock stopped.")


if __name__ == "__main__":
    # ensure images dir exists
    if not os.path.isdir(IMAGES_DIR):
        print(f"Images directory '{IMAGES_DIR}' not found. Create it and add images, then run again.")
        sys.exit(1)
    try:
        main_loop()
    except Exception as e:
        print(f"[{now_str()}] Fatal exception: {e}")
        raise
