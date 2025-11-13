#!/usr/bin/env python3
"""
AutoClock (India timezone) — Patched script.

Changes made:
- Keeps using the clocking-widget API as the source of truth.
- After a successful punch (HTTP 201), poll the widget API a few times (timeout/backoff)
  to confirm the server updated prev_punch_count / clock_in. If it doesn't, fall back
  to the punch response's clockWidget and update local state to avoid scheduling the wrong next event.
- Ensure /forceout uses the same outfit folder as the corresponding clock-in if still valid for today.
- Retry Telegram and web requests up to 2 attempts.
- Send Telegram notification on Sunday & company holiday.
- Rotate/truncate the log file so it only keeps the last 2 days' entries.
"""

import os
import json
import base64
import random
import time
import datetime
import signal
import sys
import threading
import re
from zoneinfo import ZoneInfo
import requests
import logging

# -------- CONFIG --------
EMAIL = "9544790012"
PASSWORD = "Shanid@786"

IMAGES_DIR = "images"               # contains subfolders "1", "2", ... "6"
STATE_FILE = "state/session_state.json"   # persists token, last_outfit, clock_in_image, date, flags
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

LOGIN_URL = "https://app.kredily.com/ws/v1/accounts/api-token-auth/"
PUNCH_URL = "https://app.kredily.com/ws/v1/attendance-log/punch/"
WIDGET_URL = "https://app.kredily.com/ws/v1/attendance-log/clocking-widget-api/"
HOLIDAY_API = "https://app.kredily.com/ws/v2/company/get-event-by-month/"  # expects ?from=<epoch_ms_of_month_start>

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
MORNING_WINDOW_START = datetime.time(10, 8, 0)
MORNING_WINDOW_END = datetime.time(10, 15, 0)
EVENING_WINDOW_START = datetime.time(18, 5, 10)
EVENING_WINDOW_END = datetime.time(18, 8, 0)

PRE_ALERT_MINUTES = 10  # pre-alert before punch

# Telegram config
BOT_TOKEN = "8179352079:AAEbmNmkVLJvyqpIm7DK8G9BcJAn43U5_hA"
CHANNEL_ID = "631331311"  # where photos/notifications go
TELEGRAM_ADMIN_CHAT_ID = None

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOG_FILE = "autoclock.log"

# Retry schedule in seconds for network retries: immediate, then after 1, 5, 10 minutes
RETRY_DELAYS = [0, 60, 300, 600]

# ------------------------
running = True
session = requests.Session()
state_lock = threading.Lock()

# configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("autoclock")


# ----------------- Helpers -----------------
def now_kolkata():
    return datetime.datetime.now(TZ)


def now_date_str():
    return now_kolkata().date().isoformat()


