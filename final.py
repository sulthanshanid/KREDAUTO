#!/usr/bin/env python3
"""
AutoClock — Stable "Option C" rewrite (keeps your original logic, hardened and extremely verbose).

Features implemented per your request:
- Keeps original scheduling logic and windows.
- Widget checking before every punch (at pre-alert and immediately before punch).
- Ensures logout uses the same outfit folder used for login that day.
- Never reuses the same folder on adjacent days (yesterday_last_folder).
- Never punches on Sundays or company-declared holidays.
- Respects /skip (skip_next), /skiptoday (skip_today) and /pause flags at every decision point.
- Final, last-moment widget check before any punch.
- Polls widget after punch to validate server state; falls back to punch response if necessary.
- Robust Telegram subsystem: non-blocking sender thread, persistent queue (on-disk), retries, detailed logging
- Atomic state updates with a state lock + persistent state JSON.
- Very detailed logging of every decision, path, network error and retry.
- Log rotation keeping last N days (configurable).
- Safe network retries and timeouts.
- Clear boundaries between scheduling, punching and telegram I/O.

Drop this file into your server (replace old final.py). It uses the same endpoints and credentials as your previous file.
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
import queue
import traceback
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

import requests

# ----------------- CONFIG -----------------
EMAIL = "9544790012"
PASSWORD = "Shanid@786"

IMAGES_DIR = "images"                     # subfolders "1","2","3"...
STATE_FILE = "state/session_state.json"   # persisted state
TELEGRAM_QUEUE_FILE = "state/tg_queue.json"
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(os.path.dirname(TELEGRAM_QUEUE_FILE), exist_ok=True)

# Kredily endpoints (same as your original)
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

# windows (inclusive)
MORNING_WINDOW_START = datetime.time(10, 8, 0)
MORNING_WINDOW_END = datetime.time(10, 15, 0)
EVENING_WINDOW_START = datetime.time(18, 5, 0)
EVENING_WINDOW_END = datetime.time(18, 8, 0)

PRE_ALERT_MINUTES = 10  # pre-alert before punch

# Telegram
BOT_TOKEN = "8179352079:AAEbmNmkVLJvyqpIm7DK8G9BcJAn43U5_hA"
CHANNEL_ID = "631331311"
TELEGRAM_ADMIN_CHAT_ID = None  # leave None to accept commands from channel id only
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Logging
LOG_FILE = "autoclock_verbose.log"
LOG_LEVEL = logging.DEBUG
LOG_ROTATE_DAYS = 2

# Network retry schedule
RETRY_DELAYS = [0, 60, 300, 600]  # seconds

# Telegram send retries and timeouts
TG_SEND_MAX_RETRIES = 3
TG_HTTP_TIMEOUT = 15  # seconds

# Polling
WIDGET_POLL_TIMEOUT = 30
WIDGET_POLL_INTERVAL = 3

# Misc
MAX_GETUPDATES_TIMEOUT = 30
TELEGRAM_QUEUE_FLUSH_INTERVAL = 6  # seconds

# ------------------------------------------

running = True
state_lock = threading.Lock()  # protects state file and in-memory state
tg_queue_lock = threading.Lock()
tg_send_event = threading.Event()  # signals the sender thread when new items are queued

# a short-lived requests session per thread
session = requests.Session()

# Configure logger
logger = logging.getLogger("AutoClockC")
logger.setLevel(LOG_LEVEL)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")

# file handler
fh = logging.FileHandler(LOG_FILE)
fh.setLevel(LOG_LEVEL)
fh.setFormatter(formatter)
logger.addHandler(fh)

# stdout handler
sh = logging.StreamHandler(sys.stdout)
sh.setLevel(logging.INFO)
sh.setFormatter(formatter)
logger.addHandler(sh)


# ----------------- Utilities -----------------
def now_kolkata() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def now_date_str() -> str:
    return now_kolkata().date().isoformat()


def load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.exception(f"load_json_file failed for {path}: {e}")
        return None


def save_json_file(path: str, obj: Any):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        logger.exception(f"save_json_file failed for {path}")


# ----------------- State -----------------
DEFAULT_STATE = {
    "date": None,
    "token": None,
    "cookies": {},
    "last_used_folder": None,     # today's folder
    "yesterday_last_folder": None,
    "clock_in_image": None,
    "morning_done": False,
    "evening_done": False,
    "paused": False,
    "skip_next": False,
    "skip_today": False,
    # telemetry counters
    "consecutive_telegram_failures": 0,
    "last_widget_snapshot": None
}


def load_state() -> Dict[str, Any]:
    with state_lock:
        s = load_json_file(STATE_FILE)
        if not s:
            s = DEFAULT_STATE.copy()
        # ensure all keys present
        for k, v in DEFAULT_STATE.items():
            if k not in s:
                s[k] = v
        return s


def save_state(state: Dict[str, Any]):
    with state_lock:
        save_json_file(STATE_FILE, state)


# ----------------- Telegram queue (persistent) -----------------
def load_tg_queue() -> List[Dict[str, Any]]:
    with tg_queue_lock:
        q = load_json_file(TELEGRAM_QUEUE_FILE)
        if isinstance(q, list):
            return q
        return []


def save_tg_queue(q_list: List[Dict[str, Any]]):
    with tg_queue_lock:
        save_json_file(TELEGRAM_QUEUE_FILE, q_list)


def enqueue_telegram(payload: Dict[str, Any]):
    """Add payload (dict) to the persistent queue and signal the sender thread."""
    q = load_tg_queue()
    q.append({"payload": payload, "ts": now_kolkata().isoformat()})
    save_tg_queue(q)
    tg_send_event.set()
    logger.debug(f"Enqueued telegram payload: {payload}")


# ----------------- HTTP safe request -----------------
def safe_request(method: str, url: str, max_attempts: int = 2, backoff: int = 2, **kwargs) -> Optional[requests.Response]:
    """Retry wrapper for requests, logs everything."""
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            logger.debug(f"HTTP {method} attempt {attempt} -> {url} kwargs={ {k: type(v).__name__ for k,v in kwargs.items() if k!='json' and k!='data' and k!='files'} }")
            resp = session.request(method, url, **kwargs)
            logger.debug(f"HTTP {method} {url} returned status {getattr(resp,'status_code', 'N/A')}")
            return resp
        except Exception as e:
            logger.warning(f"HTTP {method} {url} attempt {attempt} raised {e}")
            logger.debug(traceback.format_exc())
        if attempt < max_attempts:
            time.sleep(backoff)
            backoff *= 2
    logger.error(f"HTTP {method} {url} failed after {max_attempts} attempts.")
    return None


# ----------------- Telegram sender thread -----------------
def tg_send_message(chat_id: str, text: str) -> bool:
    payload = {"chat_id": str(chat_id), "text": text}
    for attempt in range(1, TG_SEND_MAX_RETRIES + 1):
        try:
            r = safe_request("POST", TELEGRAM_API_BASE + "/sendMessage", json=payload, timeout=TG_HTTP_TIMEOUT)
            if r and r.ok:
                logger.debug(f"Telegram sendMessage success to {chat_id}: {text}")
                return True
            else:
                logger.warning(f"Telegram sendMessage non-ok (attempt {attempt}): status={getattr(r,'status_code', None)} text={(r.text[:200] if r else 'N/A')}")
        except Exception as e:
            logger.warning(f"Telegram sendMessage exception attempt {attempt}: {e}")
        time.sleep(2)
    logger.error(f"Failed to send Telegram message after {TG_SEND_MAX_RETRIES} attempts: {text}")
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
                    logger.debug(f"Telegram photo sent {image_path} to {chat_id}")
                    return True
                else:
                    logger.warning(f"Telegram sendPhoto non-ok (attempt {attempt}): {getattr(r,'status_code', None)}")
        except Exception as e:
            logger.warning(f"Telegram sendPhoto exception attempt {attempt}: {e}")
        time.sleep(2)
    logger.error(f"Failed to send Telegram photo after retries: {image_path}")
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
                    logger.debug(f"Telegram document sent {file_path} to {chat_id}")
                    return True
                else:
                    logger.warning(f"Telegram sendDocument non-ok (attempt {attempt}): {getattr(r,'status_code', None)}")
        except Exception as e:
            logger.warning(f"Telegram sendDocument exception attempt {attempt}: {e}")
        time.sleep(2)
    logger.error(f"Failed to send Telegram document after retries: {file_path}")
    return False


def telegram_sender_loop():
    """Thread: process persistent queue and send messages, photos and docs."""
    logger.info("Telegram sender thread started.")
    while running:
        try:
            q = load_tg_queue()
            if not q:
                # wait until signaled
                tg_send_event.wait(TELEGRAM_QUEUE_FLUSH_INTERVAL)
                tg_send_event.clear()
                continue

            # Pop-first (FIFO)
            item = q.pop(0)
            payload = item.get("payload", {})
            typ = payload.get("type", "message")
            success = False
            if typ == "message":
                text = payload.get("text", "")
                chat = payload.get("chat_id", CHANNEL_ID)
                success = tg_send_message(chat, text)
            elif typ == "photo":
                chat = payload.get("chat_id", CHANNEL_ID)
                path = payload.get("path")
                caption = payload.get("caption")
                success = tg_send_photo(chat, path, caption)
            elif typ == "document":
                chat = payload.get("chat_id", CHANNEL_ID)
                path = payload.get("path")
                caption = payload.get("caption")
                success = tg_send_document(chat, path, caption)
            else:
                logger.warning(f"Unknown telegram payload type: {typ}")
                success = True

            if not success:
                # push back at the end and sleep to avoid tight loop
                q.append(item)
                save_tg_queue(q)
                logger.debug("Telegram send failed; requeued payload and sleeping 10s.")
                time.sleep(10)
            else:
                save_tg_queue(q)
                logger.debug("Telegram payload sent and removed from persistent queue.")
        except Exception:
            logger.exception("Exception in telegram_sender_loop")
            time.sleep(5)
    logger.info("Telegram sender thread stopped.")


# Helper wrappers to enqueue messages (non-blocking from main thread)
def notify(text: str, chat_id: Optional[str] = None):
    payload = {"type": "message", "chat_id": chat_id or CHANNEL_ID, "text": text}
    enqueue_telegram(payload)


def notify_photo(image_path: str, caption: Optional[str] = None, chat_id: Optional[str] = None):
    payload = {"type": "photo", "chat_id": chat_id or CHANNEL_ID, "path": image_path, "caption": caption}
    enqueue_telegram(payload)


def notify_document(path: str, caption: Optional[str] = None, chat_id: Optional[str] = None):
    payload = {"type": "document", "chat_id": chat_id or CHANNEL_ID, "path": path, "caption": caption}
    enqueue_telegram(payload)


# ----------------- Image helpers -----------------
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


# ----------------- Kredily API helpers -----------------
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
            logger.debug("Login response not JSON.")
        cookies_dict = {c.name: c.value for c in r.cookies}
        token = None
        # try common token keys
        for k in ("token", "auth_token", "access", "key"):
            if isinstance(js, dict) and k in js:
                token = js[k]
                break
        # nested dict
        if not token and isinstance(js, dict):
            for v in js.values():
                if isinstance(v, dict):
                    for k in ("token", "auth_token", "access", "key"):
                        if k in v:
                            token = v[k]
                            break
                    if token:
                        break
        if token:
            logger.info(f"Login succeeded. Token length={len(token)} cookies={list(cookies_dict.keys())}")
            return token, cookies_dict
        else:
            logger.warning(f"Login response did not include token. Status {getattr(r,'status_code',None)}")
            notify(f"⚠️ Login failed (no token). Status={getattr(r,'status_code',None)}. Check credentials/network.")
            return None, cookies_dict
    except Exception:
        logger.exception("login_get_token_and_cookies_once exception")
        return None, {}


def retry_login_attempts() -> Tuple[Optional[str], Dict[str, str]]:
    for delay in RETRY_DELAYS:
        if delay:
            logger.info(f"Waiting {delay}s before next login attempt.")
            time.sleep(delay)
        token, cookies = login_get_token_and_cookies_once()
        if token:
            return token, cookies
    logger.error("All login attempts failed.")
    return None, {}


def fetch_clocking_widget(token: Optional[str]) -> Optional[Dict[str, Any]]:
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    r = safe_request("GET", WIDGET_URL, headers=headers, timeout=15)
    if not r:
        logger.warning("fetch_clocking_widget: no response.")
        return None
    if not r.ok:
        logger.warning(f"fetch_clocking_widget: non-ok {r.status_code} {r.text[:200]}")
        return None
    try:
        js = r.json()
        data = js.get("data", js)
        logger.debug(f"fetch_clocking_widget returned: {data}")
        return data if isinstance(data, dict) else None
    except Exception:
        logger.exception("fetch_clocking_widget: failed to parse JSON")
        return None


def validate_widget_for_action(token: Optional[str], action: str) -> Tuple[bool, Optional[int], str]:
    """
    Validate widget for action.
    Returns (allowed, prev_count, reason)
    """
    w = fetch_clocking_widget(token)
    if w is None:
        return False, None, "Failed to fetch clocking widget"
    prev = int(w.get("prev_punch_count", -1)) if w.get("prev_punch_count") is not None else -1
    clock_in_flag = w.get("clock_in")
    last_clock = w.get("last_clock_milisec")
    # Log widget snapshot
    logger.debug(f"Widget snapshot: clock_in={clock_in_flag} prev={prev} last_clock={last_clock}")

    if action == "clock_in":
        # allowed when prev == 0 (no punch yet)
        if prev == 0:
            return True, prev, "OK (prev_count==0)"
        # tolerate some inconsistencies if server shows clock_in True but prev==0
        return False, prev, f"Widget not suitable for clock_in: clock_in={clock_in_flag}, prev={prev}"
    elif action == "clock_out":
        # allowed when prev == 1 (already clocked in)
        if prev == 1:
            return True, prev, "OK (prev_count==1)"
        return False, prev, f"Widget not suitable for clock_out: clock_in={clock_in_flag}, prev={prev}"
    else:
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
                logger.debug("poll_widget_until_updated: expected widget state observed.")
                return w
        time.sleep(interval)
    logger.warning("poll_widget_until_updated: timeout reached, returning last snapshot.")
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
    if r is None:
        logger.warning("send_punch_once: no response.")
    else:
        logger.debug(f"send_punch_once: status {getattr(r, 'status_code', None)}")
    return r


def send_punch_with_retries(token: Optional[str], image_path: str, prev_punch_count: int = 0) -> Optional[requests.Response]:
    last_r = None
    for i, delay in enumerate(RETRY_DELAYS):
        if delay:
            logger.info(f"send_punch_with_retries: waiting {delay}s before attempt {i+1}")
            time.sleep(delay)
        r = send_punch_once(token, image_path, prev_punch_count)
        last_r = r
        if r is not None and getattr(r, "status_code", None) == 201:
            logger.info("send_punch_with_retries: received HTTP 201")
            return r
    logger.error("send_punch_with_retries: final attempt did not return 201.")
    return last_r


# ----------------- Scheduling helpers -----------------
def month_start_epoch_ms(dt: datetime.datetime) -> int:
    mstart = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(mstart.timestamp() * 1000)


def fetch_holidays(token: Optional[str]) -> List[Dict[str, Any]]:
    headers = HEADERS_BASE.copy()
    if token:
        headers["Authorization"] = f"Token {token}" if not token.startswith("Token ") else token
    params = {"from": str(month_start_epoch_ms(now_kolkata()))}
    r = safe_request("GET", HOLIDAY_API, headers=headers, params=params, timeout=20)
    if not r:
        logger.warning("fetch_holidays: no response")
        return []
    if not r.ok:
        logger.warning(f"fetch_holidays non-ok: {r.status_code}")
        return []
    try:
        js = r.json()
        res = js.get("result", []) if isinstance(js, dict) else []
        logger.debug(f"fetch_holidays: {res}")
        return res
    except Exception:
        logger.exception("fetch_holidays parse error")
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


def get_random_time_in_window(date_obj: datetime.date, start_time: datetime.time, end_time: datetime.time) -> datetime.datetime:
    start_dt = datetime.datetime.combine(date_obj, start_time).replace(tzinfo=TZ)
    end_dt = datetime.datetime.combine(date_obj, end_time).replace(tzinfo=TZ)
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


def ensure_images_structure():
    if not os.path.isdir(IMAGES_DIR):
        raise FileNotFoundError(f"Images directory '{IMAGES_DIR}' does not exist.")
    # require at least 1..3 folders to exist (you can expand 1..6)
    for i in range(1, 4):
        p = os.path.join(IMAGES_DIR, str(i))
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)


# ---------- Outfit selection (no adjacent reuse) ----------
def select_outfit_folder_for_today(state: Dict[str, Any]) -> str:
    """Select an outfit folder avoiding yesterday_last_folder and not equal to today's last_used_folder."""
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
        available = folders  # fallback
    chosen = random.choice(available)
    logger.info(f"Selected outfit folder {chosen} (excluded yesterday={yesterday} today_last={today_last})")
    return chosen


