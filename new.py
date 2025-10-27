#!/usr/bin/env python3
"""
AutoClock (India timezone)

- Uses Asia/Kolkata timezone
- Random clock-in between 10:00:00 and 10:10:00
- Random clock-out between 18:00:00 and 18:08:00
- 6 outfit folders inside IMAGES_DIR named "1".."6"
- If clock-in used folder N, clock-out uses a different image from same folder N
- State persisted in session_state.json so restarts won't lose the chosen outfit
"""

import os
import json
import base64
import random
import time
import datetime
import signal
import sys
from zoneinfo import ZoneInfo

import requests

# -------- CONFIG --------
EMAIL = "muzu04994@gmail.com"
PASSWORD = "Shanid@123"

IMAGES_DIR = "images"               # contains subfolders "1", "2", ... "6"
STATE_FILE = "state/session_state.json"   # persists token, last_outfit, clock_in_image, date
LOGIN_URL = "https://app.kredily.com/ws/v1/accounts/api-token-auth/"
PUNCH_URL = "https://app.kredily.com/ws/v1/attendance-log/punch/"

HEADERS_BASE = {
    "Versionname": "2.12.2",
    "Versioncode": "283",
    "Applicationtype": "Android",
    "Applicationid": "d4308674-1a69-4f2a-a6a0-a9e15c4264b8",
    "Language": "en",
    "X-Requested-With": "com.kredily.app",
    "Content-Type": "application/json; charset=UTF-8",
    "Accept-Encoding": "gzip, deflate, br",
    "User-Agent": "okhttp/4.9.1"
}

TZ = ZoneInfo("Asia/Kolkata")

# windows (inclusive)
MORNING_WINDOW_START = datetime.time(10, 0, 45)
MORNING_WINDOW_END = datetime.time(10, 15, 45)
EVENING_WINDOW_START = datetime.time(18, 3, 0)
EVENING_WINDOW_END = datetime.time(18, 10, 0)

# ------------------------


# Replace with your actual bot token
BOT_TOKEN = "8025422826:AAEezLn7fN_6cisTZmvmAuMlQRwmnB3xKgw"

# Replace with your channel username or chat ID (with '@' for public channels)
CHANNEL_ID = "@kreduto"  

# Local image path or direct URL


# Telegram API endpoint

running = True
session = requests.Session()


def now_kolkata():
    return datetime.datetime.now(TZ)


def now_date_str():
    return now_kolkata().date().isoformat()


def load_state():
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[{now_kolkata()}] Failed to save state: {e}")


def login_get_token_and_cookies():
    """Perform login; return (token, cookies_dict)."""
    try:
        r = session.post(LOGIN_URL, headers=HEADERS_BASE, json={
            "consent": True, "password": PASSWORD, "username": EMAIL
        }, timeout=20)
    except Exception as e:
        print(f"[{now_kolkata()}] Login exception: {e}")
        return None, {}

    cookies_dict = {c.name: c.value for c in r.cookies}
    token = None
    try:
        js = r.json()
        # common keys
        for k in ("token", "auth_token", "access", "key"):
            if k in js:
                token = js[k]
                break
        if not token:
            # try one-level nested
            for v in js.values():
                if isinstance(v, dict):
                    for k in ("token", "auth_token", "access", "key"):
                        if k in v:
                            token = v[k]
                            break
                    if token:
                        break
    except Exception:
        token = None

    if token:
        print(f"[{now_kolkata()}] Login succeeded, token length={len(token)} cookies={list(cookies_dict.keys())}")
    else:
        print(f"[{now_kolkata()}] Login response didn't include token JSON. Status {r.status_code}. Cookies: {list(cookies_dict.keys())}")
    return token, cookies_dict


def encode_image_to_data_url(filepath):
    with open(filepath, "rb") as f:
        b = f.read()
    # Guess mime type by extension; default webp if unknown
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif ext == ".png":
        mime = "image/png"
    elif ext == ".webp":
        mime = "image/webp"
    else:
        mime = "image/webp"
    return f"data:{mime};base64," + base64.b64encode(b).decode("utf-8")


def send_punch(token, image_path):
    headers = HEADERS_BASE.copy()
    if token:
        if token.startswith("Token "):
            headers["Authorization"] = token
        else:
            headers["Authorization"] = f"Token {token}"
    body = {
        "app_version": "2.12.2",
        "auto_clock_punch": False,
        "clock_lat": 33.985805,
        "clock_long": -118.2541117,
        "device_model_id": "SM-G977N",
        "device_name": "samsung",
        "os_version": "android 12",
        "platform": "kredilylite",
        "prev_punch_count": 0,
        "real_time_lat": 0.0,
        "real_time_long": 0.0,
        "selfie_image": encode_image_to_data_url(image_path)
    }
    try:
        r = session.post(PUNCH_URL, headers=headers, json=body, timeout=30)
        return r
    except Exception as e:
        print(f"[{now_kolkata()}] Punch request exception: {e}")
        return None