def load_state():
    with state_lock:
        if not os.path.isfile(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_state(state):
    with state_lock:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.exception(f"Failed to save state: {e}")


def tail_log(lines=200):
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception as e:
        log.exception(f"tail_log failed: {e}")
        return ""


# ---------- Safe web request helper (2 attempts) ----------
def safe_request(method, url, max_attempts=2, backoff=2, **kwargs):
    """Simple wrapper to retry a requests.request call up to max_attempts (default 2)."""
    for attempt in range(1, max_attempts + 1):
        try:
            r = session.request(method, url, **kwargs)
            if r.ok:
                return r
            else:
                log.warning(f"Request {method} {url} returned {r.status_code} on attempt {attempt}: {r.text[:200]}")
        except Exception as e:
            log.exception(f"Request exception {method} {url} attempt {attempt}: {e}")
        if attempt < max_attempts:
            time.sleep(backoff)
    log.error(f"Request {method} {url} failed after {max_attempts} attempts.")
    return None


# ---------- Telegram helpers (2 attempts) ----------
def send_telegram_message(chat_id, text):
    for attempt in range(1, 3):
        try:
            url = TELEGRAM_API_BASE + "/sendMessage"
            payload = {"chat_id": chat_id, "text": text}
            r = safe_request("POST", url, json=payload, timeout=10)
            if r and r.ok:
                return True
        except Exception as e:
            log.exception(f"Telegram sendMessage exception attempt {attempt}: {e}")
        time.sleep(2)
    log.error(f"Telegram sendMessage failed after retries: {text[:80]}")
    return False


def send_telegram_photo(chat_id, image_path, caption=None):
    for attempt in range(1, 3):
        try:
            url = TELEGRAM_API_BASE + "/sendPhoto"
            with open(image_path, "rb") as image_file:
                files = {"photo": image_file}
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                r = safe_request("POST", url, data=data, files=files, timeout=30)
            if r and r.ok:
                return True
        except Exception as e:
            log.exception(f"sendPhoto exception attempt {attempt}: {e}")
        time.sleep(2)
    log.error(f"sendPhoto ultimately failed for {image_path}")
    return False


def send_telegram_document(chat_id, file_path, caption=None):
    for attempt in range(1, 3):
        try:
            url = TELEGRAM_API_BASE + "/sendDocument"
            with open(file_path, "rb") as f:
                files = {"document": f}
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                r = safe_request("POST", url, data=data, files=files, timeout=60)
            if r and r.ok:
                return True
        except Exception as e:
            log.exception(f"sendDocument exception attempt {attempt}: {e}")
        time.sleep(2)
    log.error(f"sendDocument ultimately failed for {file_path}")
    return False


def download_telegram_file(file_id, dest_path):
    for attempt in range(1, 3):
        try:
            r = safe_request("GET", f"{TELEGRAM_API_BASE}/getFile", params={"file_id": file_id}, timeout=15)
            if not r:
                continue
            js = r.json()
            file_path = js.get("result", {}).get("file_path")
            if not file_path:
                log.warning("No file_path in getFile response")
                continue
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            rr = safe_request("GET", url, timeout=30)
            if not rr:
                continue
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(rr.content)
            return True
        except Exception as e:
            log.exception(f"download_telegram_file exception attempt {attempt}: {e}")
        time.sleep(2)
    log.error(f"download_telegram_file failed for file_id {file_id}")
    return False


# ---------- Image encoding and selection ----------
def encode_image_to_data_url(filepath):
    with open(filepath, "rb") as f:
        b = f.read()
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif ext == ".png":
        mime = "image/png"
    elif ext == ".webp":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(b).decode("utf-8")


def choose_random_image_from_folder(folder_path, exclude_filename=None):
    files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    if exclude_filename and exclude_filename in files and len(files) > 1:
        files = [f for f in files if f != exclude_filename]
    if not files:
        raise FileNotFoundError(f"No files in folder {folder_path}")
    return os.path.join(folder_path, random.choice(files))


# ---------- Login ----------
def login_get_token_and_cookies_once():
    """Single attempt login; return (token, cookies_dict) or (None, {})."""
    try:
        r = safe_request("POST", LOGIN_URL, headers=HEADERS_BASE,
                         json={"consent": True, "password": PASSWORD, "username": EMAIL}, timeout=20)
    except Exception as e:
        log.exception(f"Login exception: {e}")
        return None, {}

    if not r:
        return None, {}

    cookies_dict = {c.name: c.value for c in r.cookies}
    token = None
    try:
        js = r.json()
        for k in ("token", "auth_token", "access", "key"):
            if k in js:
                token = js[k]
                break
        if not token:
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
        log.info(f"Login succeeded, token length={len(token)} cookies={list(cookies_dict.keys())}")
    else:
        log.warning(f"Login response didn't include token JSON. Status {getattr(r,'status_code',None)}")
        try:
            send_telegram_message(CHANNEL_ID, "⚠️ Login is failing (check credentials or network).")
        except Exception:
            pass
    return token, cookies_dict


def retry_login_attempts():
    """Attempt login with retry delays. Returns (token, cookies) or (None, {})."""
    for delay in RETRY_DELAYS:
        if delay:
            log.info(f"Waiting {delay} seconds before login attempt")
            time.sleep(delay)
        token, cookies = login_get_token_and_cookies_once()
        if token:
            return token, cookies
    return None, {}


# ---------- Widget validation & fetch ----------
def fetch_clocking_widget(token):
    """Return parsed JSON['data'] from the clocking-widget API or None on error."""
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    try:
        r = safe_request("GET", WIDGET_URL, headers=headers, timeout=15)
        if not r:
            log.warning("Clocking-widget GET failed (no response).")
            return None
        if not r.ok:
            log.warning(f"Clocking-widget GET non-OK {r.status_code}: {r.text[:200]}")
            return None
        js = r.json()
        return js.get("data", {}) if isinstance(js, dict) else None
    except Exception as e:
        log.exception(f"fetch_clocking_widget exception: {e}")
        return None


def validate_widget_for_action(token, action):
    """
    Validate server widget before attempting action.
    Returns (allowed:bool, prev_count:int, reason:str).
    Note: server may be eventually consistent; caller should poll after punch success.
    """
    data = fetch_clocking_widget(token)
    if data is None:
        return False, None, "Failed to fetch clocking widget"

    prev = int(data.get("prev_punch_count", -1))
    clock_in_flag = data.get("clock_in")
    last_clock = data.get("last_clock_milisec")

    if action == "clock_in":
        if clock_in_flag is True and prev == 0:
            return True, prev, "OK"
        # allow borderline where server returns unexpected flag but prev==0 (trust prev_count)
        if prev == 0:
            return True, prev, "OK (prev_count==0, trusting prev_count despite clock_in flag)"
        return False, prev, f"Widget not suitable for clock_in: clock_in={clock_in_flag}, prev={prev}"
    elif action == "clock_out":
        if clock_in_flag is False and prev == 1 and last_clock:
            return True, prev, "OK"
        # some servers may show clock_in True but prev==1 (inconsistent) -> disallow but provide reason
        return False, prev, f"Widget not suitable for clock_out: clock_in={clock_in_flag}, prev={prev}, last_clock_present={bool(last_clock)}"
    else:
        return False, prev, "Unknown action"


# ---------- Poll widget until updated ----------
def poll_widget_until_updated(token, expect_prev_count=None, expect_clock_in=None, timeout=30, interval=3):
    """
    Poll the clocking-widget until it matches expected prev_count and clock_in flag,
    or until timeout (seconds) is reached. Returns the latest widget dict or None.
    If expect_* is None, that condition is ignored.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        w = fetch_clocking_widget(token)
        if w is None:
            last = None
        else:
            last = w
            try:
                ok_prev = True if expect_prev_count is None else (int(w.get("prev_punch_count", -1)) == int(expect_prev_count))
            except Exception:
                ok_prev = False
            ok_clock = True if expect_clock_in is None else (w.get("clock_in") == expect_clock_in)
            if ok_prev and ok_clock:
                return w
        time.sleep(interval)
    return last


# ---------- Punch functions ----------
def send_punch_once(token, image_path, prev_punch_count=0):
    headers = HEADERS_BASE.copy()
    if token:
        if token.startswith("Token "):
            headers["Authorization"] = token
        else:
            headers["Authorization"] = f"Token {token}"
    body = {
        "app_version": "2.12.2",
        "auto_clock_punch": False,
        "clock_lat": 8.5397988,
        "clock_long": 76.8934938,
        "device_model_id": "A95",
        "device_name": "OPPO",
        "os_version": "android 13",
        "platform": "kredilylite",
        "prev_punch_count": prev_punch_count,
        "real_time_lat": 0.0,
        "real_time_long": 0.0,
        "selfie_image": encode_image_to_data_url(image_path)
    }
    try:
        r = safe_request("POST", PUNCH_URL, headers=headers, json=body, timeout=30)
        return r
    except Exception as e:
        log.exception(f"Punch request exception: {e}")
        return None


def send_punch_with_retries(token, image_path, prev_punch_count=0):
    """Attempt punch with retries using RETRY_DELAYS schedule. Return final response or None."""
    last_r = None
    for i, delay in enumerate(RETRY_DELAYS):
        if delay:
            log.info(f"Waiting {delay} seconds before punch attempt #{i+1}")
            time.sleep(delay)
        r = send_punch_once(token, image_path, prev_punch_count)
        last_r = r
        if r is not None and getattr(r, "status_code", None) == 201:
            return r
    return last_r


# ---------- Outfit selection & image prep ----------
def select_outfit_folder(state):
    """
    Always avoid using yesterday's last_used_folder.
    Even if last_used_folder is reset to None for new day,
    keep yesterday's folder separately to prevent re-selection.
    """
    folders = [str(i) for i in range(1, 4)]

    # This stores yesterday's outfit (saved in state)
    yesterday_folder = state.get("yesterday_last_folder")

    # For a new day, do NOT reuse yesterday's folder
    available = [f for f in folders if f != yesterday_folder]

    # Safety: if somehow everything excluded, fallback to all
    if not available:
        available = folders

    chosen = random.choice(available)

    # save chosen as today's last_used_folder
    state["last_used_folder"] = chosen
    save_state(state)

    try:
        send_telegram_message(CHANNEL_ID, f"👕 Outfit: {chosen} ✅")
    except Exception:
        log.exception("Failed to send outfit notification")

    return chosen



def pick_clock_in_image(state):
    outfit_folder = select_outfit_folder(state)
    folder_path = os.path.join(IMAGES_DIR, outfit_folder)
    image_path = choose_random_image_from_folder(folder_path)
    state["clock_in_image"] = os.path.basename(image_path)
    state["last_used_folder"] = outfit_folder
    state["morning_done"] = True
    state["date"] = now_date_str()
    save_state(state)
    return image_path


def pick_clock_out_image(state):
    outfit_folder = state.get("last_used_folder")
    if not outfit_folder:
        # fallback: pick a folder (shouldn't happen normally)
        outfit_folder = select_outfit_folder(state)
    folder_path = os.path.join(IMAGES_DIR, outfit_folder)
    exclude = state.get("clock_in_image")
    image_path = choose_random_image_from_folder(folder_path, exclude_filename=exclude)
    state["evening_done"] = True
    state["date"] = now_date_str()
    save_state(state)
    return image_path


# ---------- Holidays ----------
def month_start_epoch_ms(dt: datetime.datetime):
    mstart = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(mstart.timestamp() * 1000)


def fetch_holidays(token):
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    params = {"from": str(month_start_epoch_ms(now_kolkata()))}
    try:
        r = safe_request("GET", HOLIDAY_API, headers=headers, params=params, timeout=20)
        if not r:
            log.warning("Holiday fetch failed (no response).")
            return []
        if not r.ok:
            log.warning(f"Holiday fetch non-OK {r.status_code}: {r.text[:200]}")
            return []
        js = r.json()
        return js.get("result", []) if isinstance(js, dict) else []
    except Exception as e:
        log.exception(f"fetch_holidays exception: {e}")
        return []


def is_holiday_today(token):
    events = fetch_holidays(token)
    today_ms = int(now_kolkata().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    for ev in events:
        try:
            if int(ev.get("start", 0)) == today_ms and ev.get("type", "").lower() == "holiday":
                return True, ev
        except Exception:
            continue
    return False, None


# ---------- Scheduling helpers ----------
def get_random_time_in_window(date: datetime.date, start_time: datetime.time, end_time: datetime.time):
    start_dt = datetime.datetime.combine(date, start_time).replace(tzinfo=TZ)
    end_dt = datetime.datetime.combine(date, end_time).replace(tzinfo=TZ)
    total_seconds = int((end_dt - start_dt).total_seconds())
    if total_seconds < 0:
        return start_dt
    offset = random.randint(0, total_seconds)
    return start_dt + datetime.timedelta(seconds=offset)


def sleep_until(target_dt: datetime.datetime):
    while running:
        now = now_kolkata()
        seconds = (target_dt - now).total_seconds()
        if seconds <= 0:
            return
        time.sleep(min(30, max(0.5, seconds)))


def next_event_datetime_and_type(state):
    today = now_kolkata().date()
    date_str = state.get("date")
    morning_done = state.get("morning_done", False) if date_str == today.isoformat() else False
    evening_done = state.get("evening_done", False) if date_str == today.isoformat() else False

    now = now_kolkata()
    morning_end_dt = datetime.datetime.combine(today, MORNING_WINDOW_END).replace(tzinfo=TZ)
    evening_end_dt = datetime.datetime.combine(today, EVENING_WINDOW_END).replace(tzinfo=TZ)

    if not morning_done and now < evening_end_dt:
        target = get_random_time_in_window(today, MORNING_WINDOW_START, MORNING_WINDOW_END)
        if target <= now:
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            return target, "clock_out"
        return target, "clock_in"

    if morning_done and not evening_done:
        if now <= evening_end_dt + datetime.timedelta(hours=6):
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            if target <= now:
                target = now + datetime.timedelta(minutes=2)
            return target, "clock_out"
        tomorrow = today + datetime.timedelta(days=1)
        target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
        return target, "clock_in"

    tomorrow = today + datetime.timedelta(days=1)
    target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
    return target, "clock_in"


def ensure_images_structure():
    if not os.path.isdir(IMAGES_DIR):
        raise FileNotFoundError(f"Images directory '{IMAGES_DIR}' does not exist.")
    # require at least 1..3 folders to exist (you can expand to 1..6)
    for i in range(1, 4):
        p = os.path.join(IMAGES_DIR, str(i))
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)


# ---------- Force punch (validating widget first) ----------
def perform_force_punch(action_type, chat_id=None):
    """Perform an immediate punch ("clock_in" or "clock_out")."""
    try:
        state = load_state()

        # ensure token
        token = state.get("token")
        if not token:
            token, cookies = retry_login_attempts()
            if token:
                state["token"] = token
                state["cookies"] = cookies
                save_state(state)

        # validate widget
        allowed, prev_count, reason = validate_widget_for_action(state.get("token"), action_type)
        if not allowed:
            msg = f"⏳ Cannot perform {action_type}: {reason}"
            log.info(msg)
            if chat_id:
                send_telegram_message(chat_id, msg)
            else:
                send_telegram_message(CHANNEL_ID, msg)
            return False

        # choose image AFTER validation
        if action_type == "clock_in":
            image_path = pick_clock_in_image(state)
            log.info(f"FORCED CLOCK-IN using {image_path}")
        else:
            # ensure we reuse today's last_used_folder if present
            image_path = pick_clock_out_image(state)
            log.info(f"FORCED CLOCK-OUT using {image_path}")

        # send punch using prev_count from server
        r = send_punch_with_retries(state.get("token"), image_path, prev_punch_count=prev_count)
        if r is not None and getattr(r, "status_code", None) == 201:
            # Poll widget for server consistency and update state accordingly
            expected_prev = 1 if action_type == "clock_in" else 0
            expected_clock_in_flag = False if action_type == "clock_in" else True
            widget_after = poll_widget_until_updated(state.get("token"), expect_prev_count=expected_prev,
                                                    expect_clock_in=expected_clock_in_flag,
                                                    timeout=30, interval=3)
            try:
                if widget_after:
                    # set local flags based on action
                    if action_type == "clock_in":
                        state["morning_done"] = True
                    else:
                        state["evening_done"] = True
                    save_state(state)
                    log.info("Forced punch succeeded and widget updated.")
                else:
                    # fallback to punch response's clockWidget
                    resp_json = None
                    try:
                        resp_json = r.json()
                    except Exception:
                        resp_json = None
                    cw = None
                    if isinstance(resp_json, dict):
                        cw = resp_json.get("clockWidget") or resp_json.get("clock_widget") or None
                    if cw:
                        if action_type == "clock_in":
                            state["morning_done"] = True
                        else:
                            state["evening_done"] = True
                        save_state(state)
                        log.info("Forced punch succeeded; updated local state from punch response clockWidget.")
                    else:
                        # conservatively mark done locally
                        if action_type == "clock_in":
                            state["morning_done"] = True
                        else:
                            state["evening_done"] = True
                        save_state(state)
                        log.info("Forced punch succeeded; widget did not update; marked action done locally.")
            except Exception:
                log.exception("Error updating state after forced punch.")

            try:
                send_telegram_photo(CHANNEL_ID, image_path, caption=f"✅ FORCED {action_type.replace('clock_', '').upper()} at {now_kolkata().strftime('%I:%M %p')}")
            except Exception:
                pass
            if chat_id:
                send_telegram_message(chat_id, f"✅ Forced {action_type.replace('clock_', '')} successful.")
            log.info(f"Forced {action_type} successful.")
            return True
        else:
            log.warning(f"Forced {action_type} failed. Final response: {getattr(r,'status_code', 'N/A')}")
            if chat_id:
                send_telegram_message(chat_id, f"❌ Forced {action_type.replace('clock_', '')} failed. See log.")
            return False

    except Exception as e:
        log.exception(f"perform_force_punch exception: {e}")
        if chat_id:
            send_telegram_message(chat_id, f"❌ Error performing forced punch: {e}")
        return False


# ---------- Telegram listener ----------
def telegram_command_listener():
    print(f"[{now_kolkata()}] Telegram listener started.")
    offset = None
    while running:
        try:
            url = TELEGRAM_API_BASE + "/getUpdates"
            params = {"timeout": 20}
            if offset:
                params["offset"] = offset
            r = safe_request("GET", url, params=params, timeout=30)
            if not r:
                log.warning("getUpdates failed: no response")
                time.sleep(5)
                continue
            if not r.ok:
                log.warning(f"getUpdates failed: {r.status_code} {r.text[:200]}")
                time.sleep(5)
                continue
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = msg.get("text", "") or ""
                text = text.strip()
                from_user = msg.get("from", {})
                username = from_user.get("username")
                # Admin enforcement
                if TELEGRAM_ADMIN_CHAT_ID is not None and str(chat_id) != str(TELEGRAM_ADMIN_CHAT_ID):
                    log.info(f"Ignored command from chat {chat_id} (admin required).")
                    continue

                log.info(f"Telegram message from {chat_id} ({username}): {text}")

                changed = False
                st = load_state()

                # handle photo uploads: user sends photo with caption "/addphoto N"
                if "photo" in msg and (text.lower().startswith("/addphoto") or (msg.get("caption") and msg.get("caption").lower().startswith("/addphoto"))):
                    caption_text = msg.get("caption") or text
                    parts = caption_text.split()
                    folder = None
                    if len(parts) >= 2 and parts[1].isdigit():
                        folder = parts[1]
                    if not folder or not (1 <= int(folder) <= 6):
                        send_telegram_message(chat_id, "Please use caption: /addphoto <folder_number> (1..6).")
                    else:
                        photos = msg.get("photo")
                        file_id = photos[-1].get("file_id")
                        dest_folder = os.path.join(IMAGES_DIR, folder)
                        os.makedirs(dest_folder, exist_ok=True)
                        filename = f"telegram_{int(time.time())}_{random.randint(1000,9999)}.jpg"
                        dest_path = os.path.join(dest_folder, filename)
                        ok = download_telegram_file(file_id, dest_path)
                        if ok:
                            send_telegram_message(chat_id, f"Saved photo to {dest_path}")
                        else:
                            send_telegram_message(chat_id, "Failed to download photo. Check bot permissions and network.")
                    continue

                if text.startswith("/skip"):
                    st["skip_next"] = True
                    changed = True
                    send_telegram_message(chat_id, "✅ Next scheduled punch will be skipped.")
                elif text.startswith("/todayskip") or text.startswith("/skiptoday"):
                    st["skip_today"] = True
                    changed = True
                    send_telegram_message(chat_id, "✅ All punches for today will be skipped.")
                elif text.startswith("/pause"):
                    st["paused"] = True
                    changed = True
                    send_telegram_message(chat_id, "⏸️ Automation paused. Use /resume to resume.")
                elif text.startswith("/resume"):
                    st["paused"] = False
                    changed = True
                    send_telegram_message(chat_id, "▶️ Automation resumed.")
                elif text.startswith("/reset"):
                    st["skip_today"] = False
                    st["skip_next"] = False
                    st["paused"] = False
                    changed = True
                    send_telegram_message(chat_id, "▶️ Reset Done.")
                elif text.startswith("/status"):
                    token = st.get("token")
                    status_msg = [
                        f"Date: {st.get('date')}\n",
                        f"Paused: {st.get('paused', False)}\n",
                        f"Skip next: {st.get('skip_next', False)}\n",
                        f"Skip today: {st.get('skip_today', False)}\n",
                        f"Morning done: {st.get('morning_done', False)}\n",
                        f"Evening done: {st.get('evening_done', False)}\n",
                        f"Token present: {bool(token)}\n",
                        f"Last outfit: {st.get('last_used_folder')}\n",
                    ]
                    # append config info
                    status_msg.append("\nConfig:\n")
                    status_msg.append(f"Email: {EMAIL}\n")
                    status_msg.append(f"Timezone: {TZ}\n")
                    status_msg.append(f"Morning window: {MORNING_WINDOW_START} - {MORNING_WINDOW_END}\n")
                    status_msg.append(f"Evening window: {EVENING_WINDOW_START} - {EVENING_WINDOW_END}\n")
                    status_msg.append(f"Channel ID: {CHANNEL_ID}\n")
                    send_telegram_message(chat_id, "".join(status_msg))
                elif text.startswith("/forcein"):
                    send_telegram_message(chat_id, "Performing forced clock-in now...")
                    threading.Thread(target=perform_force_punch, args=("clock_in", chat_id), daemon=True).start()
                elif text.startswith("/forceout"):
                    send_telegram_message(chat_id, "Performing forced clock-out now...")
                    threading.Thread(target=perform_force_punch, args=("clock_out", chat_id), daemon=True).start()
                elif text.startswith("/sendlog"):
                    send_telegram_message(chat_id, "Uploading recent log...")
                    ok = send_telegram_document(chat_id, LOG_FILE, caption="Recent AutoClock log")
                    if not ok:
                        send_telegram_message(chat_id, "Failed to upload log (check file size or bot permissions).")
                # save state if changed
                if changed:
                    save_state(st)

        except Exception as e:
            log.exception(f"Telegram listener exception: {e}")
            time.sleep(5)
    log.info("Telegram listener stopped.")


# ---------- Log rotation: keep last N days ----------
def rotate_log(days=2):
    """
    Truncate LOG_FILE to keep only entries from the last `days` days.
    Expects log lines starting with [YYYY-MM-DD ...
    """
    try:
        if not os.path.exists(LOG_FILE):
            return
        cutoff_date = (now_kolkata() - datetime.timedelta(days=days)).date()
        kept = []
        date_pattern = re.compile(r"^\[(\d{4}-\d{2}-\d{2})")
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                m = date_pattern.match(ln)
                if not m:
                    # keep non-matching lines (be conservative)
                    kept.append(ln)
                    continue
                try:
                    ln_date = datetime.date.fromisoformat(m.group(1))
                    if ln_date >= cutoff_date:
                        kept.append(ln)
                except Exception:
                    kept.append(ln)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(kept)
        log.info(f"Log rotation completed. Kept entries since {cutoff_date.isoformat()}.")
    except Exception as e:
        log.exception(f"Log rotation failed: {e}")


# ---------- Main loop ----------
def graceful_shutdown(signum, frame):
    global running
    log.info(f"Received signal {signum}, shutting down gracefully...")
    running = False


def main_loop():
    global running
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    ensure_images_structure()

    # rotate logs once at startup to keep last 2 days only
    rotate_log(days=2)

    # start telegram listener
    t = threading.Thread(target=telegram_command_listener, daemon=True)
    t.start()

    state = load_state()
    if state.get("date") != now_date_str():
    # store yesterday's last outfit so today's picker avoids it
        yesterday = state.get("last_used_folder")

        state = {
            "date": now_date_str(),
            "morning_done": False,
            "evening_done": False,
            "paused": state.get("paused", False),
            "skip_next": False,
            "skip_today": False,

            # today's outfit not selected yet
            "last_used_folder": None,
            "clock_in_image": None,

            # memory of yesterday's outfit
            "yesterday_last_folder": yesterday
        }
        save_state(state)


    token = state.get("token")
    if not token:
        token, cookies = retry_login_attempts()
        if token:
            state["token"] = token
            state["cookies"] = cookies
            save_state(state)

    log.info(f"AutoClock started (India timezone).")

    while running:
        # rotate logs periodically (once per loop iteration is fine)
        try:
            rotate_log(days=2)
        except Exception:
            pass

        # refresh state
        state = load_state()
        # if date rolled, reset daily flags but keep last_used_folder if present
        if state.get("date") != now_date_str():
            # store yesterday's last outfit so today's picker avoids it
            yesterday = state.get("last_used_folder")

            state = {
                "date": now_date_str(),
                "morning_done": False,
                "evening_done": False,
                "paused": state.get("paused", False),
                "skip_next": False,
                "skip_today": False,

                # today's outfit not selected yet
                "last_used_folder": None,
                "clock_in_image": None,

                # memory of yesterday's outfit
                "yesterday_last_folder": yesterday
            }
            save_state(state)


        # if paused, sleep and continue
        if state.get("paused", False):
            log.info("Automation is paused. Sleeping 30s.")
            time.sleep(30)
            continue

        # Sunday handling
        if now_kolkata().weekday() == 6:  # Sunday == 6
            log.info("Today is Sunday. Skipping scheduling for today.")
            try:
                send_telegram_message(CHANNEL_ID, "☀️ Today is Sunday. AutoClock will skip punches.")
            except Exception:
                log.exception("Failed to send Sunday notification.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            time.sleep(3600)
            continue

        # check company holiday
        token = state.get("token")
        is_hol, ev = is_holiday_today(token)
        if is_hol:
            title = ev.get("title", "Holiday")
            log.info(f"Today is company holiday: {title}. Skipping punches.")
            try:
                send_telegram_message(CHANNEL_ID, f"📅 Today is company holiday: {title}. AutoClock will skip punches.")
            except Exception:
                log.exception("Failed to send holiday notification.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            time.sleep(3600)
            continue

        # decide next event
        target_dt, action_type = next_event_datetime_and_type(state)
        target_dt = target_dt.astimezone(TZ)
        log.info(f"Next event: {action_type} at {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # compute pre-alert time
        pre_alert_dt = target_dt - datetime.timedelta(minutes=PRE_ALERT_MINUTES)

        # If skip_today already set for today, just mark done and continue
        if state.get("skip_today", False):
            log.info("skip_today flag set — skipping today's punches.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue

        # If skip_next is set, skip the next event and clear flag
        if state.get("skip_next", False):
            log.info("skip_next flag set — skipping next scheduled punch.")
            state["skip_next"] = False
            if action_type == "clock_in":
                state["morning_done"] = True
            else:
                state["evening_done"] = True
            save_state(state)
            continue

        now = now_kolkata()

        # If pre-alert time is in the future, wait until pre-alert and then send notification.
        if pre_alert_dt > now:
            log.info(f"Sleeping until pre-alert at {pre_alert_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            send_telegram_message(CHANNEL_ID, f"Sleeping until pre-alert at {pre_alert_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            sleep_until(pre_alert_dt)
            if not running:
                break

            # Reload state (maybe paused/skipped)
            state = load_state()

            # Re-check Sunday/holiday/flags at pre-alert moment
            if state.get("paused", False):
                log.info("Paused at pre-alert time; not sending pre-alert.")
                continue
            if state.get("skip_today", False):
                log.info("skip_today set at pre-alert time; skipping.")
                continue
            if state.get("skip_next", False):
                log.info("skip_next set at pre-alert time; skipping and clearing skip_next.")
                state["skip_next"] = False
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                continue

            # check holiday again using fresh token
            token = state.get("token")
            is_hol, ev = is_holiday_today(token)
            if is_hol:
                try:
                    send_telegram_message(CHANNEL_ID, f"📅 Today became a holiday ({ev.get('title','Holiday')}). Skipping punches.")
                except Exception:
                    log.exception("Failed to send holiday notification at pre-alert time.")
                state["morning_done"] = True
                state["evening_done"] = True
                save_state(state)
                continue

            # send pre-alert
            friendly_time = target_dt.strftime("%I:%M %p").lstrip("0")
            pre_text = f"⚠️ Going to punch {action_type.replace('clock_', '')} at {friendly_time} (in {PRE_ALERT_MINUTES} minutes). Use /skip (skip next) or /skiptoday (skip today)."
            send_telegram_message(CHANNEL_ID, pre_text)
            log.info(f"Pre-alert sent: {pre_text}")

        else:
            # pre-alert time has already passed; possibly schedule soon: if target in future continue to immediate scheduling
            if target_dt <= now:
                log.info(f"Planned target time {target_dt} already passed; recalculating.")
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                continue

        # Sleep until actual target
        log.info(f"Sleeping until actual punch time {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        send_telegram_message(CHANNEL_ID, f"Sleeping until pre-alert at {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        sleep_until(target_dt)
        if not running:
            break

        # Final checks before performing punch
        state = load_state()
        if state.get("paused", False):
            log.info("Paused at punch time; skipping punch.")
            continue
        if state.get("skip_today", False):
            log.info("skip_today set at punch time; skipping punch.")
            continue
        if state.get("skip_next", False):
            log.info("skip_next set at punch time; skipping and clearing skip_next.")
            state["skip_next"] = False
            if action_type == "clock_in":
                state["morning_done"] = True
            else:
                state["evening_done"] = True
            save_state(state)
            continue

        # check Sunday / holiday one more time
        if now_kolkata().weekday() == 6:
            log.info("Sunday at punch time — skipping.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue
        token = state.get("token")
        is_hol, ev = is_holiday_today(token)
        if is_hol:
            log.info(f"Holiday at punch time ({ev.get('title')}) — skipping.")
            send_telegram_message(CHANNEL_ID, f"📅 Today is holiday ({ev.get('title')}). Skipping punch.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue

        # BEFORE selecting images: validate widget to avoid double-punch
        allowed, prev_count, reason = validate_widget_for_action(state.get("token"), action_type)
        if not allowed:
            msg = f"⚠️ Scheduled {action_type} skipped: {reason}"
            log.info(msg)
            send_telegram_message(CHANNEL_ID, msg)
            # don't mark done automatically; leave flags unchanged
            save_state(state)
            continue

        # perform punch: choose image etc (image chosen AFTER validation)
        try:
            if action_type == "clock_in":
                image_path = pick_clock_in_image(state)
                log.info(f"CLOCK-IN using image {image_path}")
            else:
                image_path = pick_clock_out_image(state)
                log.info(f"CLOCK-OUT using image {image_path}")

            # ensure token
            token = state.get("token")
            if not token:
                token, cookies = retry_login_attempts()
                if token:
                    state["token"] = token
                    state["cookies"] = cookies
                    save_state(state)

            r = send_punch_with_retries(token, image_path, prev_punch_count=prev_count)
            if r is None or getattr(r, "status_code", None) != 201:
                log.warning(f"Punch attempt status {getattr(r,'status_code', 'N/A')}. Response: {getattr(r,'text', 'N/A')[:400] if r else 'N/A'}")
                log.info("Punch failed or non-201. Will attempt re-login and retry once.")
                token, cookies = retry_login_attempts()
                if token:
                    state["token"] = token
                    state["cookies"] = cookies
                    save_state(state)
                    r = send_punch_with_retries(token, image_path, prev_punch_count=prev_count)

            if r is None:
                log.error("Final punch attempt raised exception.")
                send_telegram_message(CHANNEL_ID, f"❌ Punch attempt raised exception at {now_kolkata().strftime('%Y-%m-%d %H:%M:%S')}.")
            else:
                if getattr(r, "status_code", None) == 201:
                    # Poll widget for server consistency and update state accordingly
                    expected_prev = 1 if action_type == "clock_in" else 0
                    expected_clock_in_flag = False if action_type == "clock_in" else True
                    widget_after = poll_widget_until_updated(state.get("token"), expect_prev_count=expected_prev,
                                                            expect_clock_in=expected_clock_in_flag,
                                                            timeout=30, interval=3)

                    resp_json = None
                    try:
                        resp_json = r.json()
                    except Exception:
                        resp_json = None

                    if widget_after:
                        # Update state from the widget for local scheduling correctness
                        try:
                            if action_type == "clock_in":
                                state["morning_done"] = True
                            else:
                                state["evening_done"] = True
                            save_state(state)
                        except Exception:
                            log.exception("Failed to update state after widget poll.")
                        log.info(f"Punch SUCCESS and widget updated. Response summary: {json.dumps(resp_json)[:400] if resp_json else getattr(r, 'text', '')[:400]}")
                    else:
                        # widget didn't update in time — fallback to the punch response's clockWidget if available
                        log.warning("Punch succeeded but widget did not reflect update within timeout. Falling back to punch response clockWidget if present.")
                        cw = None
                        if isinstance(resp_json, dict):
                            cw = resp_json.get("clockWidget") or resp_json.get("clock_widget") or None
                        if cw:
                            try:
                                # set local flags conservatively based on what the server reported in the punch response
                                if action_type == "clock_in":
                                    state["morning_done"] = True
                                else:
                                    state["evening_done"] = True
                                save_state(state)
                                log.info("Updated local state from punch response clockWidget.")
                            except Exception:
                                log.exception("Failed to update state from punch response clockWidget.")
                        else:
                            # No clockWidget — still mark local done so next event doesn't move to next day incorrectly
                            if action_type == "clock_in":
                                state["morning_done"] = True
                            else:
                                state["evening_done"] = True
                            save_state(state)
                            log.info("No clockWidget in response — conservatively marking the action done locally.")

                    try:
                        send_telegram_photo(CHANNEL_ID, image_path, caption=f"✅ {action_type.replace('clock_', '').upper()} successful at {now_kolkata().strftime('%I:%M %p')}")
                    except Exception:
                        log.exception("Failed to send success photo.")
                else:
                    log.error(f"Punch FAILED after retry. Status {getattr(r,'status_code', 'N/A')}. Response: {getattr(r,'text','')[:800]}")
                    send_telegram_message(CHANNEL_ID, f"❌ Punch FAILED. Status {getattr(r,'status_code','N/A')}.")
        except Exception as exc:
            log.exception(f"Unexpected error during scheduled action: {exc}")
            try:
                send_telegram_message(CHANNEL_ID, f"❌ Unexpected error during scheduled action: {exc}")
                send_telegram_document(CHANNEL_ID, LOG_FILE, caption="AutoClock error log")
            except Exception:
                pass

        # If both morning and evening done for the day, clear outfit info for next day but keep token
        state = load_state()
        if state.get("morning_done") and state.get("evening_done"):
            token_keep = state.get("token")
            new_state = {"date": now_date_str(), "morning_done": True, "evening_done": True,
                         "paused": state.get("paused", False),
                         "skip_next": False, "skip_today": False}
            if token_keep:
                new_state["token"] = token_keep
            # intentionally clear clock_in_image but keep last_used_folder for reference next day
            if state.get("last_used_folder"):
                new_state["last_used_folder"] = state.get("last_used_folder")
            save_state(new_state)

        time.sleep(2)

    log.info(f"AutoClock stopped.")


if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        try:
            send_telegram_message(CHANNEL_ID, f"🔥 Fatal error in AutoClock: {e}")
            send_telegram_document(CHANNEL_ID, LOG_FILE, caption="AutoClock fatal log")
        except Exception:
            pass
        sys.exit(1)