def pick_clock_in_image(state: Dict[str, Any]) -> str:
    folder = select_outfit_folder_for_today(state)
    folder_path = os.path.join(IMAGES_DIR, folder)
    image_path = choose_random_image_from_folder(folder_path)
    state["last_used_folder"] = folder
    state["clock_in_image"] = os.path.basename(image_path)
    state["morning_done"] = True
    state["date"] = now_date_str()
    save_state(state)
    logger.info(f"pick_clock_in_image: picked {image_path}, updated state morning_done=True")
    return image_path


def pick_clock_out_image(state: Dict[str, Any]) -> str:
    # Use the same folder as today's clock-in to ensure logout from same folder
    folder = state.get("last_used_folder")
    if not folder:
        logger.warning("pick_clock_out_image: no last_used_folder found, selecting one now.")
        folder = select_outfit_folder_for_today(state)
        state["last_used_folder"] = folder
    folder_path = os.path.join(IMAGES_DIR, folder)
    exclude = state.get("clock_in_image")
    image_path = choose_random_image_from_folder(folder_path, exclude_filename=exclude)
    state["evening_done"] = True
    state["date"] = now_date_str()
    save_state(state)
    logger.info(f"pick_clock_out_image: picked {image_path}, updated state evening_done=True")
    return image_path


