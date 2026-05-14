"""
Space Bot — Webex chatbot that reports the current ISS location.

Author:  Aboubakar Hameed Sultan (23059674)
Module:  Web Technology (5FTC2167)
Brief:   The bot watches a Webex room. When a user posts a slash command
         (e.g. "/10"), the bot waits N seconds, fetches the current ISS
         position, reverse-geocodes it to a human-readable place, and posts
         the result back to the room.

Architecture (MVC-style):
    Model      – API client functions (Webex / ISS / Mapbox / SpaceX)
                 that fetch and return raw data.
    Controller – command_dispatch() / handle_message() interpret a Webex
                 message and decide which model functions to call.
    View       – format_iss_reply() / format_launch_reply() / log()
                 turn data into the strings posted back to Webex or the console.

Environment variables (loaded from .env automatically if present):
    WEBEX_TOKEN       – personal access token from developer.webex.com
    GEOCODE_API_KEY   – Mapbox geocoding access token

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

# python-dotenv is optional — silently skip if not installed.
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
POLL_INTERVAL_S       = 3    # how often to check Webex for new messages (seconds)
REQUEST_TIMEOUT_S     = 10   # per-HTTP-request timeout (seconds)
MAX_CONSECUTIVE_FAILS = 5    # back off after this many consecutive polling errors


# =========================================================================
# View layer — logging
# =========================================================================

def log(level: str, message: str) -> None:
    """Print a timestamped log line to stdout. Level is a short label e.g. INFO / WARN / ERR."""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {level:5} {message}", flush=True)


# =========================================================================
# Model layer — robust HTTP helpers
#
# All API calls route through these two functions so that connection errors,
# timeouts, bad status codes, and non-JSON bodies are handled consistently
# in one place rather than scattered around the codebase.
# =========================================================================

def http_get(url: str, *,
             headers: Optional[dict] = None,
             params: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
    """
    Perform an HTTP GET and return the parsed JSON body.

    Returns:
        (data, None)   on success — data is the parsed dict/list.
        (None, error)  on any failure — error is a human-readable string safe to log.
    """
    try:
        response = requests.get(url, headers=headers, params=params,
                                timeout=REQUEST_TIMEOUT_S)
    except requests.exceptions.Timeout:
        return None, f"GET timed out after {REQUEST_TIMEOUT_S}s ({url})"
    except requests.exceptions.ConnectionError as exc:
        return None, f"connection error: {exc.__class__.__name__} ({url})"
    except requests.exceptions.RequestException as exc:
        return None, f"request error: {exc}"

    if not response.ok:
        return None, f"HTTP {response.status_code} {response.reason} ({url})"

    try:
        return response.json(), None
    except json.JSONDecodeError:
        return None, f"response was not valid JSON ({url})"


def http_post_json(url: str, *,
                   headers: dict,
                   payload: dict) -> Tuple[Optional[dict], Optional[str]]:
    """
    Perform an HTTP POST with a JSON body and return the parsed JSON response.

    Returns:
        (data, None)   on success.
        (None, error)  on any failure.
    """
    try:
        response = requests.post(url, headers=headers, json=payload,
                                 timeout=REQUEST_TIMEOUT_S)
    except requests.exceptions.Timeout:
        return None, f"POST timed out after {REQUEST_TIMEOUT_S}s ({url})"
    except requests.exceptions.ConnectionError as exc:
        return None, f"connection error: {exc.__class__.__name__} ({url})"
    except requests.exceptions.RequestException as exc:
        return None, f"request error: {exc}"

    if not response.ok:
        return None, f"HTTP {response.status_code} {response.reason} ({url})"

    try:
        return response.json(), None
    except json.JSONDecodeError:
        # Webex normally returns JSON on 200, but be defensive.
        return {}, None


# =========================================================================
# Model layer — Webex API
# =========================================================================

def _webex_headers(token: str) -> dict:
    """Build the standard Authorization + Content-Type headers for Webex calls."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


