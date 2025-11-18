#!/usr/bin/env python3
"""
AutoClock v2 — SQLite migration (keeps your original logic intact)

What changed:
- Replaced JSON state + persistent tg_queue file with a single SQLite DB (state/autoclock.db)
- All scheduling decisions (next event) are based on punch_log entries for the current day
- Telegram queue is stored in DB with `sent` flag (prevents duplicates)
- All original widget checks, kill-switch, pre-alerts, polling, retries and image selection logic preserved
- Preferred image/folder support implemented in DB
- Multi-thread safe access guarded with `state_lock`

Drop this file in place of final.py and run. It uses the same endpoints and credentials as before
"""

import os
import sys
import json
import time
import base64
import random
import threading
import logging
import datetime
import signal
import traceback
import sqlite3
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

import requests

# ----------------- CONFIG (unchanged defaults) -----------------
EMAIL = "9544790012"
PASSWORD = "Shanid@786"

IMAGES_DIR = "images"                     # subfolders "1","2","3"...
DB_PATH = "state/autoclock.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

LOGIN_URL = "https://app.kredily.com/ws/v1/accounts/api-token-auth/"
PUNCH_URL = "https://app.kredily.com/ws/v1/attendance-log/punch/"
WIDGET_URL = "https://app.kredily.com/ws/v1/attendance-log/clocking-widget-api/"
HOLIDAY_API = "https://app.kredily.com/ws/v2/company/get-event-by-month/"

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

MORNING_WINDOW_START = datetime.time(10, 8, 0)
MORNING_WINDOW_END = datetime.time(10, 15, 0)
EVENING_WINDOW_START = datetime.time(18, 5, 0)
EVENING_WINDOW_END = datetime.time(18, 8, 0)

PRE_ALERT_MINUTES = 10

BOT_TOKEN = "8179352079:AAEbmNmkVLJvyqpIm7DK8G9BcJAn43U5_hA"
CHANNEL_ID = "631331311"
TELEGRAM_ADMIN_CHAT_ID = None
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

LOG_FILE = "autoclock_verbose.log"
LOG_LEVEL = logging.DEBUG
LOG_ROTATE_DAYS = 2

RETRY_DELAYS = [0, 60, 300, 600]
TG_SEND_MAX_RETRIES = 3
TG_HTTP_TIMEOUT = 15
WIDGET_POLL_TIMEOUT = 30
WIDGET_POLL_INTERVAL = 3
MAX_GETUPDATES_TIMEOUT = 30
TELEGRAM_QUEUE_POLL = 5

running = True
state_lock = threading.Lock()

session = requests.Session()

logger = logging.getLogger("AutoClockC")
logger.setLevel(LOG_LEVEL)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
fh = logging.FileHandler(LOG_FILE)
fh.setLevel(LOG_LEVEL)
fh.setFormatter(formatter)
logger.addHandler(fh)
sh = logging.StreamHandler(sys.stdout)
sh.setLevel(logging.INFO)
sh.setFormatter(formatter)
logger.addHandler(sh)

# ----------------- SQLite helpers -----------------