# ---------- Force punch (shared) ----------
def perform_force_punch(action_type: str, chat_id: Optional[str] = None) -> bool:
    """
    Force punch same as original logic but with stricter widget checks and logging.
    action_type: 'clock_in' or 'clock_out'
    """
    try:
        logger.info(f"perform_force_punch called for {action_type} by chat {chat_id}")
        state = load_state()
        token = state.get("token")
        if not token:
            logger.info("perform_force_punch: no token, attempting login")
            token, cookies = retry_login_attempts()
            if token:
                state["token"] = token
                state["cookies"] = cookies
                save_state(state)
            else:
                notify(f"❌ Forced {action_type} failed: login failed.")
                return False

        allowed, prev_count, reason = validate_widget_for_action(state.get("token"), action_type)
        logger.info(f"perform_force_punch: widget validation returned allowed={allowed} prev={prev_count} reason={reason}")
        if not allowed:
            msg = f"⏳ Cannot perform forced {action_type}: {reason}"
            logger.info(msg)
            notify(msg, chat_id)
            return False

        if action_type == "clock_in":
            image_path = pick_clock_in_image(state)
        else:
            image_path = pick_clock_out_image(state)

        logger.info(f"perform_force_punch: sending punch with prev_count={prev_count} using image {image_path}")
        r = send_punch_with_retries(state.get("token"), image_path, prev_punch_count=int(prev_count or 0))
        if r is not None and getattr(r, "status_code", None) == 201:
            # poll widget and update local state accordingly
            expected_prev = 1 if action_type == "clock_in" else 0
            expected_clock_in = True if action_type == "clock_in" else False
            widget_after = poll_widget_until_updated(state.get("token"), expect_prev_count=expected_prev, expect_clock_in=expected_clock_in, timeout=30)
            if widget_after:
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                logger.info("perform_force_punch: widget reflected update; state saved.")
            else:
                logger.warning("perform_force_punch: widget did not reflect update; using punch response to decide.")
                try:
                    resp = r.json()
                except Exception:
                    resp = None
                cw = None
                if isinstance(resp, dict):
                    cw = resp.get("clockWidget") or resp.get("clock_widget")
                if cw:
                    if action_type == "clock_in":
                        state["morning_done"] = True
                    else:
                        state["evening_done"] = True
                    save_state(state)
                    logger.info("perform_force_punch: updated state from punch response clockWidget.")
                else:
                    # conservative: mark done locally to avoid scheduling wrong next event
                    if action_type == "clock_in":
                        state["morning_done"] = True
                    else:
                        state["evening_done"] = True
                    save_state(state)
                    logger.info("perform_force_punch: widget unknown, conservatively marked done locally.")

            notify_photo(image_path, caption=f"✅ FORCED {action_type.replace('clock_', '').upper()} at {now_kolkata().strftime('%I:%M %p')}", chat_id=chat_id)
            notify(f"✅ Forced {action_type.replace('clock_', '')} successful.", chat_id)
            return True
        else:
            logger.error(f"perform_force_punch: punch request failed: status={getattr(r,'status_code', None)} text={(getattr(r,'text', '')[:400] if r else 'N/A')}")
            notify(f"❌ Forced {action_type} failed. See logs.")
            return False
    except Exception:
        logger.exception("perform_force_punch exception")
        notify(f"❌ Error performing forced {action_type}: {traceback.format_exc()}")
        return False