def webex_me(token: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    Return the /people/me profile for the supplied token.
    Used on startup to identify the bot's own email address so we can
    filter out messages the bot sends itself.
    """
    return http_get(WEBEX_ME_URL, headers=_webex_headers(token))


def webex_list_rooms(token: str) -> Tuple[Optional[list], Optional[str]]:
    """Return the list of Webex rooms the token has access to."""
    data, err = http_get(WEBEX_ROOMS_URL, headers=_webex_headers(token))
    if err:
        return None, err
    return data.get("items", []), None


def webex_latest_message(token: str,
                          room_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    Return the single most-recent message in the room, or None if the room
    is empty.

    Webex returns messages newest-first, so items[0] is the latest.
    We fetch max=10 to give a small buffer, but we only inspect items[0]
    here — duplicate suppression is handled in the polling loop via
    last_seen_id.
    """
    data, err = http_get(
        WEBEX_MESSAGES_URL,
        headers=_webex_headers(token),
        params={"roomId": room_id, "max": 10},
    )
    if err:
        return None, err
    items = data.get("items", [])
    # items[0] is newest; guard against an empty list.
    return (items[0] if items else None), None


def webex_post_message(token: str, room_id: str,
                        text: str) -> Tuple[Optional[dict], Optional[str]]:
    """Post a plain-text message to a Webex room."""
    result, err = http_post_json(
        WEBEX_MESSAGES_URL,
        headers=_webex_headers(token),
        payload={"roomId": room_id, "text": text},
    )
    if err:
        log("ERR ", f"failed to post message to Webex: {err}")
    return result, err


# =========================================================================
# Model layer — ISS Location API (primary + automatic fallback)
# =========================================================================

def _epoch_to_utc_string(epoch_seconds: int) -> str:
    """
    Convert a Unix epoch timestamp (integer seconds since 1970-01-01 UTC)
    to a human-readable UTC string.

    We explicitly pass tz=timezone.utc so the conversion is always UTC
    regardless of the machine's local timezone.

    Example output: "Thu May 14 09:30:00 2026 UTC"
    """
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return dt.strftime("%a %b %d %H:%M:%S %Y UTC")


def get_iss_location() -> Tuple[Optional[float], Optional[float],
                                 Optional[str], Optional[str]]:
    """
    Fetch the current ISS position and return (latitude, longitude, utc_string, error).

    Tries open-notify.org first (the primary API referenced in the brief).
    If that fails or returns a malformed body, falls back to wheretheiss.at,
    which returns the same data in a slightly different shape.

    Returns (None, None, None, error_string) if both APIs fail.
    """
    # --- Primary: open-notify.org ---
    data, err = http_get(ISS_API_PRIMARY)
    if data and "iss_position" in data:
        try:
            lat = float(data["iss_position"]["latitude"])
            lon = float(data["iss_position"]["longitude"])
            # 'timestamp' is a Unix epoch integer in the open-notify response.
            ts  = int(data["timestamp"])
            return lat, lon, _epoch_to_utc_string(ts), None
        except (KeyError, ValueError, TypeError) as exc:
            log("WARN", f"open-notify response malformed ({exc}); trying fallback")

    # --- Fallback: wheretheiss.at ---
    data, err = http_get(ISS_API_FALLBACK)
    if data and "latitude" in data:
        try:
            lat = float(data["latitude"])
            lon = float(data["longitude"])
            # wheretheiss.at also provides a Unix epoch 'timestamp'.
            ts  = int(data["timestamp"])
            return lat, lon, _epoch_to_utc_string(ts), None
        except (KeyError, ValueError, TypeError) as exc:
            return None, None, None, f"fallback ISS API response malformed: {exc}"

    # Both failed — surface the most recent error.
    return None, None, None, err or "both ISS APIs are unreachable"


# =========================================================================
# Model layer — Mapbox Reverse Geocoding API
# =========================================================================

def reverse_geocode(lat: float, lon: float,
                    api_key: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Convert (lat, lon) to a human-readable place name via Mapbox.

    Returns (place_name, None) on success.
    Falls back to a coordinate string when Mapbox returns no features
    (e.g. ISS is over open ocean) — this is normal, not an error.
    Returns (None, error_string) on network/API failure.
    """
    url  = f"{MAPBOX_BASE}/{lon},{lat}.json"
    data, err = http_get(url, params={"access_token": api_key, "limit": 1})
    if err:
        return None, err

    features = data.get("features", [])
    if not features:
        # Open ocean — no named place; return coordinates as a fallback label.
        return f"open ocean near ({lat:.2f}°, {lon:.2f}°)", None

    place_name = features[0].get("place_name", "an unnamed location")
    return place_name, None


# =========================================================================
# Model layer — SpaceX Next Launch API (extended feature)
# =========================================================================

def next_spacex_launch() -> Tuple[Optional[dict], Optional[str]]:
    """
    Return a summary dict for the next scheduled SpaceX launch, or (None, error).

    Dict keys: name, date_utc, details, flight_number.
    """
    data, err = http_get(SPACEX_NEXT_LAUNCH)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "unexpected SpaceX API response shape"
    return {
        "name":          data.get("name", "unknown mission"),
        "date_utc":      data.get("date_utc", "unknown date"),
        "details":       data.get("details") or "no further details available",
        "flight_number": data.get("flight_number", "?"),
    }, None


# =========================================================================
# View layer — format bot replies
# =========================================================================

def format_iss_reply(lat: float, lon: float,
                     utc_string: str, place: str) -> str:
    """Build the Webex message text for a successful ISS location report."""
    return (
        f"🛰️  At {utc_string}, the ISS was passing over {place} "
        f"({lat:.4f}°, {lon:.4f}°)."
    )


def format_launch_reply(info: dict) -> str:
    """Build the Webex message text for a /launch query."""
    return (
        f"🚀  Next SpaceX launch — "
        f"flight {info['flight_number']}: {info['name']} "
        f"at {info['date_utc']}. {info['details']}"
    )


# Help text shown by /help — kept as a constant so it never drifts out of sync.
HELP_TEXT = (
    "Available commands:\n"
    f"  /N         — wait N seconds ({MIN_DELAY}–{MAX_DELAY}), then report the ISS position\n"
    "  /launch    — show the next scheduled SpaceX launch\n"
    "  /help      — show this help message"
)


# =========================================================================
# Controller — parse commands and route to the right model + view calls
# =========================================================================

def parse_delay_command(text: str) -> Optional[int]:
    """
    If text is exactly '/N' where N is an integer in [MIN_DELAY, MAX_DELAY],
    return N.  Return None for everything else.

    Examples:
        "/10"  → 10
        "/0"   → None  (below MIN_DELAY)
        "/abc" → None  (not a number)
        "10"   → None  (no leading slash)
    """
    if not text or not text.startswith("/"):
        return None
    body = text[1:].strip()
    if not body.isdigit():
        return None
    n = int(body)
    if n < MIN_DELAY or n > MAX_DELAY:
        return None
    return n


def handle_message(token: str, room_id: str,
                   text: str, geocode_key: str) -> None:
    """
    Controller entry point: receive a single message text, decide which
    command it represents, call the appropriate model functions, and post
    the formatted reply via the view helpers.
    """
    text = (text or "").strip()
    log("INFO", f"handling command: {text!r}")

    # --- /help (and common aliases) ---
    if text.lower() in ("/help", "/?", "/h"):
        webex_post_message(token, room_id, HELP_TEXT)
        return

    # --- /launch — extended feature (SpaceX next launch) ---
    if text.lower() == "/launch":
        info, err = next_spacex_launch()
        if err:
            log("ERR ", f"SpaceX API error: {err}")
            webex_post_message(token, room_id,
                               f"⚠️ Could not reach the SpaceX API: {err}")
            return
        webex_post_message(token, room_id, format_launch_reply(info))
        return

    # --- /N — wait N seconds then fetch ISS position ---
    delay = parse_delay_command(text)
    if delay is not None:
        # Acknowledge immediately so the user knows the command was received.
        webex_post_message(
            token, room_id,
            f"⏱ Waiting {delay} second{'s' if delay != 1 else ''} "
            "before checking the ISS position…",
        )
        time.sleep(delay)

        lat, lon, utc_string, err = get_iss_location()
        if err:
            log("ERR ", f"ISS API error: {err}")
            webex_post_message(token, room_id,
                               f"⚠️ Could not fetch the ISS location: {err}")
            return

        place, err = reverse_geocode(lat, lon, geocode_key)
        if err:
            # Geocoding failure is non-fatal — fall back to raw coordinates.
            log("WARN", f"reverse geocode failed ({err}); using coordinates as label")
            place = f"({lat:.4f}°, {lon:.4f}°)"

        webex_post_message(token, room_id,
                           format_iss_reply(lat, lon, utc_string, place))
        return

    # --- Unknown slash command ---
    # Only respond when the text looks like a slash command to avoid the bot
    # flooding the room with help text during normal conversation.
    if text.startswith("/"):
        webex_post_message(token, room_id,
                           f"❓ Unknown command.\n\n{HELP_TEXT}")


# =========================================================================
# Setup helpers — token acquisition and room selection
# =========================================================================

def acquire_webex_token() -> Optional[str]:
    """
    Obtain and validate a Webex personal access token.

    Checks the WEBEX_TOKEN environment variable first (the recommended path).
    Falls back to an interactive prompt if the variable is not set.
    Returns None if no valid token is provided after three attempts.
    """
    env_token = os.getenv("WEBEX_TOKEN", "").strip()
    if env_token:
        log("INFO", "using WEBEX_TOKEN from environment")
        return env_token

    log("WARN", "WEBEX_TOKEN not set — falling back to interactive prompt")
    print("Tip: create a .env file with WEBEX_TOKEN=<your_token> "
          "to skip this step.\n")

    for attempt in range(1, 4):
        token = input(f"Enter your Webex access token "
                      f"(attempt {attempt}/3, blank to cancel): ").strip()
        if not token:
            return None
        me, err = webex_me(token)
        if me is not None:
            email = (me.get("emails") or ["?"])[0]
            log("INFO", f"authenticated as {email}")
            return token
        log("ERR ", f"token rejected — {err}. Please try again.")

    return None


def acquire_geocode_key() -> Optional[str]:
    """
    Obtain a Mapbox access token from the environment or a one-shot prompt.
    Returns None if nothing is provided.
    """
    key = os.getenv("GEOCODE_API_KEY", "").strip()
    if key:
        return key

    log("WARN", "GEOCODE_API_KEY not set — prompting once")
    key = input("Enter your Mapbox access token (blank to cancel): ").strip()
    return key or None


def choose_room(token: str) -> Optional[Tuple[str, str]]:
    """
    Display the rooms accessible to the token and let the user pick one.
    Returns (room_id, room_title) on success, or None if cancelled.
    """
    for attempt in range(1, 4):
        rooms, err = webex_list_rooms(token)
        if err:
            log("ERR ", f"could not list rooms: {err}")
            return None

        if not rooms:
            log("ERR ", "this token has access to no Webex rooms.")
            return None

        print("\nAvailable Webex rooms:")
        for i, room in enumerate(rooms, start=1):
            rtype = room.get("type", "?")
            title = room.get("title", "(no title)")
            print(f"  {i:>3}.  [{rtype:>5}]  {title}")

        choice = input("\nEnter the room number to monitor "
                       "(or blank to cancel): ").strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(rooms):
            room = rooms[int(choice) - 1]
            return room["id"], room.get("title", "(no title)")

        log("WARN", f"invalid selection on attempt {attempt}/3 — "
                    "please enter a number from the list.")

    return None


# =========================================================================
# Main polling loop
# =========================================================================

def monitor_room(token: str, room_id: str,
                 bot_email: str, geocode_key: str) -> None:
    """
    Poll the Webex room every POLL_INTERVAL_S seconds and dispatch any new
    messages that were not sent by the bot itself.

    Duplicate-message prevention:
        On startup we record the ID of the most-recent message (last_seen_id).
        On each poll we only act when msg["id"] differs from last_seen_id,
        then immediately update last_seen_id.  This guarantees each message
        is handled exactly once, even if the poll fires multiple times during
        a slow command like /60.

    Consecutive-failure back-off:
        If Webex returns errors on MAX_CONSECUTIVE_FAILS polls in a row we
        sleep for 30 s before resuming, to avoid hammering a degraded API.
    """
    # Anchor: snapshot the latest message ID so we only act on messages that
    # arrive *after* the bot started.
    initial_msg, _ = webex_latest_message(token, room_id)
    last_seen_id    = initial_msg["id"] if initial_msg else None
    log("INFO", f"monitoring started — anchor message id: {last_seen_id}")
    log("INFO", "send /help in the Webex room to list commands. "
                "Press Ctrl+C here to stop.")

    consecutive_fails = 0

    while True:
        try:
            time.sleep(POLL_INTERVAL_S)

            msg, err = webex_latest_message(token, room_id)

            # --- Handle polling errors ---
            if err:
                consecutive_fails += 1
                log("WARN", f"poll error (fail {consecutive_fails}/{MAX_CONSECUTIVE_FAILS}): {err}")
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    log("ERR ", "too many consecutive poll failures — pausing 30 s")
                    time.sleep(30)
                    consecutive_fails = 0
                continue

            consecutive_fails = 0  # reset on a successful poll

            # --- No message at all, or same message as last poll ---
            if msg is None or msg.get("id") == last_seen_id:
                continue

            # --- (Self-message filter — disabled for personal-token use) ---
            # When the bot is run with a *personal* Webex access token (i.e.
            # the operator's own account, as in this assignment), every
            # /command the operator types in the room shows up to the bot
            # as a message from itself, and the filter below would silently
            # discard it.  The filter is therefore disabled by default.
            #
            # This is safe because:
            #   1. handle_message() only acts on text starting with "/".
            #   2. None of the bot's own replies start with "/" — they
            #      begin with 🛰  🚀  ⏱  ⚠  or ❓.
            #      So there is no risk of an infinite reply loop.
            #
            # In a production deployment using a *dedicated* Webex bot
            # account, uncomment the block below so the bot ignores the
            # bot account's own posts but still responds to real users.
            #
            # sender = (msg.get("personEmail") or "").lower()
            # if sender == bot_email.lower():
            #     last_seen_id = msg["id"]
            #     continue
            _ = bot_email  # retained in signature for future re-enable

            # --- New message from a real user: dispatch and record ---
            text = msg.get("text", "")
            last_seen_id = msg["id"]   # update BEFORE dispatching to avoid
                                       # re-processing if handle_message is slow
            handle_message(token, room_id, text, geocode_key)

        except KeyboardInterrupt:
            log("INFO", "shutdown requested — exiting.")
            return


# =========================================================================
# Entry point
# =========================================================================

def main() -> int:
    log("INFO", "Space Bot starting up.")

    token = acquire_webex_token()
    if not token:
        log("ERR ", "no Webex token provided — exiting.")
        return 1

    geocode_key = acquire_geocode_key()
    if not geocode_key:
        log("ERR ", "no Mapbox key provided — exiting.")
        return 1

    # Confirm bot identity (also validates the token is still live)
    me, err = webex_me(token)
    if err:
        log("ERR ", f"could not identify bot account: {err}")
        return 1
    bot_email = (me.get("emails") or [""])[0]
    log("INFO", f"bot identity confirmed: {bot_email}")

    selection = choose_room(token)
    if not selection:
        log("ERR ", "no room selected — exiting.")
        return 1
    room_id, room_title = selection
    log("INFO", f"monitoring room: {room_title!r}")

    monitor_room(token, room_id, bot_email, geocode_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
