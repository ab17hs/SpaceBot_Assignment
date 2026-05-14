"""
Space Bot — Webex chatbot that reports the current ISS location.

Author:  Aboubakar Hameed Sultan (23059674)
Module:  Web Technology (5FTC2167)
Brief:   The bot watches a Webex room. When a user posts a slash command
         (e.g. "/10"), the bot waits N seconds, fetches the current ISS
         position, reverse-geocodes it to a human-readable place, and posts
         the result back to the room.

Architecture (MVC-style):
    Model       – the API client functions (Webex / ISS / Mapbox / SpaceX)
                  that fetch and return raw data.
    Controller  – command_dispatch() interprets a Webex message and decides
                  which model functions to call.
    View        – format_iss_reply() / format_launch_reply() / log()
                  turn data into the strings posted back to Webex or the
                  console.

Environment variables required (loaded from .env automatically if present):
    WEBEX_TOKEN       – a personal access token from developer.webex.com
    GEOCODE_API_KEY   – a Mapbox geocoding access token

The .env file is excluded from Git by .gitignore so secrets never enter
source control.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

# python-dotenv is optional — if it's not installed we silently skip and
# fall back to reading the environment directly.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# =========================================================================
# Configuration
# =========================================================================

WEBEX_BASE          = "https://webexapis.com/v1"
WEBEX_ROOMS_URL     = f"{WEBEX_BASE}/rooms"
WEBEX_MESSAGES_URL  = f"{WEBEX_BASE}/messages"
WEBEX_ME_URL        = f"{WEBEX_BASE}/people/me"

ISS_API_PRIMARY     = "http://api.open-notify.org/iss-now.json"
ISS_API_FALLBACK    = "https://api.wheretheiss.at/v1/satellites/25544"

MAPBOX_BASE         = "https://api.mapbox.com/geocoding/v5/mapbox.places"
SPACEX_NEXT_LAUNCH  = "https://api.spacexdata.com/v5/launches/next"

# /N command bounds (seconds)
MIN_DELAY           = 1
MAX_DELAY           = 60

# Polling settings
POLL_INTERVAL_S     = 3       # how often to check Webex for new messages
REQUEST_TIMEOUT_S   = 10      # per-HTTP-request timeout
MAX_CONSECUTIVE_FAILS = 5     # back off after this many polling errors


# =========================================================================
# Tiny logger — prints with a timestamp so runtime traces are useful
# =========================================================================

def log(level: str, message: str) -> None:
    """Print a timestamped log line. Level is a short label like INFO / ERR."""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {level:5} {message}", flush=True)


# =========================================================================
# Robust HTTP helpers — used by every API call so error handling is in one place
# =========================================================================

def http_get(url: str, *, headers: Optional[dict] = None,
             params: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
    """
    GET <url> and return its JSON.

    Returns:
        (data, None)   on success
        (None, error)  on any failure — connection, timeout, HTTP error,
                       non-JSON body. The error string is safe to log.
    """
    try:
        r = requests.get(url, headers=headers, params=params,
                         timeout=REQUEST_TIMEOUT_S)
    except requests.exceptions.Timeout:
        return None, f"GET timed out after {REQUEST_TIMEOUT_S}s"
    except requests.exceptions.ConnectionError as e:
        return None, f"connection error: {e.__class__.__name__}"
    except requests.exceptions.RequestException as e:
        return None, f"request error: {e}"

    if not r.ok:
        return None, f"HTTP {r.status_code} {r.reason}"

    try:
        return r.json(), None
    except json.JSONDecodeError:
        return None, "response was not valid JSON"


def http_post_json(url: str, *, headers: dict,
                   payload: dict) -> Tuple[Optional[dict], Optional[str]]:
    """POST a JSON body and return the parsed JSON response."""
    try:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=REQUEST_TIMEOUT_S)
    except requests.exceptions.Timeout:
        return None, f"POST timed out after {REQUEST_TIMEOUT_S}s"
    except requests.exceptions.ConnectionError as e:
        return None, f"connection error: {e.__class__.__name__}"
    except requests.exceptions.RequestException as e:
        return None, f"request error: {e}"

    if not r.ok:
        return None, f"HTTP {r.status_code} {r.reason}"

    try:
        return r.json(), None
    except json.JSONDecodeError:
        # Webex returns 200 with a JSON body, but be defensive anyway.
        return {}, None


# =========================================================================
# Webex API (Model layer)
# =========================================================================

def webex_headers(token: str) -> dict:
    """Standard auth header for every Webex call."""
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def webex_me(token: str) -> Tuple[Optional[dict], Optional[str]]:
    """Identify the authenticated user — used to filter out our own messages."""
    return http_get(WEBEX_ME_URL, headers=webex_headers(token))


def webex_list_rooms(token: str) -> Tuple[Optional[list], Optional[str]]:
    """Return the list of rooms the token can see."""
    data, err = http_get(WEBEX_ROOMS_URL, headers=webex_headers(token))
    if err:
        return None, err
    return data.get("items", []), None


def webex_latest_message(token: str,
                         room_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    Return the single most recent message in the room (or None if the room
    is empty). Webex returns messages newest-first, so the latest is
    items[0] — NOT items[-1].
    """
    data, err = http_get(WEBEX_MESSAGES_URL,
                         headers=webex_headers(token),
                         params={"roomId": room_id, "max": 1})
    if err:
        return None, err
    items = data.get("items", [])
    return (items[0] if items else None), None