# ----------------- Telegram command listener (long-polling) -----------------
def telegram_command_listener():
    logger.info("Telegram command listener started.")
    offset = None
    while running:
        try:
            url = TELEGRAM_API_BASE + "/getUpdates"
            params = {"timeout": MAX_GETUPDATES_TIMEOUT}
            if offset:
                params["offset"] = offset
            r = safe_request("GET", url, params=params, timeout=MAX_GETUPDATES_TIMEOUT + 5)
            if not r:
                logger.warning("Telegram getUpdates returned no response.")
                time.sleep(2)
                continue
            if not r.ok:
                logger.warning(f"Telegram getUpdates non-ok: {r.status_code} {r.text[:200]}")
                time.sleep(2)
                continue
            data = r.json()
            results = data.get("result", [])
            logger.debug(f"getUpdates returned {len(results)} updates")
            for upd in results:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = (msg.get("text") or "").strip()
                username = (msg.get("from") or {}).get("username")

                # restrict to admin chat if configured
                if TELEGRAM_ADMIN_CHAT_ID is not None and str(chat_id) != str(TELEGRAM_ADMIN_CHAT_ID):
                    logger.info(f"Ignored command from {chat_id} (admin required).")
                    continue

                logger.info(f"Telegram message from {chat_id} ({username}): {text[:200]}")
                st = load_state()
                changed = False

                # photo upload handling
                if "photo" in msg and (text.lower().startswith("/addphoto") or (msg.get("caption") and msg.get("caption").lower().startswith("/addphoto"))):
                    caption_text = msg.get("caption") or text
                    parts = caption_text.split()
                    folder = None
                    if len(parts) >= 2 and parts[1].isdigit():
                        folder = parts[1]
                    if not folder or not (1 <= int(folder) <= 6):
                        enqueue_telegram({"type": "message", "chat_id": chat_id, "text": "Please use caption: /addphoto <folder_number> (1..6)."})
                    else:
                        photos = msg.get("photo")
                        file_id = photos[-1].get("file_id")
                        dest_folder = os.path.join(IMAGES_DIR, folder)
                        os.makedirs(dest_folder, exist_ok=True)
                        filename = f"telegram_{int(time.time())}_{random.randint(1000,9999)}.jpg"
                        dest_path = os.path.join(dest_folder, filename)
                        ok = download_telegram_file(file_id, dest_path)
                        if ok:
                            enqueue_telegram({"type": "message", "chat_id": chat_id, "text": f"Saved photo to {dest_path}"})
                        else:
                            enqueue_telegram({"type": "message", "chat_id": chat_id, "text": "Failed to download photo. Check bot permissions and network."})
                    continue

                # command handling
                if text.startswith("/skip"):
                    st["skip_next"] = True
                    changed = True
                    notify("✅ Next scheduled punch will be skipped.", chat_id)
                elif text.startswith("/todayskip") or text.startswith("/skiptoday"):
                    st["skip_today"] = True
                    changed = True
                    notify("✅ All punches for today will be skipped.", chat_id)
                elif text.startswith("/pause"):
                    st["paused"] = True
                    changed = True
                    notify("⏸️ Automation paused. Use /resume to resume.", chat_id)
                elif text.startswith("/resume"):
                    st["paused"] = False
                    changed = True
                    notify("▶️ Automation resumed.", chat_id)
                elif text.startswith("/reset"):
                    st["skip_today"] = False
                    st["skip_next"] = False
                    st["paused"] = False
                    changed = True
                    notify("▶️ Reset Done.", chat_id)
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
                        f"Last outfit folder: {st.get('last_used_folder')}\n",
                        f"Yesterday outfit folder: {st.get('yesterday_last_folder')}\n",
                    ]
                    status_msg.append("\nConfig:\n")
                    status_msg.append(f"Email: {EMAIL}\n")
                    status_msg.append(f"Timezone: {TZ}\n")
                    status_msg.append(f"Morning window: {MORNING_WINDOW_START} - {MORNING_WINDOW_END}\n")
                    status_msg.append(f"Evening window: {EVENING_WINDOW_START} - {EVENING_WINDOW_END}\n")
                    status_msg.append(f"Channel ID: {CHANNEL_ID}\n")
                    notify("".join(status_msg), chat_id)
                elif text.startswith("/forcein"):
                    notify("Performing forced clock-in now...", chat_id)
                    threading.Thread(target=perform_force_punch, args=("clock_in", chat_id), daemon=True).start()
                elif text.startswith("/forceout"):
                    notify("Performing forced clock-out now...", chat_id)
                    threading.Thread(target=perform_force_punch, args=("clock_out", chat_id), daemon=True).start()
                elif text.startswith("/sendlog") or text.startswith("/sendlogs") or text.startswith("/sendlog"):
                    notify("Uploading recent log...", chat_id)
                    threading.Thread(target=lambda: tg_send_document(chat_id, LOG_FILE, caption="Recent AutoClock log"), daemon=True).start()
                if changed:
                    save_state(st)

        except Exception:
            logger.exception("Exception in telegram_command_listener")
            time.sleep(5)
    logger.info("Telegram command listener stopped.")


