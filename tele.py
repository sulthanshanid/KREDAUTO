#!/usr/bin/env python3
"""
AutoClock (India timezone) — Extended with Telegram controls

Features:
- Asia/Kolkata timezone
- Random clock-in between MORNING_WINDOW_START .. MORNING_WINDOW_END
- Random clock-out between EVENING_WINDOW_START .. EVENING_WINDOW_END
- 6 outfit folders inside IMAGES_DIR named "1".."6"
- Pre-alert to Telegram 10 minutes before scheduled punch
- Telegram commands: /skip, /skiptoday, /pause, /resume, /status
- Skip Sundays and company holidays (fetched from Kredily GET /ws/v2/company/get-event-by-month/?from=...)
- Persistent state in STATE_FILE (including pause and skip flags)
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
from zoneinfo import ZoneInfo
import requests
'''
proxy = 'http://127.0.0.1:8080'
os.environ['http_proxy'] = proxy
os.environ['HTTP_PROXY'] = proxy
os.environ['https_proxy'] = proxy
os.environ['HTTPS_PROXY'] = proxy
os.environ['REQUESTS_CA_BUNDLE'] = "C:\\Users\\User\\Desktop\\cacert.pem"
'''
# -------- CONFIG --------
EMAIL = "muzu04994@gmail.com"
PASSWORD = "Shanid@123"

IMAGES_DIR = "images"               # contains subfolders "1", "2", ... "6"
STATE_FILE = "state/session_state.json"   # persists token, last_outfit, clock_in_image, date, flags
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

LOGIN_URL = "https://app.kredily.com/ws/v1/accounts/api-token-auth/"
PUNCH_URL = "https://app.kredily.com/ws/v1/attendance-log/punch/"
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
BOT_TOKEN = "8025422826:AAEezLn7fN_6cisTZmvmAuMlQRwmnB3xKgw"
CHANNEL_ID = "@kreduto"  # where photos/notifications go
# For command handling restrict to a numeric chat ID (e.g., your Telegram user id).
# If None, commands accepted from any chat (not recommended long-term).
TELEGRAM_ADMIN_CHAT_ID = None

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ------------------------

running = True
session = requests.Session()
state_lock = threading.Lock()


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
        print(f"[{now_kolkata()}] Login succeeded, token length={len(token)} cookies={list(cookies_dict.keys())}")
    else:
        print(f"[{now_kolkata()}] Login response didn't include token JSON. Status {r.status_code}. Cookies: {list(cookies_dict.keys())}")
        send_telegram_message("@abbpdfs", "Login iS Failing")
        
    return token, cookies_dict


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
        "device_model_id": "A95",
        "device_name": "OPPO",
        "os_version": "android 13",
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


def send_telegram_message(chat_id, text):
    try:
        url = TELEGRAM_API_BASE + "/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return True
        else:
            print(f"[{now_kolkata()}] Telegram sendMessage failed: {r.status_code} {r.text}")
            return False
    except Exception as e:
        print(f"[{now_kolkata()}] Telegram sendMessage exception: {e}")
        return False


def send_telegram_photo(chat_id, image_path, caption=None):
    try:
        url = TELEGRAM_API_BASE + "/sendPhoto"
        with open(image_path, "rb") as image_file:
            files = {"photo": image_file}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            r = requests.post(url, data=data, files=files, timeout=30)
        if r.ok:
            return True
        else:
            print(f"[{now_kolkata()}] sendPhoto failed: {r.status_code} {r.text}")
            return False
    except Exception as e:
        print(f"[{now_kolkata()}] sendPhoto exception: {e}")
        return False


def choose_random_image_from_folder(folder_path, exclude_filename=None):
    files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    if not files:
        raise FileNotFoundError(f"No files in folder {folder_path}")
    if exclude_filename and exclude_filename in files and len(files) > 1:
        files = [f for f in files if f != exclude_filename]
    if not files:
        raise FileNotFoundError(f"No available images in folder after excluding {exclude_filename}")
    return os.path.join(folder_path, random.choice(files))


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


def month_start_epoch_ms(dt: datetime.datetime):
    mstart = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(mstart.timestamp() * 1000)


def fetch_holidays(token):
    """Fetch events for current month. Return list of events (dicts) or empty list."""
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    params = {"from": str(month_start_epoch_ms(now_kolkata()))}
    try:
        r = session.get(HOLIDAY_API, headers=headers, params=params, timeout=20)
        if not r.ok:
            print(f"[{now_kolkata()}] Holiday fetch non-OK {r.status_code}: {r.text[:200]}")
            return []
        js = r.json()
        return js.get("result", []) if isinstance(js, dict) else []
    except Exception as e:
        print(f"[{now_kolkata()}] fetch_holidays exception: {e}")
        return []


def is_holiday_today(token):
    """Return (bool, event) where event is the matching dict or None."""
    events = fetch_holidays(token)
    today_ms = int(now_kolkata().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    for ev in events:
        try:
            if int(ev.get("start", 0)) == today_ms and ev.get("type", "").lower() == "holiday":
                return True, ev
        except Exception:
            continue
    return False, None


def next_event_datetime_and_type(state):
    """
    Decide next event datetime and whether it's a 'clock_in' or 'clock_out'.
    Uses state to know whether today's morning/evening punch was already done.
    If evening punch was missed (e.g., restart after 6 PM), it still schedules
    a clock_out before moving to next day's clock_in.
    """
    today = now_kolkata().date()
    date_str = state.get("date")
    morning_done = state.get("morning_done", False) if date_str == today.isoformat() else False
    evening_done = state.get("evening_done", False) if date_str == today.isoformat() else False

    now = now_kolkata()
    morning_end_dt = datetime.datetime.combine(today, MORNING_WINDOW_END).replace(tzinfo=TZ)
    evening_end_dt = datetime.datetime.combine(today, EVENING_WINDOW_END).replace(tzinfo=TZ)

    # Case 1: morning not done yet → schedule morning clock-in
    if not morning_done and now < evening_end_dt:
        target = get_random_time_in_window(today, MORNING_WINDOW_START, MORNING_WINDOW_END)
        if target <= now:
            # morning window passed → go for evening
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            return target, "clock_out"
        return target, "clock_in"

    # Case 2: morning done but evening not yet done
    if morning_done and not evening_done:
        # If still within or slightly after evening window, try to punch out
        if now <= evening_end_dt + datetime.timedelta(hours=6):
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            # If window already passed, punch out within next few minutes
            if target <= now:
                target = now + datetime.timedelta(minutes=2)
            return target, "clock_out"
        # Otherwise (too late), move to next morning
        tomorrow = today + datetime.timedelta(days=1)
        target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
        return target, "clock_in"

    # Case 3: both done → plan for next morning
    tomorrow = today + datetime.timedelta(days=1)
    target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
    return target, "clock_in"



def ensure_images_structure():
    if not os.path.isdir(IMAGES_DIR):
        raise FileNotFoundError(f"Images directory '{IMAGES_DIR}' does not exist.")
    for i in range(1, 7):
        p = os.path.join(IMAGES_DIR, str(i))
        if not os.path.isdir(p):
            raise FileNotFoundError(f"Expected outfit folder missing: {p}")


def graceful_shutdown(signum, frame):
    global running
    print(f"\n[{now_kolkata()}] Received signal {signum}, shutting down gracefully...")
    running = False


def telegram_command_listener():
    """
    Poll Telegram getUpdates and update shared state flags:
    /skip -> set skip_next True
    /skiptoday -> set skip_today True
    /pause -> set paused True
    /resume -> set paused False
    /status -> send current state
    """
    print(f"[{now_kolkata()}] Telegram listener started.")
    offset = None
    while running:
        try:
            url = TELEGRAM_API_BASE + "/getUpdates"
            params = {"timeout": 20}
            if offset:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=30)
            if not r.ok:
                print(f"[{now_kolkata()}] getUpdates failed: {r.status_code} {r.text[:200]}")
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
                text = msg.get("text", "").strip()
                from_user = msg.get("from", {})
                username = from_user.get("username")
                # Enforce admin if provided
                if TELEGRAM_ADMIN_CHAT_ID is not None and str(chat_id) != str(TELEGRAM_ADMIN_CHAT_ID):
                    print(f"[{now_kolkata()}] Ignored command from chat {chat_id} (admin required).")
                    continue

                print(f"[{now_kolkata()}] Telegram message from {chat_id} ({username}): {text}")

                changed = False
                st = load_state()
                if text.startswith("/skip"):
                    st["skip_next"] = True
                    changed = True
                    send_telegram_message(chat_id, "✅ Next scheduled punch will be skipped.")
                elif text.startswith("/todayskip"):
                    st["skip_today"] = True
                    changed = True
                    send_telegram_message(chat_id, "✅ All punches for today will be skipped.")
                elif text.startswith("/pause"):
                    st["paused"] = True
                    changed = True
                    send_telegram_message(chat_id, "⏸️ Automation paused. Use /resume to resume.")
                elif text.startswith("/resume"):
                    previous = st.get("paused", False)
                    st["paused"] = False
                    changed = True
                    send_telegram_message(chat_id, "▶️ Automation resumed.")
                elif text.startswith("/reset"):
                    previous = st.get("paused", False)
                    st["skip_today"] = False
                    st["skip_next"] = False
                    st["paused"] = False
                    changed = True
                    send_telegram_message(chat_id, "▶️ Reset Done.")
                elif text.startswith("/status"):
                    token = st.get("token")
                    status_msg = [
                        f"Date: {st.get('date')}",
                        f"Paused: {st.get('paused', False)}",
                        f"Skip next: {st.get('skip_next', False)}",
                        f"Skip today: {st.get('skip_today', False)}",
                        f"Morning done: {st.get('morning_done', False)}",
                        f"Evening done: {st.get('evening_done', False)}",
                        f"Token present: {bool(token)}"
                    ]
                    send_telegram_message(chat_id, "\n".join(status_msg))
                # other commands can be added here

                if changed:
                    save_state(st)

        except Exception as e:
            print(f"[{now_kolkata()}] Telegram listener exception: {e}")
            time.sleep(5)
    print(f"[{now_kolkata()}] Telegram listener stopped.")


def main_loop():
    global running
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    ensure_images_structure()

    # start telegram listener
    t = threading.Thread(target=telegram_command_listener, daemon=True)
    t.start()

    state = load_state()
    if state.get("date") != now_date_str():
        state = {"date": now_date_str(), "morning_done": False, "evening_done": False,
                 "paused": False, "skip_next": False, "skip_today": False}
        save_state(state)

    token = state.get("token")
    if not token:
        token, cookies = login_get_token_and_cookies()
        if token:
            state["token"] = token
            state["cookies"] = cookies
            save_state(state)

    print(f"[{now_kolkata()}] AutoClock started (India timezone).")

    while running:
        # refresh state
        state = load_state()
        # if date rolled, reset daily flags
        if state.get("date") != now_date_str():
            state["date"] = now_date_str()
            state["morning_done"] = False
            state["evening_done"] = False
            state["skip_today"] = False
            state.pop("last_outfit", None)
            state.pop("clock_in_image", None)
            save_state(state)

        # if paused, sleep and continue
        if state.get("paused", False):
            print(f"[{now_kolkata()}] Automation is paused. Sleeping 30s.")
            time.sleep(30)
            continue

        # skip Sundays entirely
        if now_kolkata().weekday() == 6:  # Sunday == 6
            print(f"[{now_kolkata()}] Today is Sunday. Skipping scheduling for today.")
            # set both done to true for today to not schedule anything
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            time.sleep(3600)  # wake hourly to check
            continue

        # check company holiday
        token = state.get("token")
        is_hol, ev = is_holiday_today(token)
        if is_hol:
            title = ev.get("title", "Holiday")
            print(f"[{now_kolkata()}] Today is company holiday: {title}. Skipping punches.")
            # notify
            send_telegram_message(CHANNEL_ID, f"📅 Today is company holiday: {title}. AutoClock will skip punches.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            time.sleep(3600)
            continue

        # decide next event
        target_dt, action_type = next_event_datetime_and_type(state)
        target_dt = target_dt.astimezone(TZ)
        print(f"[{now_kolkata()}] Next event: {action_type} at {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # compute pre-alert time
        pre_alert_dt = target_dt - datetime.timedelta(minutes=PRE_ALERT_MINUTES)

        # If skip_today already set for today, just mark done and continue
        if state.get("skip_today", False):
            print(f"[{now_kolkata()}] skip_today flag set — skipping today's punches.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue

        # If skip_next is set, skip the next event and clear flag
        if state.get("skip_next", False):
            print(f"[{now_kolkata()}] skip_next flag set — skipping next scheduled punch.")
            # clear skip_next
            state["skip_next"] = False
            # mark the appropriate done flag so scheduling moves on
            if action_type == "clock_in":
                state["morning_done"] = True
            else:
                state["evening_done"] = True
            save_state(state)
            continue

        now = now_kolkata()

        # If pre-alert time is in the future, wait until pre-alert and then send notification.
        if pre_alert_dt > now:
            print(f"[{now_kolkata()}] Sleeping until pre-alert at {pre_alert_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            sleep_until(pre_alert_dt)
            if not running:
                break

            # Reload state (maybe paused/skipped)
            state = load_state()

            # Re-check Sunday/holiday/flags at pre-alert moment
            if state.get("paused", False):
                print(f"[{now_kolkata()}] Paused at pre-alert time; not sending pre-alert.")
                continue
            if state.get("skip_today", False):
                print(f"[{now_kolkata()}] skip_today set at pre-alert time; skipping.")
                continue
            if state.get("skip_next", False):
                print(f"[{now_kolkata()}] skip_next set at pre-alert time; skipping and clearing skip_next.")
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
                send_telegram_message(CHANNEL_ID, f"📅 Today became a holiday ({ev.get('title','Holiday')}). Skipping punches.")
                state["morning_done"] = True
                state["evening_done"] = True
                save_state(state)
                continue

            # send pre-alert
            friendly_time = target_dt.strftime("%I:%M %p").lstrip("0")
            pre_text = f"⚠️ Going to punch {action_type.replace('clock_', '')} at {friendly_time} (in {PRE_ALERT_MINUTES} minutes). Use /skip (skip next) or /skiptoday (skip today)."
            send_telegram_message(CHANNEL_ID, pre_text)
            print(f"[{now_kolkata()}] Pre-alert sent: {pre_text}")

        else:
            # pre-alert time has already passed; possibly schedule soon: if target in future continue to immediate scheduling
            if target_dt <= now:
                # target already passed: skip to next loop
                print(f"[{now_kolkata()}] Planned target time {target_dt} already passed; recalculating.")
                # mark appropriate done to move forward, to avoid tight loop
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                continue

        # Sleep until actual target
        print(f"[{now_kolkata()}] Sleeping until actual punch time {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        sleep_until(target_dt)
        if not running:
            break

        # Final checks before performing punch
        state = load_state()
        if state.get("paused", False):
            print(f"[{now_kolkata()}] Paused at punch time; skipping punch.")
            continue
        if state.get("skip_today", False):
            print(f"[{now_kolkata()}] skip_today set at punch time; skipping punch.")
            continue
        if state.get("skip_next", False):
            print(f"[{now_kolkata()}] skip_next set at punch time; skipping and clearing skip_next.")
            state["skip_next"] = False
            if action_type == "clock_in":
                state["morning_done"] = True
            else:
                state["evening_done"] = True
            save_state(state)
            continue

        # check Sunday / holiday one more time
        if now_kolkata().weekday() == 6:
            print(f"[{now_kolkata()}] Sunday at punch time — skipping.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue
        token = state.get("token")
        is_hol, ev = is_holiday_today(token)
        if is_hol:
            print(f"[{now_kolkata()}] Holiday at punch time ({ev.get('title')}) — skipping.")
            send_telegram_message(CHANNEL_ID, f"📅 Today is holiday ({ev.get('title')}). Skipping punch.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue

        # perform punch: choose image etc
        try:
            if action_type == "clock_in":
                outfit_folder = str(random.randint(1, 6))
                folder_path = os.path.join(IMAGES_DIR, outfit_folder)
                image_path = choose_random_image_from_folder(folder_path)
                state["last_outfit"] = outfit_folder
                state["clock_in_image"] = os.path.basename(image_path)
                state["morning_done"] = True
                state["date"] = now_date_str()
                save_state(state)
                print(f"[{now_kolkata()}] CLOCK-IN using folder {outfit_folder}, image {os.path.basename(image_path)}")
            else:
                outfit_folder = state.get("last_outfit")
                if not outfit_folder:
                    outfit_folder = str(random.randint(1, 6))
                    print(f"[{now_kolkata()}] WARNING: no last_outfit in state; choosing random folder {outfit_folder}")
                folder_path = os.path.join(IMAGES_DIR, outfit_folder)
                exclude = state.get("clock_in_image")
                image_path = choose_random_image_from_folder(folder_path, exclude_filename=exclude)
                state["evening_done"] = True
                state["date"] = now_date_str()
                save_state(state)
                print(f"[{now_kolkata()}] CLOCK-OUT using same folder {outfit_folder}, image {os.path.basename(image_path)}")

            # ensure token
            token = state.get("token")
            if not token:
                token, cookies = login_get_token_and_cookies()
                if token:
                    state["token"] = token
                    state["cookies"] = cookies
                    save_state(state)

            r = send_punch(token, image_path)
            if r is None or r.status_code != 201:
                print(f"[{now_kolkata()}] Punch attempt status {r.status_code if r else 'N/A'}. Response: {r.text[:400] if r else 'N/A'}")
                print(f"[{now_kolkata()}] Punch failed or non-201. Will attempt re-login and retry once.")
                token, cookies = login_get_token_and_cookies()
                if token:
                    state["token"] = token
                    state["cookies"] = cookies
                    save_state(state)
                    r = send_punch(token, image_path)

            if r is None:
                print(f"[{now_kolkata()}] Final punch attempt raised exception.")
                send_telegram_message(CHANNEL_ID, f"❌ Punch attempt raised exception at {now_kolkata().strftime('%Y-%m-%d %H:%M:%S')}.")
            else:
                if r.status_code == 201:
                    try:
                        resp = r.json()
                        print(f"[{now_kolkata()}] Punch SUCCESS. Response summary: {json.dumps(resp)[:400]}")
                        send_telegram_photo(CHANNEL_ID, image_path, caption=f"✅ {action_type.replace('clock_', '').upper()} successful at {now_kolkata().strftime('%I:%M %p')}")
                    except Exception:
                        print(f"[{now_kolkata()}] Punch SUCCESS. Raw body truncated: {r.text[:400]}")
                        send_telegram_message(CHANNEL_ID, f"✅ {action_type.replace('clock_', '').upper()} successful (response not JSON).")
                else:
                    print(f"[{now_kolkata()}] Punch FAILED after retry. Status {r.status_code}. Response: {r.text[:800]}")
                    send_telegram_message(CHANNEL_ID, f"❌ Punch FAILED. Status {r.status_code}.")
        except Exception as exc:
            print(f"[{now_kolkata()}] Unexpected error during scheduled action: {exc}")
            send_telegram_message(CHANNEL_ID, f"❌ Unexpected error during scheduled action: {exc}")

        # If both morning and evening done for the day, clear outfit info for next day but keep token
        state = load_state()
        if state.get("morning_done") and state.get("evening_done"):
            token_keep = state.get("token")
            new_state = {"date": now_date_str(), "morning_done": True, "evening_done": True,
                         "paused": state.get("paused", False),
                         "skip_next": False, "skip_today": False}
            if token_keep:
                new_state["token"] = token_keep
            save_state(new_state)

        time.sleep(2)

    print(f"[{now_kolkata()}] AutoClock stopped.")


if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        print(f"[{now_kolkata()}] Fatal error: {e}")
        sys.exit(1)