def webex_post_message(token: str, room_id: str,
                       text: str) -> Tuple[Optional[dict], Optional[str]]:
    """Post a plain-text message to a room."""
    return http_post_json(WEBEX_MESSAGES_URL,
                          headers=webex_headers(token),
                          payload={"roomId": room_id, "text": text})


# =========================================================================
# ISS API (Model layer) — primary + fallback
# =========================================================================

def get_iss_location() -> Tuple[Optional[float], Optional[float],
                                Optional[str], Optional[str]]:
    """
    Return (latitude, longitude, readable_utc_time, error).

    Tries open-notify.org first (the API the brief assumes). If that fails
    for any reason (open-notify is intermittent), falls back to
    wheretheiss.at, which returns equivalent data.
    """
    data, err = http_get(ISS_API_PRIMARY)
    if data and "iss_position" in data:
        try:
            lat = float(data["iss_position"]["latitude"])
            lon = float(data["iss_position"]["longitude"])
            ts  = int(data["timestamp"])
            readable = datetime.fromtimestamp(ts, tz=timezone.utc) \
                               .strftime("%a %b %d %H:%M:%S %Y UTC")
            return lat, lon, readable, None
        except (KeyError, ValueError, TypeError) as e:
            log("WARN", f"open-notify response malformed: {e}, trying fallback")

    # Fallback
    data, err = http_get(ISS_API_FALLBACK)
    if data and "latitude" in data:
        try:
            lat = float(data["latitude"])
            lon = float(data["longitude"])
            ts  = int(data["timestamp"])
            readable = datetime.fromtimestamp(ts, tz=timezone.utc) \
                               .strftime("%a %b %d %H:%M:%S %Y UTC")
            return lat, lon, readable, None
        except (KeyError, ValueError, TypeError) as e:
            return None, None, None, f"fallback response malformed: {e}"

    return None, None, None, err or "both ISS APIs unreachable"


# =========================================================================
# Mapbox Geocoding API (Model layer)
# =========================================================================