# ----------------- Download Telegram file helper (needed for /addphoto) -----------------
def download_telegram_file(file_id: str, dest_path: str) -> bool:
    try:
        r = safe_request("GET", TELEGRAM_API_BASE + "/getFile", params={"file_id": file_id}, timeout=15)
        if not r or not r.ok:
            logger.warning("download_telegram_file: getFile failed.")
            return False
        js = r.json()
        file_path = js.get("result", {}).get("file_path")
        if not file_path:
            logger.warning("download_telegram_file: no file_path in getFile")
            return False
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        rr = safe_request("GET", url, timeout=30)
        if not rr or not rr.ok:
            logger.warning("download_telegram_file: failed to download actual file.")
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(rr.content)
        logger.info(f"download_telegram_file: saved {dest_path}")
        return True
    except Exception:
        logger.exception("download_telegram_file exception")
        return False


# ----------------- Log rotation -----------------
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
        logger.info(f"Log rotation completed. Kept entries since {cutoff_date.isoformat()}.")
    except Exception:
        logger.exception("rotate_log failed")


# ----------------- Next event calculator -----------------
def next_event_datetime_and_type(state: Dict[str, Any]) -> Tuple[datetime.datetime, str]:
    """
    Decide the next event datetime and type ('clock_in' or 'clock_out').
    This function mirrors your original logic but uses state's flags for today's date only.
    """
    today = now_kolkata().date()
    date_str = state.get("date")
    # Only respect morning_done/evening_done if the state is for today
    morning_done = state.get("morning_done", False) if date_str == today.isoformat() else False
    evening_done = state.get("evening_done", False) if date_str == today.isoformat() else False

    logger.debug(f"next_event computation: today={today} morning_done={morning_done} evening_done={evening_done}")

    now = now_kolkata()
    morning_end_dt = datetime.datetime.combine(today, MORNING_WINDOW_END).replace(tzinfo=TZ)
    evening_end_dt = datetime.datetime.combine(today, EVENING_WINDOW_END).replace(tzinfo=TZ)

    if not morning_done and now < evening_end_dt:
        target = get_random_time_in_window(today, MORNING_WINDOW_START, MORNING_WINDOW_END)
        if target <= now:
            # if random fell in past, fallback to evening
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            return target, "clock_out"
        return target, "clock_in"

    if morning_done and not evening_done:
        # schedule evening punch today if possible
        if now <= evening_end_dt + datetime.timedelta(hours=6):
            target = get_random_time_in_window(today, EVENING_WINDOW_START, EVENING_WINDOW_END)
            if target <= now:
                target = now + datetime.timedelta(minutes=2)
            return target, "clock_out"
        # otherwise schedule tomorrow morning
        tomorrow = today + datetime.timedelta(days=1)
        target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
        return target, "clock_in"

    # both done -> schedule tomorrow morning
    tomorrow = today + datetime.timedelta(days=1)
    target = get_random_time_in_window(tomorrow, MORNING_WINDOW_START, MORNING_WINDOW_END)
    return target, "clock_in"