def get_random_time_in_window(date: datetime.date, start_time: datetime.time, end_time: datetime.time):
    """Return a timezone-aware datetime on given date in TZ between start_time and end_time (inclusive)."""
    start_dt = datetime.datetime.combine(date, start_time).replace(tzinfo=TZ)
    end_dt = datetime.datetime.combine(date, end_time).replace(tzinfo=TZ)
    total_seconds = int((end_dt - start_dt).total_seconds())
    if total_seconds < 0:
        # window wraps or invalid; return start
        return start_dt
    offset = random.randint(0, total_seconds)
    return start_dt + datetime.timedelta(seconds=offset)


def sleep_until(target_dt: datetime.datetime):
    """Sleep until the given tz-aware datetime (in small chunks to be interruptible)."""
    while running:
        now = now_kolkata()
        seconds = (target_dt - now).total_seconds()
        if seconds <= 0:
            return
        # sleep in chunks
        time.sleep(min(30, max(0.5, seconds)))


def choose_random_image_from_folder(folder_path, exclude_filename=None):
    files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    if not files:
        raise FileNotFoundError(f"No files in folder {folder_path}")
    if exclude_filename and exclude_filename in files and len(files) > 1:
        files = [f for f in files if f != exclude_filename]
    if not files:
        raise FileNotFoundError(f"No available images in folder after excluding {exclude_filename}")
    return os.path.join(folder_path, random.choice(files))


def next_event_datetime_and_type(state):
    """
    Decide next event datetime and whether it's a 'clock_in' or 'clock_out'.
    Uses state to know whether today's morning punch was already done.
    """
    today = now_kolkata().date()
    date_str = state.get("date")
    morning_done = state.get("morning_done", False) if date_str == today.isoformat() else False
    evening_done = state.get("evening_done", False) if date_str == today.isoformat() else False

    # If morning not done and current time before morning window end -> schedule morning random time
    now = now_kolkata()
    morning_end_dt = datetime.datetime.combine(today, MORNING_WINDOW_END).replace(tzinfo=TZ)
    evening_end_dt = datetime.datetime.combine(today, EVENING_WINDOW_END).replace(tzinfo=TZ)

    if not morning_done and now < morning_end_dt:
        target = get_random_time_in_window(today, MORNING_WINDOW_START, MORNING_WINDOW_END)
        # If random time already passed (rare), schedule next possible - either evening or tomorrow
        if target <= now:
            # if still before evening window, schedule evening; else tomorrow morning
            if now < datetime.datetime.combine(today, EVENING_WINDOW_END).replace(tzinfo=TZ):
                target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
                return target, "clock_out"
            else:
                tomorrow = today + datetime.timedelta(days=1)
                target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
                return target, "clock_in"
        return target, "clock_in"

    # If morning done but evening not done and now before evening end -> schedule evening random time
    if morning_done and not evening_done and now < evening_end_dt:
        target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
        if target <= now:
            # if it's already past evening window end, schedule next day's morning
            tomorrow = today + datetime.timedelta(days=1)
            target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
            return target, "clock_in"
        return target, "clock_out"

    # If both done for today or we're past today's windows -> schedule tomorrow morning
    tomorrow = today + datetime.timedelta(days=1)
    target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
    return target, "clock_in"


def graceful_shutdown(signum, frame):
    global running
    print(f"\n[{now_kolkata()}] Received signal {signum}, shutting down gracefully...")
    running = False


def ensure_images_structure():
    # check IMAGES_DIR exists and subfolders 1..6 exist
    if not os.path.isdir(IMAGES_DIR):
        raise FileNotFoundError(f"Images directory '{IMAGES_DIR}' does not exist.")
    for i in range(1, 7):
        p = os.path.join(IMAGES_DIR, str(i))
        if not os.path.isdir(p):
            raise FileNotFoundError(f"Expected outfit folder missing: {p}")