def reverse_geocode(lat: float, lon: float,
                    api_key: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (place_name, error). Falls back to a coordinate string when
    Mapbox returns no features (e.g. the ISS is over open ocean).
    """
    url = f"{MAPBOX_BASE}/{lon},{lat}.json"
    data, err = http_get(url, params={"access_token": api_key, "limit": 1})
    if err:
        return None, err
    features = data.get("features", [])
    if not features:
        return f"open ocean near ({lat:.2f}, {lon:.2f})", None
    return features[0].get("place_name", "an unnamed place"), None


# =========================================================================
# SpaceX API (Model layer) — Extended Feature
# =========================================================================

def next_spacex_launch() -> Tuple[Optional[dict], Optional[str]]:
    """Return summary information about the next scheduled SpaceX launch."""
    data, err = http_get(SPACEX_NEXT_LAUNCH)
    if err:
        return None, err
    try:
        return {
            "name":       data.get("name", "unknown mission"),
            "date_utc":   data.get("date_utc", "unknown date"),
            "details":    data.get("details") or "no further details available",
            "flight":     data.get("flight_number", "?"),
        }, None
    except AttributeError:
        return None, "unexpected SpaceX response shape"


# =========================================================================
# View layer — turn data into human-readable strings
# =========================================================================

def format_iss_reply(lat: float, lon: float,
                     readable_time: str, place: str) -> str:
    """The string the bot posts to Webex after an ISS check."""
    return (f"🛰️  At {readable_time}, the ISS was passing over **{place}** "
            f"({lat:.4f}°, {lon:.4f}°).")


def format_launch_reply(info: dict) -> str:
    """The string the bot posts to Webex for /launch."""
    return (f"🚀  Next SpaceX launch — flight {info['flight']}: "
            f"**{info['name']}** at {info['date_utc']}. {info['details']}")


HELP_TEXT = (
    "Available commands:\n"
    f"  /N         — wait N seconds (1–{MAX_DELAY}), then report the ISS position\n"
    "  /launch    — show the next scheduled SpaceX launch\n"
    "  /help      — show this help message"
)


# =========================================================================
# Controller — interpret a Webex message and dispatch the right handler
# =========================================================================

def parse_delay_command(text: str) -> Optional[int]:
    """
    Return N if text is exactly '/N' for an integer N in [MIN_DELAY, MAX_DELAY].
    Return None for anything else (no command, out of range, not an integer).
    """
    if not text or not text.startswith("/"):
        return None
    body = text[1:].strip()
    if not body.isdigit():
        return None
    try:
        n = int(body)
    except ValueError:
        return None
    if n < MIN_DELAY or n > MAX_DELAY:
        return None
    return n


def handle_message(token: str, room_id: str, text: str,
                   geocode_key: str) -> None:
    """Route a single incoming text command to the right action."""
    text = (text or "").strip()
    log("INFO", f"command received: {text!r}")

    # /help
    if text.lower() in ("/help", "/?", "/h"):
        webex_post_message(token, room_id, HELP_TEXT)
        return

    # /launch — extended feature
    if text.lower() == "/launch":
        info, err = next_spacex_launch()
        if err:
            webex_post_message(token, room_id,
                               f"⚠️ Could not reach SpaceX API: {err}")
            return
        webex_post_message(token, room_id, format_launch_reply(info))
        return

    # /N — delayed ISS report
    delay = parse_delay_command(text)
    if delay is not None:
        webex_post_message(
            token, room_id,
            f"⏱ Waiting {delay} second{'s' if delay != 1 else ''} before "
            "checking the ISS position…"
        )
        time.sleep(delay)

        lat, lon, readable, err = get_iss_location()
        if err:
            webex_post_message(token, room_id,
                               f"⚠️ Could not reach ISS API: {err}")
            return

        place, err = reverse_geocode(lat, lon, geocode_key)
        if err:
            place = f"({lat:.4f}°, {lon:.4f}°)"
            log("WARN", f"geocode failed, using coordinates only: {err}")

        webex_post_message(token, room_id,
                           format_iss_reply(lat, lon, readable, place))
        return

    # Anything else — only respond if it looks like a slash command, so the
    # bot doesn't spam the room with help text for every human conversation.
    if text.startswith("/"):
        webex_post_message(token, room_id,
                           "❓ Unknown command. " + HELP_TEXT)


# =========================================================================
# Set-up — token + room selection with retry loops
# =========================================================================

def acquire_webex_token() -> Optional[str]:
    """
    Prefer the WEBEX_TOKEN environment variable (recommended path).
    Otherwise ask the user — never store the token in source.
    Returns None if the user gives up.
    """
    env_token = os.getenv("WEBEX_TOKEN", "").strip()
    if env_token:
        log("INFO", "using WEBEX_TOKEN from environment")
        return env_token

    log("WARN", "WEBEX_TOKEN not set in environment — falling back to prompt")
    print("To set this permanently, create a .env file with: "
          "WEBEX_TOKEN=...\n")

    for attempt in range(3):
        token = input(f"Enter your Webex access token (attempt "
                      f"{attempt + 1}/3, blank to cancel): ").strip()
        if not token:
            return None
        # Validate by calling /people/me
        me, err = webex_me(token)
        if me is not None:
            log("INFO", f"authenticated as {me.get('emails', ['?'])[0]}")
            return token
        log("ERR ", f"token rejected: {err}. Try again.")
    return None


def acquire_geocode_key() -> Optional[str]:
    """Read GEOCODE_API_KEY from env, prompt as a fallback."""
    key = os.getenv("GEOCODE_API_KEY", "").strip()
    if key:
        return key
    log("WARN", "GEOCODE_API_KEY not set — prompting once")
    key = input("Enter your Mapbox access token (blank to cancel): ").strip()
    return key or None


def choose_room(token: str) -> Optional[Tuple[str, str]]:
    """
    Show the user the rooms the token can see and let them pick one.
    Returns (room_id, room_title) or None if cancelled.
    """
    for attempt in range(3):
        rooms, err = webex_list_rooms(token)
        if err:
            log("ERR ", f"could not list rooms: {err}")
            return None

        if not rooms:
            log("ERR ", "this token has access to no rooms.")
            return None

        print("\nAvailable Webex rooms:")
        for i, r in enumerate(rooms, start=1):
            print(f"  {i:>2}. [{r.get('type','?'):>5}] {r.get('title','(no title)')}")

        choice = input("\nEnter the room number to monitor "
                       "(or blank to cancel): ").strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(rooms):
            r = rooms[int(choice) - 1]
            return r["id"], r.get("title", "(no title)")
        log("WARN", f"invalid selection (attempt {attempt + 1}/3). "
                   "Enter a number from the list.")
    return None


# =========================================================================
# Main polling loop
# =========================================================================

def monitor_room(token: str, room_id: str, bot_email: str,
                 geocode_key: str) -> None:
    """
    Poll the room every POLL_INTERVAL_S seconds. For every new message that
    is NOT from this bot itself, dispatch to handle_message().
    """
    # Anchor: only respond to messages newer than whatever was latest at start
    latest, err = webex_latest_message(token, room_id)
    last_seen_id = latest["id"] if latest else None
    log("INFO", f"monitoring room — anchored at message {last_seen_id}")
    log("INFO", "type /help in the Webex room to see available commands. "
                "Press Ctrl+C to stop.")

    consecutive_fails = 0
    while True:
        try:
            time.sleep(POLL_INTERVAL_S)
            msg, err = webex_latest_message(token, room_id)
            if err:
                consecutive_fails += 1
                log("WARN", f"poll error ({consecutive_fails}): {err}")
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    log("ERR ", "too many consecutive failures — backing off 30s")
                    time.sleep(30)
                    consecutive_fails = 0
                continue
            consecutive_fails = 0

            if msg is None or msg["id"] == last_seen_id:
                continue  # no new message yet

            # Skip the bot's own messages so it doesn't reply to itself
            sender = (msg.get("personEmail") or "").lower()
            if sender == bot_email.lower():
                last_seen_id = msg["id"]
                continue

            text = msg.get("text", "")
            handle_message(token, room_id, text, geocode_key)
            last_seen_id = msg["id"]

        except KeyboardInterrupt:
            log("INFO", "stop requested — exiting.")
            return


# =========================================================================
# Entry point
# =========================================================================

def main() -> int:
    log("INFO", "Space Bot starting up.")

    token = acquire_webex_token()
    if not token:
        log("ERR ", "no Webex token provided. Exiting.")
        return 1

    geocode_key = acquire_geocode_key()
    if not geocode_key:
        log("ERR ", "no Mapbox key provided. Exiting.")
        return 1

    me, err = webex_me(token)
    if err:
        log("ERR ", f"could not identify bot account: {err}")
        return 1
    bot_email = (me.get("emails") or [""])[0]
    log("INFO", f"bot identity: {bot_email}")

    selection = choose_room(token)
    if not selection:
        log("ERR ", "no room selected. Exiting.")
        return 1
    room_id, room_title = selection
    log("INFO", f"will monitor room: {room_title}")

    monitor_room(token, room_id, bot_email, geocode_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