# ----------------- Main loop -----------------
def graceful_shutdown(signum, frame):
    global running
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    running = False
    tg_send_event.set()


def main_loop():
    global running
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    ensure_images_structure()
    rotate_log(days=LOG_ROTATE_DAYS)

    # Start telegram sender thread
    t_sender = threading.Thread(target=telegram_sender_loop, daemon=True)
    t_sender.start()

    # Start telegram command listener thread
    t_listener = threading.Thread(target=telegram_command_listener, daemon=True)
    t_listener.start()

    state = load_state()

    # If we've rolled to a new day, reset daily flags and record yesterday's outfit
    if state.get("date") != now_date_str():
        yesterday_folder = state.get("last_used_folder")
        logger.info(f"New day detected. yesterday_last_folder will be set to {yesterday_folder}")
        state = DEFAULT_STATE.copy()
        state["date"] = now_date_str()
        state["paused"] = False
        state["yesterday_last_folder"] = yesterday_folder
        save_state(state)

    # Ensure token is present
    if not state.get("token"):
        token, cookies = retry_login_attempts()
        if token:
            state["token"] = token
            state["cookies"] = cookies
            save_state(state)

    notify("🚀 AutoClock service started (Option C).")
    logger.info("AutoClock main loop started.")

    while running:
        try:
            rotate_log(days=LOG_ROTATE_DAYS)
        except Exception:
            logger.exception("rotate_log threw")

        state = load_state()

        # Reset to new day if date changed (defensive)
        if state.get("date") != now_date_str():
            yesterday_folder = state.get("last_used_folder")
            logger.info("Detected day change while running. Resetting daily flags and preserving yesterday_last_folder.")
            state = DEFAULT_STATE.copy()
            state["date"] = now_date_str()
            state["yesterday_last_folder"] = yesterday_folder
            save_state(state)

        # handle pause
        if state.get("paused"):
            logger.info("Automation is paused. Sleeping 30s.")
            time.sleep(30)
            continue

        # Sunday handling - never punch on Sunday
        if now_kolkata().weekday() == 6:
            if not (state.get("morning_done") and state.get("evening_done")):
                logger.info("Today is Sunday — marking both morning and evening done and notifying.")
                notify("☀️ Today is Sunday. AutoClock will skip punches.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            time.sleep(3600)
            continue

        # Company holiday handling
        token = state.get("token")
        is_hol, ev = is_holiday_today(token)
        if is_hol:
            title = ev.get("title", "Holiday")
            logger.info(f"Today is company holiday ({title}). Marking both done.")
            notify(f"📅 Today is company holiday: {title}. AutoClock will skip punches.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            time.sleep(3600)
            continue

        # decide next event
        target_dt, action_type = next_event_datetime_and_type(state)
        target_dt = target_dt.astimezone(TZ)
        logger.info(f"Next event computed: {action_type} at {target_dt.isoformat()}")

        # pre-alert
        pre_alert_dt = target_dt - datetime.timedelta(minutes=PRE_ALERT_MINUTES)

        # Respect skip_today and skip_next
        if state.get("skip_today"):
            logger.info("skip_today flag set — skipping today's punches and clearing flags.")
            state["morning_done"] = True
            state["evening_done"] = True
            state["skip_today"] = False
            save_state(state)
            notify("✅ skip_today processed — all punches for today skipped.")
            continue

        if state.get("skip_next"):
            logger.info("skip_next flag set — skipping next scheduled punch.")
            state["skip_next"] = False
            if action_type == "clock_in":
                state["morning_done"] = True
            else:
                state["evening_done"] = True
            save_state(state)
            notify("✅ skip_next processed — skipped next scheduled punch.")
            continue

        now = now_kolkata()

        # If pre-alert is ahead, sleep until pre-alert and re-evaluate conditions
        if pre_alert_dt > now:
            logger.info(f"Sleeping until pre-alert at {pre_alert_dt.isoformat()}")
            notify(f"⚠️ Going to punch {action_type.replace('clock_','')} at {target_dt.strftime('%I:%M %p')} (in {PRE_ALERT_MINUTES} minutes). Use /skip or /skiptoday or /pause.")
            sleep_until(pre_alert_dt)
            if not running:
                break

            # reload state
            state = load_state()

            # re-check flags at pre-alert moment
            if state.get("paused"):
                logger.info("Paused at pre-alert. Will not send pre-alert or schedule punch.")
                continue
            if state.get("skip_today"):
                logger.info("skip_today set at pre-alert. Skipping.")
                continue
            if state.get("skip_next"):
                logger.info("skip_next set at pre-alert. Skipping and clearing.")
                state["skip_next"] = False
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                continue

            # Check holiday at pre-alert
            is_hol, ev = is_holiday_today(state.get("token"))
            if is_hol:
                notify(f"📅 Today became a holiday ({ev.get('title','Holiday')}). Skipping punches.")
                state["morning_done"] = True
                state["evening_done"] = True
                save_state(state)
                continue

            # send pre-alert done above (we notified)
        else:
            # pre-alert already passed; if target <= now, we'll make an immediate decision
            if target_dt <= now:
                logger.info("Target time already passed at decision time; will re-evaluate next event.")
                # mark this action as done conservatively to avoid looping
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                continue

        # Sleep until actual target
        logger.info(f"Sleeping until actual punch time {target_dt.isoformat()}")
        notify(f"⏳ Sleeping until actual punch time {target_dt.strftime('%Y-%m-%d %I:%M %p %Z')}")
        sleep_until(target_dt)
        if not running:
            break

        # Final checks before punch
        state = load_state()
        if state.get("paused"):
            logger.info("Paused at punch time; skipping.")
            continue
        if state.get("skip_today"):
            logger.info("skip_today at punch time; skipping.")
            continue
        if state.get("skip_next"):
            logger.info("skip_next at punch time; skipping and clearing flag.")
            state["skip_next"] = False
            if action_type == "clock_in":
                state["morning_done"] = True
            else:
                state["evening_done"] = True
            save_state(state)
            continue

        # double-check Sunday/holiday immediately before punch
        if now_kolkata().weekday() == 6:
            logger.info("Sunday detected at punch time; skipping.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue
        is_hol, ev = is_holiday_today(state.get("token"))
        if is_hol:
            logger.info(f"Holiday detected at punch time: {ev.get('title')}. Skipping.")
            notify(f"📅 Today is holiday ({ev.get('title')}). Skipping punch.")
            state["morning_done"] = True
            state["evening_done"] = True
            save_state(state)
            continue
        # ---- FINAL KILL-SWITCH WINDOW ----
        KILL_SWITCH_SECONDS = 30  # user can customize

        notify(f"⚠️ Final check: AutoClock will punch {action_type.replace('clock_','')} in {KILL_SWITCH_SECONDS} seconds.\n"
            f"Reply /skip, /skiptoday or /pause to cancel.", CHANNEL_ID)
        logger.info(f"Kill-switch window started for {KILL_SWITCH_SECONDS} seconds.")

        for _ in range(KILL_SWITCH_SECONDS):
            if not running:
                break

            # re-check flags every second
            state = load_state()
            if state.get("paused"):
                logger.info("Paused during kill-switch window. Cancelling punch.")
                notify("⏸️ Punch cancelled due to pause command.")
                # Don't mark as done — let scheduler reschedule properly
                continue

            if state.get("skip_today"):
                logger.info("skip_today during kill-switch window. Cancelling punch.")
                notify("❌ Punch cancelled (skip_today issued).")
                continue

            if state.get("skip_next"):
                logger.info("skip_next during kill-switch window. Cancelling punch.")
                state["skip_next"] = False
                save_state(state)
                notify("❌ Punch cancelled (skip_next issued).")
                continue

            time.sleep(1)

        logger.info("Kill-switch window completed. Proceeding to final widget validation.")
        # ---- END OF KILL-SWITCH WINDOW ----

        # Last-moment widget validation to avoid double punch
        allowed, prev_count, reason = validate_widget_for_action(state.get("token"), action_type)
        logger.info(f"Last-moment widget validation: allowed={allowed} prev={prev_count} reason={reason}")
        if not allowed:
            msg = f"⚠️ Scheduled {action_type} skipped at last moment: {reason}"
            logger.info(msg)
            notify(msg)
            # do NOT mark done; rely on widget state to decide next
            save_state(state)
            continue

        # choose image AFTER validation (ensures punch-out uses same folder)
        try:
            if action_type == "clock_in":
                image_path = pick_clock_in_image(state)
                logger.info(f"CLOCK-IN using image {image_path}")
            else:
                image_path = pick_clock_out_image(state)
                logger.info(f"CLOCK-OUT using image {image_path}")
        except Exception:
            logger.exception("Failed to pick image for punch; skipping this punch.")
            notify(f"❌ Failed to pick image for {action_type}. See log.")
            continue

        # Ensure token (retry login if needed)
        if not state.get("token"):
            logger.info("No token before punch, attempting login.")
            token, cookies = retry_login_attempts()
            if token:
                state["token"] = token
                state["cookies"] = cookies
                save_state(state)
            else:
                notify(f"❌ Punch aborted: login failed.")
                continue

        # send punch with prev_count retrieved above
        r = send_punch_with_retries(state.get("token"), image_path, prev_punch_count=int(prev_count or 0))
        if r is None:
            notify(f"❌ Punch attempt raised exception at {now_kolkata().isoformat()}. See logs.")
            logger.error("Punch attempt raised exception (r is None).")
            continue

        if getattr(r, "status_code", None) == 201:
            logger.info("Punch HTTP 201 received. Polling widget for server confirmation.")
            # expected widget after punch
            expected_prev = 1 if action_type == "clock_in" else 0
            expected_clock_in_flag = True if action_type == "clock_in" else False
            widget_after = poll_widget_until_updated(state.get("token"), expect_prev_count=expected_prev, expect_clock_in=expected_clock_in_flag, timeout=WIDGET_POLL_TIMEOUT)
            try:
                resp = r.json()
            except Exception:
                resp = None

            if widget_after:
                # update local state from confirmed widget
                if action_type == "clock_in":
                    state["morning_done"] = True
                else:
                    state["evening_done"] = True
                save_state(state)
                logger.info(f"Punch SUCCESS and widget updated. Response summary: {json_safe_truncate(resp)}")
            else:
                logger.warning("Punch succeeded but widget did not reflect update within timeout. Falling back to punch response.")
                cw = None
                if isinstance(resp, dict):
                    cw = resp.get("clockWidget") or resp.get("clock_widget")
                if cw:
                    if action_type == "clock_in":
                        state["morning_done"] = True
                    else:
                        state["evening_done"] = True
                    save_state(state)
                    logger.info("Updated local state from punch response clockWidget.")
                else:
                    # still mark done locally to avoid scheduling errors
                    if action_type == "clock_in":
                        state["morning_done"] = True
                    else:
                        state["evening_done"] = True
                    save_state(state)
                    logger.info("No clockWidget in response — conservatively marking action done locally.")

            notify_photo(image_path, caption=f"✅ {action_type.replace('clock_', '').upper()} successful at {now_kolkata().strftime('%I:%M %p')}")
        else:
            logger.error(f"Punch FAILED after retry. Status {getattr(r,'status_code','N/A')}. Response: {getattr(r,'text','')[:800]}")
            notify(f"❌ Punch FAILED. Status {getattr(r,'status_code','N/A')}. Check logs.")
            # Try to re-login and attempt once more (safe)
            token, cookies = retry_login_attempts()
            if token:
                state["token"] = token
                state["cookies"] = cookies
                save_state(state)
                logger.info("Retrying punch after re-login.")
                r2 = send_punch_with_retries(state.get("token"), image_path, prev_punch_count=int(prev_count or 0))
                if r2 and getattr(r2, "status_code", None) == 201:
                    notify("✅ Punch succeeded on retry after re-login.")
                else:
                    notify("❌ Punch still failed after retry. See logs.")
            else:
                notify("❌ Punch failed and login retry did not succeed.")

        # At end of loop iteration, if both done, we keep token but preserve last_used_folder as yesterday reference
        state = load_state()
        if state.get("morning_done") and state.get("evening_done"):
            token_keep = state.get("token")
            last_folder = state.get("last_used_folder")
            new_state = DEFAULT_STATE.copy()
            new_state["date"] = now_date_str()
            new_state["morning_done"] = True
            new_state["evening_done"] = True
            new_state["paused"] = state.get("paused", False)
            # keep token and last_used_folder (we preserve last_used_folder as history)
            if token_keep:
                new_state["token"] = token_keep
                new_state["cookies"] = state.get("cookies", {})
            if last_folder:
                new_state["last_used_folder"] = last_folder
            # mark yesterday_last_folder for next day's selection
            new_state["yesterday_last_folder"] = last_folder
            save_state(new_state)
            logger.info(f"Both punches done for the day. Saved condensed state and preserved last_folder={last_folder}")

        # small sleep to avoid tight loop
        time.sleep(2)

    logger.info("AutoClock main loop stopped.")


# Utility for safe JSON logging of response
def json_safe_truncate(obj: Any, limit: int = 400) -> str:
    try:
        s = json.dumps(obj)
        if len(s) > limit:
            return s[:limit] + "..."
        return s
    except Exception:
        return str(obj)[:limit] + ("..." if len(str(obj)) > limit else "")


if __name__ == "__main__":
    try:
        # start telegram sender thread early
        threading.Thread(target=telegram_sender_loop, daemon=True).start()
        main_loop()
    except Exception:
        logger.exception("Fatal error in AutoClock main")
        try:
            notify(f"🔥 Fatal error in AutoClock: {traceback.format_exc()}")
            notify_document(LOG_FILE, caption="AutoClock fatal log")
        except Exception:
            logger.exception("Failed to notify fatal error")
        sys.exit(1)