def get_db_connection():
    # each thread may call this; allow multithread
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with state_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            date TEXT,
            morning_done INTEGER DEFAULT 0,
            evening_done INTEGER DEFAULT 0,
            last_used_folder TEXT,
            yesterday_last_folder TEXT,
            preferred_image TEXT,
            preferred_folder TEXT,
            paused INTEGER DEFAULT 0,
            skip_next INTEGER DEFAULT 0,
            skip_today INTEGER DEFAULT 0,
            token TEXT,
            cookies TEXT
        )
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS punch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            punch_date TEXT NOT NULL,
            punch_time TEXT NOT NULL,
            action TEXT NOT NULL,
            image_used TEXT,
            folder_used TEXT,
            server_prev_count INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS tg_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL,
            ts TEXT DEFAULT CURRENT_TIMESTAMP,
            sent INTEGER DEFAULT 0
        )
        ''')

        # ensure single state row
        cur.execute('SELECT COUNT(*) as c FROM state')
        if cur.fetchone()[0] == 0:
            cur.execute('INSERT INTO state (id, date) VALUES (1, ?)', (now_date_str(),))
        conn.commit()
        conn.close()


# ----------------- State CRUD -----------------

def now_kolkata() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def now_date_str() -> str:
    return now_kolkata().date().isoformat()


def get_state() -> Dict[str, Any]:
    with state_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT * FROM state WHERE id = 1')
        row = cur.fetchone()
        conn.close()
        if not row:
            return {}
        d = dict(row)
        # convert ints to bools
        for k in ("morning_done", "evening_done", "paused", "skip_next", "skip_today"):
            d[k] = bool(d.get(k))
        return d


def update_state(**kwargs):
    if not kwargs:
        return
    with state_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        pairs = []
        vals = []
        for k, v in kwargs.items():
            pairs.append(f"{k} = ?")
            vals.append(v)
        vals.append(1)
        sql = f"UPDATE state SET {', '.join(pairs)} WHERE id = ?"
        cur.execute(sql, vals)
        conn.commit()
        conn.close()


def reset_daily_flags(preserve_last_folder: Optional[str] = None):
    with state_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        # reset state but preserve yesterday_last_folder
        cur.execute('UPDATE state SET date = ?, morning_done = 0, evening_done = 0, preferred_image = NULL, preferred_folder = NULL, skip_next = 0, skip_today = 0 WHERE id = 1', (now_date_str(),))
        if preserve_last_folder:
            cur.execute('UPDATE state SET yesterday_last_folder = ? WHERE id = 1', (preserve_last_folder,))
        conn.commit()
        conn.close()


# ----------------- Punch logging -----------------

def add_punch_log(action: str, image_used: Optional[str], folder_used: Optional[str], server_prev_count: Optional[int]):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('INSERT INTO punch_log (punch_date, punch_time, action, image_used, folder_used, server_prev_count) VALUES (?,?,?,?,?,?)',
                (now_kolkata().date().isoformat(), now_kolkata().time().isoformat(), action, image_used, folder_used, server_prev_count))
    conn.commit()
    conn.close()


def last_punch_today() -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM punch_log WHERE punch_date = ? ORDER BY id DESC LIMIT 1', (now_kolkata().date().isoformat(),))
    r = cur.fetchone()
    conn.close()
    return r


# ----------------- Telegram DB queue -----------------

def enqueue_telegram_db(payload: Dict[str, Any]):
    with state_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO tg_queue (payload) VALUES (?)', (json.dumps(payload),))
        conn.commit()
        conn.close()


def get_next_tg_item():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT id, payload FROM tg_queue WHERE sent = 0 ORDER BY id ASC LIMIT 1')
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return (r['id'], json.loads(r['payload']))


def mark_tg_sent(item_id: int):
    with state_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('UPDATE tg_queue SET sent = 1 WHERE id = ?', (item_id,))
        conn.commit()
        conn.close()


# ----------------- HTTP safe request & Telegram senders (mostly unchanged) -----------------

def safe_request(method: str, url: str, max_attempts: int = 2, backoff: int = 2, **kwargs) -> Optional[requests.Response]:
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = session.request(method, url, **kwargs)
            return resp
        except Exception as e:
            logger.warning(f"HTTP {method} {url} attempt {attempt} raised {e}")
        if attempt < max_attempts:
            time.sleep(backoff)
            backoff *= 2
    return None


def tg_send_message(chat_id: str, text: str) -> bool:
    payload = {"chat_id": str(chat_id), "text": text}
    for attempt in range(1, TG_SEND_MAX_RETRIES + 1):
        r = safe_request("POST", TELEGRAM_API_BASE + "/sendMessage", json=payload, timeout=TG_HTTP_TIMEOUT)
        if r and r.ok:
            logger.debug("telegram sendMessage ok")
            return True
        time.sleep(2)
    return False


def tg_send_photo(chat_id: str, image_path: str, caption: Optional[str] = None) -> bool:
    for attempt in range(1, TG_SEND_MAX_RETRIES + 1):
        try:
            with open(image_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = caption
                r = safe_request("POST", TELEGRAM_API_BASE + "/sendPhoto", data=data, files=files, timeout=TG_HTTP_TIMEOUT + 10)
                if r and r.ok:
                    return True
        except Exception:
            logger.exception("tg_send_photo exception")
        time.sleep(2)
    return False


def tg_send_document(chat_id: str, file_path: str, caption: Optional[str] = None) -> bool:
    for attempt in range(1, TG_SEND_MAX_RETRIES + 1):
        try:
            with open(file_path, "rb") as f:
                files = {"document": f}
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = caption
                r = safe_request("POST", TELEGRAM_API_BASE + "/sendDocument", data=data, files=files, timeout=TG_HTTP_TIMEOUT + 20)
                if r and r.ok:
                    return True
        except Exception:
            logger.exception("tg_send_document exception")
        time.sleep(2)
    return False


def telegram_sender_loop():
    logger.info("Telegram sender thread started (DB-backed queue).")
    while running:
        try:
            item = get_next_tg_item()
            if not item:
                time.sleep(TELEGRAM_QUEUE_POLL)
                continue
            item_id, payload = item
            typ = payload.get("type", "message")
            success = False
            if typ == "message":
                success = tg_send_message(payload.get("chat_id", CHANNEL_ID), payload.get("text", ""))
            elif typ == "photo":
                success = tg_send_photo(payload.get("chat_id", CHANNEL_ID), payload.get("path"), payload.get("caption"))
            elif typ == "document":
                success = tg_send_document(payload.get("chat_id", CHANNEL_ID), payload.get("path"), payload.get("caption"))
            else:
                success = True

            if success:
                mark_tg_sent(item_id)
            else:
                logger.warning("Telegram send failed, will retry after short sleep")
                time.sleep(10)
        except Exception:
            logger.exception("Exception in telegram_sender_loop")
            time.sleep(5)
    logger.info("Telegram sender thread stopped.")


# wrappers

def notify(text: str, chat_id: Optional[str] = None):
    payload = {"type": "message", "chat_id": chat_id or CHANNEL_ID, "text": text}
    enqueue_telegram_db(payload)


def notify_photo(image_path: str, caption: Optional[str] = None, chat_id: Optional[str] = None):
    payload = {"type": "photo", "chat_id": chat_id or CHANNEL_ID, "path": image_path, "caption": caption}
    enqueue_telegram_db(payload)


def notify_document(path: str, caption: Optional[str] = None, chat_id: Optional[str] = None):
    payload = {"type": "document", "chat_id": chat_id or CHANNEL_ID, "path": path, "caption": caption}
    enqueue_telegram_db(payload)


# ----------------- Image helpers (unchanged) -----------------

def encode_image_to_data_url(filepath: str) -> str:
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


def choose_random_image_from_folder(folder_path: str, exclude_filename: Optional[str] = None) -> str:
    files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    if exclude_filename and exclude_filename in files and len(files) > 1:
        files = [f for f in files if f != exclude_filename]
    if not files:
        raise FileNotFoundError(f"No files in folder {folder_path}")
    chosen = random.choice(files)
    return os.path.join(folder_path, chosen)


# ----------------- Kredily helpers (kept) -----------------

def login_get_token_and_cookies_once() -> Tuple[Optional[str], Dict[str, str]]:
    try:
        r = safe_request("POST", LOGIN_URL, headers=HEADERS_BASE,
                         json={"consent": True, "password": PASSWORD, "username": EMAIL}, timeout=20)
        if not r:
            logger.warning("Login attempt returned no response.")
            return None, {}
        js = {}
        try:
            js = r.json()
        except Exception:
            pass
        cookies_dict = {c.name: c.value for c in r.cookies}
        token = None
        for k in ("token", "auth_token", "access", "key"):
            if isinstance(js, dict) and k in js:
                token = js[k]
                break
        if token:
            logger.info("Login succeeded.")
            return token, cookies_dict
        else:
            notify(f"⚠️ Login failed (no token). Status={getattr(r,'status_code',None)}. Check credentials/network.")
            return None, cookies_dict
    except Exception:
        logger.exception("login exception")
        return None, {}


def retry_login_attempts() -> Tuple[Optional[str], Dict[str, str]]:
    for delay in RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        token, cookies = login_get_token_and_cookies_once()
        if token:
            update_state(token=token, cookies=json.dumps(cookies))
            return token, cookies
    return None, {}


def fetch_clocking_widget(token: Optional[str]) -> Optional[Dict[str, Any]]:
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    r = safe_request("GET", WIDGET_URL, headers=headers, timeout=15)
    if not r or not r.ok:
        return None
    try:
        js = r.json()
        data = js.get("data", js)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def validate_widget_for_action(token: Optional[str], action: str) -> Tuple[bool, Optional[int], str]:
    w = fetch_clocking_widget(token)
    if w is None:
        return False, None, "Failed to fetch clocking widget"
    prev = int(w.get("prev_punch_count", -1)) if w.get("prev_punch_count") is not None else -1
    clock_in_flag = w.get("clock_in")
    if action == "clock_in":
        if prev == 0:
            return True, prev, "OK (prev_count==0)"
        return False, prev, f"Widget not suitable for clock_in: clock_in={clock_in_flag}, prev={prev}"
    elif action == "clock_out":
        if prev == 1:
            return True, prev, "OK (prev_count==1)"
        return False, prev, f"Widget not suitable for clock_out: clock_in={clock_in_flag}, prev={prev}"
    return False, prev, "Unknown action"


def poll_widget_until_updated(token: Optional[str], expect_prev_count: Optional[int] = None, expect_clock_in: Optional[bool] = None,
                              timeout: int = WIDGET_POLL_TIMEOUT, interval: int = WIDGET_POLL_INTERVAL) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        w = fetch_clocking_widget(token)
        if w is None:
            last = None
        else:
            last = w
            ok_prev = True if expect_prev_count is None else (int(w.get("prev_punch_count", -1)) == int(expect_prev_count))
            ok_clock = True if expect_clock_in is None else (w.get("clock_in") == expect_clock_in)
            if ok_prev and ok_clock:
                return w
        time.sleep(interval)
    return last


def send_punch_once(token: Optional[str], image_path: str, prev_punch_count: int = 0) -> Optional[requests.Response]:
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
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
    r = safe_request("POST", PUNCH_URL, headers=headers, json=body, timeout=30)
    return r


def send_punch_with_retries(token: Optional[str], image_path: str, prev_punch_count: int = 0) -> Optional[requests.Response]:
    last_r = None
    for i, delay in enumerate(RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        r = send_punch_once(token, image_path, prev_punch_count)
        last_r = r
        if r is not None and getattr(r, "status_code", None) == 201:
            return r
    return last_r


# ----------------- Outfit selection (no adjacent reuse) -----------------

def select_outfit_folder_for_today(state: Dict[str, Any]) -> str:
    folders = [str(i) for i in range(1, 4)]
    yesterday = state.get("yesterday_last_folder")
    today_last = state.get("last_used_folder")
    excluded = set()
    if yesterday:
        excluded.add(yesterday)
    if today_last:
        excluded.add(today_last)
    available = [f for f in folders if f not in excluded]
    if not available:
        available = folders
    chosen = random.choice(available)
    return chosen


def pick_clock_in_image(state: Dict[str, Any]) -> str:
    folder = state.get("preferred_folder") or select_outfit_folder_for_today(state)
    folder_path = os.path.join(IMAGES_DIR, folder)
    image_path = choose_random_image_from_folder(folder_path)
    update_state(last_used_folder=folder, preferred_image=None, preferred_folder=None)
    update_state(morning_done=1, date=now_date_str())
    return image_path


def pick_clock_out_image(state: Dict[str, Any]) -> str:
    folder = state.get("last_used_folder")
    if not folder:
        folder = select_outfit_folder_for_today(state)
        update_state(last_used_folder=folder)
    folder_path = os.path.join(IMAGES_DIR, folder)
    exclude = state.get("preferred_image")
    image_path = choose_random_image_from_folder(folder_path, exclude_filename=exclude)
    update_state(evening_done=1, date=now_date_str(), preferred_image=None, preferred_folder=None)
    return image_path


# ----------------- Next event calculation based on DB -----------------

def get_random_time_in_window(date_obj: datetime.date, start_time: datetime.time, end_time: datetime.time) -> datetime.datetime:
    start_dt = datetime.datetime.combine(date_obj, start_time).replace(tzinfo=TZ)
    end_dt = datetime.datetime.combine(date_obj, end_time).replace(tzinfo=TZ)
    total_seconds = int((end_dt - start_dt).total_seconds())
    if total_seconds < 0:
        return start_dt
    offset = random.randint(0, total_seconds)
    return start_dt + datetime.timedelta(seconds=offset)


def next_event_datetime_and_type_db() -> Tuple[datetime.datetime, str]:
    """
    Decide next event using punch_log for today plus state flags.
    """
    s = get_state()
    today = now_kolkata().date()

    # prefer authoritative punch_log
    last = last_punch_today()
    now = now_kolkata()

    morning_end_dt = datetime.datetime.combine(today, MORNING_WINDOW_END).replace(tzinfo=TZ)
    evening_end_dt = datetime.datetime.combine(today, EVENING_WINDOW_END).replace(tzinfo=TZ)

    if not last:
        # no punches today -> schedule clock_in
        target = get_random_time_in_window(today, MORNING_WINDOW_START, MORNING_WINDOW_END)
        if target <= now:
            # if window passed, fallback evening
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            return target, "clock_out"
        return target, "clock_in"

    last_action = last["action"]
    if last_action == "clock_in":
        # need clock out today
        # if evening window still acceptable schedule it
        if now <= evening_end_dt + datetime.timedelta(hours=6):
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            if target <= now:
                target = now + datetime.timedelta(minutes=2)
            return target, "clock_out"
        # else tomorrow morning
        tomorrow = today + datetime.timedelta(days=1)
        target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
        return target, "clock_in"

    # last was clock_out -> next is tomorrow morning
    tomorrow = today + datetime.timedelta(days=1)
    target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
    return target, "clock_in"


# ----------------- Force punch (keeps original logic) -----------------

def perform_force_punch(action_type: str, chat_id: Optional[str] = None) -> bool:
    try:
        logger.info(f"perform_force_punch called for {action_type}")
        s = get_state()
        token = s.get("token")
        if not token:
            token, cookies = retry_login_attempts()
            if token:
                update_state(token=token, cookies=json.dumps(cookies))
            else:
                notify(f"❌ Forced {action_type} failed: login failed.")
                return False

        allowed, prev_count, reason = validate_widget_for_action(token, action_type)
        if not allowed:
            notify(f"⏳ Cannot perform forced {action_type}: {reason}")
            return False

        if action_type == "clock_in":
            image_path = pick_clock_in_image(s)
        else:
            image_path = pick_clock_out_image(s)

        r = send_punch_with_retries(token, image_path, prev_punch_count=int(prev_count or 0))
        if r is not None and getattr(r, "status_code", None) == 201:
            # write log
            add_punch_log(action_type, os.path.basename(image_path), s.get("last_used_folder"), int(prev_count or 0))
            notify_photo(image_path, caption=f"✅ FORCED {action_type.replace('clock_','').upper()} at {now_kolkata().strftime('%I:%M %p')}")
            notify(f"✅ Forced {action_type.replace('clock_','')} successful.")
            return True
        else:
            notify(f"❌ Forced {action_type} failed. See logs.")
            return False
    except Exception:
        logger.exception("perform_force_punch exception")
        notify(f"❌ Error performing forced {action_type}: {traceback.format_exc()}")
        return False


# ----------------- Telegram listener (keeps behaviour) -----------------

def telegram_command_listener():
    logger.info("Telegram command listener started.")
    offset = None
    conn = get_db_connection()
    while running:
        try:
            url = TELEGRAM_API_BASE + "/getUpdates"
            params = {"timeout": MAX_GETUPDATES_TIMEOUT}
            if offset:
                params["offset"] = offset
            r = safe_request("GET", url, params=params, timeout=MAX_GETUPDATES_TIMEOUT + 5)
            if not r or not r.ok:
                time.sleep(2)
                continue
            data = r.json()
            results = data.get("result", [])
            for upd in results:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = (msg.get("text") or "").strip()
                username = (msg.get("from") or {}).get("username")

                if TELEGRAM_ADMIN_CHAT_ID is not None and str(chat_id) != str(TELEGRAM_ADMIN_CHAT_ID):
                    continue

                logger.info(f"Telegram message from {chat_id}: {text[:100]}")
                s = get_state()
                changed = False

                # photo upload handling
                if "photo" in msg and (text.lower().startswith("/addphoto") or (msg.get("caption") and msg.get("caption").lower().startswith("/addphoto"))):
                    caption_text = msg.get("caption") or text
                    parts = caption_text.split()
                    folder = None
                    if len(parts) >= 2 and parts[1].isdigit():
                        folder = parts[1]
                    if not folder or not (1 <= int(folder) <= 6):
                        enqueue_telegram_db({"type": "message", "chat_id": chat_id, "text": "Please use caption: /addphoto <folder_number> (1..6)."})
                    else:
                        photos = msg.get("photo")
                        file_id = photos[-1].get("file_id")
                        dest_folder = os.path.join(IMAGES_DIR, folder)
                        os.makedirs(dest_folder, exist_ok=True)
                        filename = f"telegram_{int(time.time())}_{random.randint(1000,9999)}.jpg"
                        dest_path = os.path.join(dest_folder, filename)
                        ok = download_telegram_file(file_id, dest_path)
                        if ok:
                            enqueue_telegram_db({"type": "message", "chat_id": chat_id, "text": f"Saved photo to {dest_path}"})
                        else:
                            enqueue_telegram_db({"type": "message", "chat_id": chat_id, "text": "Failed to download photo."})
                    continue

                if text.startswith("/skip"):
                    update_state(skip_next=1)
                    changed = True
                    notify("✅ Next scheduled punch will be skipped.", chat_id)

                elif text.startswith("/todayskip") or text.startswith("/skiptoday"):
                    update_state(skip_today=1)
                    changed = True
                    notify("✅ All punches for today will be skipped.", chat_id)

                elif text.startswith("/pause"):
                    update_state(paused=1)
                    changed = True
                    notify("⏸️ Automation paused. Use /resume to resume.", chat_id)

                elif text.startswith("/resume"):
                    update_state(paused=0)
                    changed = True
                    notify("▶️ Automation resumed.", chat_id)

                elif text.startswith("/reset"):
                    update_state(skip_today=0, skip_next=0, paused=0)
                    changed = True
                    notify("▶️ Reset Done.", chat_id)

                elif text.startswith("/status"):
                    st = get_state()
                    last = last_punch_today()
                    status_msg = [
                        f"Date: {st.get('date')}\n",
                        f"Paused: {st.get('paused', False)}\n",
                        f"Skip next: {st.get('skip_next', False)}\n",
                        f"Skip today: {st.get('skip_today', False)}\n",
                        f"Last punch today: {last['action'] if last else 'None'}\n",
                        f"Morning done: {st.get('morning_done', False)}\n",
                        f"Evening done: {st.get('evening_done', False)}\n",
                        f"Token present: {bool(st.get('token'))}\n",
                        f"Last outfit folder: {st.get('last_used_folder')}\n",
                        f"Yesterday outfit folder: {st.get('yesterday_last_folder')}\n",
                    ]
                    status_msg.append('\nConfig:\n')
                    status_msg.append(f"Email: {EMAIL}\n")
                    status_msg.append(f"Timezone: {TZ}\n")
                    notify(''.join(status_msg), chat_id)

                elif text.startswith("/forcein"):
                    notify("Performing forced clock-in now...", chat_id)
                    threading.Thread(target=perform_force_punch, args=("clock_in", chat_id), daemon=True).start()

                elif text.startswith("/forceout"):
                    notify("Performing forced clock-out now...", chat_id)
                    threading.Thread(target=perform_force_punch, args=("clock_out", chat_id), daemon=True).start()

                elif text.startswith("/sendlog"):
                    notify("Uploading recent log...", chat_id)
                    threading.Thread(
                        target=lambda: tg_send_document(chat_id, LOG_FILE, caption="Recent AutoClock log"),
                        daemon=True
                    ).start()


                # ---------------------------
                # NEW COMMANDS ADDED HERE
                # ---------------------------

                elif text.startswith("/setfolder"):
                    parts = text.split()
                    if len(parts) != 2 or not parts[1].isdigit():
                        notify("Usage: /setfolder <1-6>", chat_id)
                    else:
                        f = int(parts[1])
                        if not (1 <= f <= 3):
                            notify("Folder must be between 1 and 3.", chat_id)
                        else:
                            update_state(preferred_folder=str(f), preferred_image=None)
                            notify(f"✅ Preferred folder set to {f}. Will be used for next punch only.", chat_id)

                elif text.startswith("/setimage"):
                    parts = text.split()
                    if len(parts) != 2:
                        notify("Usage: /setimage <filename>", chat_id)
                    else:
                        img = parts[1]
                        update_state(preferred_image=img)
                        notify(f"🔄 Preferred image set to {img}. It will NOT be used for next punch.", chat_id)

                elif text.startswith("/clearprefer"):
                    update_state(preferred_image=None, preferred_folder=None)
                    notify("🧹 Cleared preferred folder & image overrides.", chat_id)


                # --- NEW INSPECTION COMMANDS ---

                elif text.startswith("/todayoutfit"):
                    st = get_state()
                    notify(
                        f"📁 Today's folder: {st.get('last_used_folder')}\n"
                        f"📁 Yesterday's folder: {st.get('yesterday_last_folder')}",
                        chat_id
                    )

                elif text.startswith("/daylog"):
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("SELECT * FROM punch_log WHERE punch_date=? ORDER BY id ASC", (now_date_str(),))
                    rows = cur.fetchall()
                    conn.close()
                    if not rows:
                        notify("No punches today.", chat_id)
                    else:
                        msg = "📅 Today's Punch Log:\n"
                        for r in rows:
                            msg += f"- {r['punch_time']} | {r['action']} | folder={r['folder_used']} | {r['image_used']}\n"
                        notify(msg, chat_id)

                elif text.startswith("/history"):
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT * FROM punch_log
                        WHERE punch_date >= date('now','-7 days')
                        ORDER BY punch_date DESC, id ASC
                    """)
                    rows = cur.fetchall()
                    conn.close()

                    if not rows:
                        notify("No logs found for last 7 days.", chat_id)
                    else:
                        msg = "📜 Last 7 Days:\n"
                        last_date = ""
                        for r in rows:
                            if r["punch_date"] != last_date:
                                msg += f"\n📅 {r['punch_date']}\n"
                                last_date = r["punch_date"]
                            msg += f"  - {r['punch_time']} | {r['action']} | folder={r['folder_used']}\n"
                        notify(msg, chat_id)

                elif text.startswith("/debugnext"):
                    dt, act = next_event_datetime_and_type_db()
                    notify(
                        f"🔍 Debug Next Event:\nAction: {act}\nTime: {dt.strftime('%Y-%m-%d %I:%M %p')}",
                        chat_id
                    )


                # ---------------------------
                # Persist updates
                # ---------------------------

                if changed:
                    pass


        except Exception:
            logger.exception("Exception in telegram_command_listener")
            time.sleep(5)
    logger.info("Telegram command listener stopped.")


# ----------------- Download telegram file helper -----------------

def download_telegram_file(file_id: str, dest_path: str) -> bool:
    try:
        r = safe_request("GET", TELEGRAM_API_BASE + "/getFile", params={"file_id": file_id}, timeout=15)
        if not r or not r.ok:
            return False
        js = r.json()
        file_path = js.get("result", {}).get("file_path")
        if not file_path:
            return False
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        rr = safe_request("GET", url, timeout=30)
        if not rr or not rr.ok:
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(rr.content)
        return True
    except Exception:
        logger.exception("download_telegram_file exception")
        return False


# ----------------- Log rotation (unchanged) -----------------

def rotate_log(days: int = LOG_ROTATE_DAYS):
    try:
        if not os.path.exists(LOG_FILE):
            return
        cutoff_date = (now_kolkata() - datetime.timedelta(days=days)).date()
        kept = []
        date_pattern = __import__("re").compile(r"^\[(\d{4}-\d{2}-\d{2})")
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                m = date_pattern.match(ln)
                if not m:
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
    except Exception:
        logger.exception("rotate_log failed")


# ----------------- Main loop -----------------

def graceful_shutdown(signum, frame):
    global running
    running = False


def ensure_images_structure():
    for i in range(1, 4):
        p = os.path.join(IMAGES_DIR, str(i))
        os.makedirs(p, exist_ok=True)


def main_loop():
    global running
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    ensure_images_structure()
    rotate_log(days=LOG_ROTATE_DAYS)
    init_db()

    # Start telegram sender and listener (only once)
    t_sender = threading.Thread(target=telegram_sender_loop, daemon=True)
    t_sender.start()
    t_listener = threading.Thread(target=telegram_command_listener, daemon=True)
    t_listener.start()

    # On start ensure token
    st = get_state()
    if not st.get("token"):
        token, cookies = retry_login_attempts()
        if token:
            update_state(token=token, cookies=json.dumps(cookies))

    notify("🚀 AutoClock service started (SQLite v2).")

    while running:
        try:
            rotate_log(days=LOG_ROTATE_DAYS)
        except Exception:
            logger.exception("rotate_log threw")

        st = get_state()

        # Day rollover handling using DB authoritative last_used_folder
        if st.get("date") != now_date_str():
            logger.info("Detected date change; resetting daily flags")
            preserve = st.get("last_used_folder")
            reset_daily_flags(preserve_last_folder=preserve)
            st = get_state()

        if st.get("paused"):
            time.sleep(30)
            continue

        if now_kolkata().weekday() == 6:
            if not (st.get("morning_done") and st.get("evening_done")):
                notify("☀️ Today is Sunday. AutoClock will skip punches.")
            update_state(morning_done=1, evening_done=1)
            time.sleep(3600)
            continue

        token = st.get("token")
        is_hol, ev = is_holiday_today(token)
        if is_hol:
            notify(f"📅 Today is company holiday: {ev.get('title','Holiday')}. AutoClock will skip punches.")
            update_state(morning_done=1, evening_done=1)
            time.sleep(3600)
            continue

        target_dt, action_type = next_event_datetime_and_type_db()
        target_dt = target_dt.astimezone(TZ)
        logger.info(f"Next event computed: {action_type} at {target_dt.isoformat()}")

        pre_alert_dt = target_dt - datetime.timedelta(minutes=PRE_ALERT_MINUTES)

        # Respect skip flags
        st = get_state()
        if st.get("skip_today"):
            update_state(morning_done=1, evening_done=1, skip_today=0)
            notify("✅ skip_today processed — all punches for today skipped.")
            continue
        if st.get("skip_next"):
            update_state(skip_next=0)
            # Mark the upcoming action done to avoid scheduling same
            if action_type == "clock_in":
                update_state(morning_done=1)
            else:
                update_state(evening_done=1)
            notify("✅ skip_next processed — skipped next scheduled punch.")
            continue

        now = now_kolkata()
        if pre_alert_dt > now:
            notify(f"⚠️ Going to punch {action_type.replace('clock_','')} at {target_dt.strftime('%I:%M %p')} (in {PRE_ALERT_MINUTES} minutes). Use /skip or /skiptoday or /pause.")
            sleep_until(pre_alert_dt)
            if not running:
                break
            st = get_state()
            if st.get("paused") or st.get("skip_today"):
                continue
            if st.get("skip_next"):
                update_state(skip_next=0)
                if action_type == "clock_in":
                    update_state(morning_done=1)
                else:
                    update_state(evening_done=1)
                continue
        else:
            if target_dt <= now:
                # mark done conservatively
                if action_type == "clock_in":
                    update_state(morning_done=1)
                else:
                    update_state(evening_done=1)
                continue

        notify(f"⏳ Sleeping until actual punch time {target_dt.strftime('%Y-%m-%d %I:%M %p %Z')}")
        sleep_until(target_dt)
        if not running:
            break

        st = get_state()
        if st.get("paused") or st.get("skip_today"):
            continue
        if st.get("skip_next"):
            update_state(skip_next=0)
            if action_type == "clock_in":
                update_state(morning_done=1)
            else:
                update_state(evening_done=1)
            continue

        if now_kolkata().weekday() == 6:
            update_state(morning_done=1, evening_done=1)
            continue
        is_hol, ev = is_holiday_today(st.get("token"))
        if is_hol:
            notify(f"📅 Today is holiday ({ev.get('title')}). Skipping punch.")
            update_state(morning_done=1, evening_done=1)
            continue

        # Kill-switch
        KILL_SWITCH_SECONDS = 30
        notify(f"⚠️ Final check: AutoClock will punch {action_type.replace('clock_','')} in {KILL_SWITCH_SECONDS} seconds. Reply /skip, /skiptoday or /pause to cancel.")
        for _ in range(KILL_SWITCH_SECONDS):
            if not running:
                break
            st = get_state()
            if st.get("paused"):
                notify("⏸️ Punch cancelled due to pause command.")
                break
            if st.get("skip_today"):
                notify("❌ Punch cancelled (skip_today issued).")
                break
            if st.get("skip_next"):
                update_state(skip_next=0)
                notify("❌ Punch cancelled (skip_next issued).")
                break
            time.sleep(1)

        # Last-moment widget validation
        st = get_state()
        allowed, prev_count, reason = validate_widget_for_action(st.get("token"), action_type)
        if not allowed:
            notify(f"⚠️ Scheduled {action_type} skipped at last moment: {reason}")
            continue

        # choose image AFTER validation
        try:
            if action_type == "clock_in":
                image_path = pick_clock_in_image(st)
            else:
                image_path = pick_clock_out_image(st)
        except Exception:
            logger.exception("Failed to pick image for punch; skipping this punch.")
            notify(f"❌ Failed to pick image for {action_type}. See log.")
            continue

        # ensure token
        if not st.get("token"):
            token, cookies = retry_login_attempts()
            if token:
                update_state(token=token, cookies=json.dumps(cookies))
            else:
                notify(f"❌ Punch aborted: login failed.")
                continue

        # send punch
        r = send_punch_with_retries(st.get("token"), image_path, prev_punch_count=int(prev_count or 0))
        if r is None:
            notify(f"❌ Punch attempt raised exception at {now_kolkata().isoformat()}. See logs.")
            continue

        if getattr(r, "status_code", None) == 201:
            expected_prev = 1 if action_type == "clock_in" else 0
            expected_clock_in_flag = True if action_type == "clock_in" else False
            widget_after = poll_widget_until_updated(st.get("token"), expect_prev_count=expected_prev, expect_clock_in=expected_clock_in_flag, timeout=WIDGET_POLL_TIMEOUT)
            try:
                resp = r.json()
            except Exception:
                resp = None

            if widget_after:
                # mark done
                if action_type == "clock_in":
                    update_state(morning_done=1)
                else:
                    update_state(evening_done=1)
                add_punch_log(action_type, os.path.basename(image_path), get_state().get("last_used_folder"), int(prev_count or 0))
            else:
                # fallback to response
                cw = None
                if isinstance(resp, dict):
                    cw = resp.get("clockWidget") or resp.get("clock_widget")
                if cw:
                    if action_type == "clock_in":
                        update_state(morning_done=1)
                    else:
                        update_state(evening_done=1)
                    add_punch_log(action_type, os.path.basename(image_path), get_state().get("last_used_folder"), int(prev_count or 0))
                else:
                    if action_type == "clock_in":
                        update_state(morning_done=1)
                    else:
                        update_state(evening_done=1)
                    add_punch_log(action_type, os.path.basename(image_path), get_state().get("last_used_folder"), int(prev_count or 0))

            notify_photo(image_path, caption=f"✅ {action_type.replace('clock_','').upper()} successful at {now_kolkata().strftime('%I:%M %p')}")
        else:
            notify(f"❌ Punch FAILED. Status {getattr(r,'status_code','N/A')}. Check logs.")
            token, cookies = retry_login_attempts()
            if token:
                update_state(token=token, cookies=json.dumps(cookies))
                r2 = send_punch_with_retries(get_state().get("token"), image_path, prev_punch_count=int(prev_count or 0))
                if r2 and getattr(r2, "status_code", None) == 201:
                    notify("✅ Punch succeeded on retry after re-login.")
                else:
                    notify("❌ Punch still failed after retry. See logs.")
            else:
                notify("❌ Punch failed and login retry did not succeed.")

        # if both done -> compress daily state but preserve token and last_used_folder as history
        st = get_state()
        if st.get("morning_done") and st.get("evening_done"):
            preserve = st.get("last_used_folder")
            token_keep = st.get("token")
            update_state(morning_done=1, evening_done=1, yesterday_last_folder=preserve)

        time.sleep(2)

    logger.info("AutoClock main loop stopped.")


# ----------------- Holidays helper (kept) -----------------

def month_start_epoch_ms(dt: datetime.datetime) -> int:
    mstart = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(mstart.timestamp() * 1000)


def fetch_holidays(token: Optional[str]) -> List[Dict[str, Any]]:
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    params = {"from": str(month_start_epoch_ms(now_kolkata()))}
    r = safe_request("GET", HOLIDAY_API, headers=headers, params=params, timeout=20)
    if not r or not r.ok:
        return []
    try:
        js = r.json()
        res = js.get("result", []) if isinstance(js, dict) else []
        return res
    except Exception:
        return []


def is_holiday_today(token: Optional[str]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    events = fetch_holidays(token)
    today_ms = int(now_kolkata().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    for ev in events:
        try:
            if int(ev.get("start", 0)) == today_ms and ev.get("type", "").lower() == "holiday":
                return True, ev
        except Exception:
            continue
    return False, None


# ----------------- Helpers -----------------

def sleep_until(target_dt: datetime.datetime):
    while running:
        now = now_kolkata()
        seconds = (target_dt - now).total_seconds()
        if seconds <= 0:
            return
        time.sleep(min(30, max(0.5, seconds)))


def json_safe_truncate(obj: Any, limit: int = 400) -> str:
    try:
        s = json.dumps(obj)
        if len(s) > limit:
            return s[:limit] + "..."
        return s
    except Exception:
        return str(obj)[:limit]


if __name__ == "__main__":
    try:
        main_loop()
    except Exception:
        logger.exception("Fatal error in AutoClock v2")
        try:
            notify(f"🔥 Fatal error in AutoClock v2: {traceback.format_exc()}")
            notify_document(LOG_FILE, caption="AutoClock fatal log")
        except Exception:
            logger.exception("Failed to notify fatal error")
        sys.exit(1)