def main_loop():
    global running
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    ensure_images_structure()

    state = load_state()
    # reset state if date mismatch
    if state.get("date") != now_date_str():
        state = {"date": now_date_str(), "morning_done": False, "evening_done": False}
        save_state(state)

    token = state.get("token")
    if not token:
        token, cookies = login_get_token_and_cookies()
        if token:
            state["token"] = token
            # store cookies if any (not necessary but saved)
            state["cookies"] = cookies
            save_state(state)

    print(f"[{now_kolkata()}] AutoClock started (India timezone).")

    while running:
        # refresh state from disk in case of external edits
        state = load_state()
        # if state date is old, reset day's flags
        if state.get("date") != now_date_str():
            state["date"] = now_date_str()
            state["morning_done"] = False
            state["evening_done"] = False
            # clear last_outfit & clock_in_image for new day
            state.pop("last_outfit", None)
            state.pop("clock_in_image", None)
            save_state(state)

        target_dt, action_type = next_event_datetime_and_type(state)
        print(f"[{now_kolkata()}] Next event: {action_type} at {target_dt.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Sleep until the chosen random time (but remain interruptible)
        sleep_until(target_dt)

        if not running:
            break

        # At scheduled time: pick image & perform punch
        try:
            if action_type == "clock_in":
                outfit_folder = str(random.randint(1, 6))
                folder_path = os.path.join(IMAGES_DIR, outfit_folder)
                image_path = choose_random_image_from_folder(folder_path)
                # persist choice
                state["last_outfit"] = outfit_folder
                state["clock_in_image"] = os.path.basename(image_path)
                # morning done
                state["morning_done"] = True
                state["date"] = now_date_str()
                save_state(state)
                print(f"[{now_kolkata()}] CLOCK-IN using folder {outfit_folder}, image {os.path.basename(image_path)}")
            else:
                outfit_folder = state.get("last_outfit")
                if not outfit_folder:
                    # fallback: random outfit
                    outfit_folder = str(random.randint(1, 6))
                    print(f"[{now_kolkata()}] WARNING: no last_outfit in state; choosing random folder {outfit_folder}")
                folder_path = os.path.join(IMAGES_DIR, outfit_folder)
                exclude = state.get("clock_in_image")
                image_path = choose_random_image_from_folder(folder_path, exclude_filename=exclude)
                state["evening_done"] = True
                state["date"] = now_date_str()
                save_state(state)
                print(f"[{now_kolkata()}] CLOCK-OUT using same folder {outfit_folder}, image {os.path.basename(image_path)}")

            # attempt punch
            token = state.get("token")
            if not token:
                token, cookies = login_get_token_and_cookies()
                if token:
                    state["token"] = token
                    state["cookies"] = cookies
                    save_state(state)

            r = send_punch(token, image_path)
            # If exception or non-200, re-login and retry once
            if r is None or r.status_code != 201:
                print(f"[{now_kolkata()}] Punch failed or non-200. Will attempt re-login and retry once.")
                token, cookies = login_get_token_and_cookies()
                if token:
                    state["token"] = token
                    state["cookies"] = cookies
                    save_state(state)
                    r = send_punch(token, image_path)

            if r is None:
                print(f"[{now_kolkata()}] Final punch attempt raised exception.")
            else:
                if r.status_code == 201:
                    try:
                        resp = r.json()
                        print(f"[{now_kolkata()}] Punch SUCCESS. Response summary: {json.dumps(resp)[:400]}")
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

# Open the image file in binary mode
                        with open(image_path, "rb") as image_file:
                            files = {"photo": image_file}
                            data = {"chat_id": CHANNEL_ID, "caption": "punched successfully!"}
                            response = requests.post(url, data=data, files=files)

                        # Print the response for debugging
                        print(response.json())

                    except Exception:
                        print(f"[{now_kolkata()}] Punch SUCCESS. Raw body truncated: {r.text[:400]}")
                else:
                    print(f"[{now_kolkata()}] Punch FAILED after retry. Status {r.status_code}. Response: {r.text[:800]}")

            # If both morning and evening done for the day, optionally clear clock_in_image to avoid reuse next day
            if state.get("morning_done") and state.get("evening_done"):
                # preserve token but clear outfit info for next day
                token_keep = state.get("token")
                state = {"date": now_date_str(), "morning_done": True, "evening_done": True}
                # we keep token and cookies until day rollover; but clear outfit info
                if token_keep:
                    state["token"] = token_keep
                save_state(state)

        except Exception as exc:
            print(f"[{now_kolkata()}] Unexpected error during scheduled action: {exc}")

        # small sleep to avoid immediate tight loop
        time.sleep(2)

    print(f"[{now_kolkata()}] AutoClock stopped.")


if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        print(f"[{now_kolkata()}] Fatal error: {e}")
        sys.exit(1)
