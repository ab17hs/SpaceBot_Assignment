# Space Bot

A Python chatbot that watches a Cisco Webex space and, on a `/N` command,
waits *N* seconds, fetches the current position of the International Space
Station, reverse-geocodes the coordinates to a human-readable place, and
posts the result back to the room. A `/launch` command extends the bot to
report the next scheduled SpaceX launch.

| | |
|---|---|
| **Student** | Aboubakar Hameed Sultan |
| **Student ID** | 23059674 |
| **Module** | Web Technology (5FTC2167) |
| **Repository** | https://github.com/ab17hs/SpaceBot_Assignment |

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [File Structure](#2-file-structure)
3. [Installation and Run Instructions](#3-installation-and-run-instructions)
4. [API Investigation](#4-api-investigation)
   - [4.1 Webex Messaging API](#41-webex-messaging-api)
   - [4.2 ISS Current Location API](#42-iss-current-location-api)
   - [4.3 Mapbox Geocoding API](#43-mapbox-geocoding-api)
   - [4.4 Epoch-to-Human Time Conversion](#44-epoch-to-human-time-conversion)
5. [Web Architecture](#5-web-architecture)
6. [MVC Design Pattern](#6-mvc-design-pattern)
7. [Extended Features](#7-extended-features)
8. [Security Considerations](#8-security-considerations)
9. [Error Handling Strategy](#9-error-handling-strategy)
10. [Testing](#10-testing)
11. [References](#11-references)

---

## 1. Project Overview

The brief asks for a Python program that connects to the Webex Messaging API,
listens for a slash command in a chosen room, queries the ISS Current Location
API after the requested delay, converts the resulting latitude/longitude to a
named place using a geocoding API, and posts the formatted result back to the
same Webex room. Space Bot implements all of that and adds three extensions:

* an alternative ISS endpoint as a fallback when `open-notify.org` is down;
* a `/launch` command that reports the next SpaceX launch via the
  `spacexdata.com` API;
* environment-variable-based secrets handling with a `.env` template and a
  `.gitignore` that excludes the real `.env` file from version control.

The technology stack is intentionally minimal: a single Python script
(`space_bot.py`), the `requests` library for all HTTP, and the optional
`python-dotenv` library to load secrets from a local `.env` file.

## 2. File Structure

```
SpaceBot/
├── space_bot.py        # the bot
├── requirements.txt    # Python dependencies
├── .env.example        # template for the two required environment variables
├── .gitignore          # excludes .env, venv, __pycache__
└── README.md           # this file
```

## 3. Installation and Run Instructions

```bash
git clone https://github.com/ab17hs/SpaceBot_Assignment.git
cd SpaceBot_Assignment

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS/Linux

pip install -r requirements.txt

# One-off: copy the template and add your real keys
copy .env.example .env          # Windows
cp .env.example .env            # macOS/Linux
# then edit .env in a text editor and paste in real keys

python space_bot.py
```

At first run the bot will:

1. Read `WEBEX_TOKEN` and `GEOCODE_API_KEY` from the environment
   (or prompt for them, with up to three retries each).
2. Call `GET /people/me` on Webex to confirm the token is valid and to
   capture the bot's own identity (so it ignores its own messages later).
3. List the Webex rooms the token can see and ask which one to monitor.
4. Anchor on the latest message currently in the room, so it responds only
   to messages received *after* startup.
5. Poll every 3 seconds for new messages and dispatch any `/command`
   it receives.

## 4. API Investigation

This section is the substantive investigation of every API the program uses.
Each subsection covers what the API does, why it was chosen over the
alternatives, how it is authenticated, the specific endpoints and methods
used, the shape of a typical response, rate limits, and the failure modes
the code must handle.

### 4.1 Webex Messaging API

**Provider.** Cisco Webex Developer Platform — `https://webexapis.com/v1/`.

**Why it was chosen.** The brief mandates Webex as the chat surface. Cisco
also publishes Webex APIs for meetings, contact-centre, and admin
administration; the Messaging API is the only one of those that fits the
brief's "post a message into a room" requirement (Cisco, 2024).

**Authentication.** OAuth 2.0 bearer tokens, sent in an `Authorization`
header on every request:

```
Authorization: Bearer <WEBEX_ACCESS_TOKEN>
Content-Type: application/json
```

Two token types exist: **personal access tokens** (12-hour lifetime, fine for
this assignment) and **bot tokens** (don't expire, the correct production
choice). The submission uses a personal access token loaded from the
`WEBEX_TOKEN` environment variable — *never* hardcoded in source.

**Endpoints used by the bot.**

| Endpoint | Method | Used for |
|---|---|---|
| `/people/me` | `GET` | Verify the token at start-up and capture the bot's own email so it can ignore its own messages in the polling loop. |
| `/rooms` | `GET` | List rooms the token can see, so the user can pick one. |
| `/messages?roomId={id}&max=1` | `GET` | Fetch the single most recent message in the chosen room each polling cycle. |
| `/messages` | `POST` | Send a reply (`/launch` result, ISS report, help text, error notice) into the room. |

**Crucial detail — message ordering.** Webex returns messages **newest-first**.
The first version of this project read `items[-1]` (last in the list), which is
actually the *oldest* message and caused the bot to never see new commands.
Fixed by asking for `?max=1` and reading `items[0]`.

**Example request and response.**

```http
GET https://webexapis.com/v1/messages?roomId=Y2lz...AAA&max=1
Authorization: Bearer M2Y...

200 OK
Content-Type: application/json

{
  "items": [
    {
      "id": "Y2lz...MSG1",
      "roomId": "Y2lz...AAA",
      "roomType": "group",
      "text": "/5",
      "personEmail": "user@example.com",
      "created": "2026-05-11T14:02:11.000Z"
    }
  ]
}
```

**Rate limits.** Webex enforces 300 requests per minute per token. The bot's
3-second polling interval (~20 req/min) sits well inside that ceiling and
the consecutive-failure back-off in the polling loop further protects
against accidental bursts (Cisco, 2024).

**Error responses worth handling explicitly.**

| Code | Meaning | Bot's response |
|---|---|---|
| `400` | Malformed request — usually a missing field | Log, do not retry, do not crash |
| `401` | Token invalid / expired | Prompt user for a new token (in `acquire_webex_token`) |
| `403` | Token valid but lacks scope for this resource | Log, fall back to a different action |
| `404` | Room not found / deleted | Surface to user, ask for a different room |
| `429` | Rate-limit hit | Honour the `Retry-After` header, back off |
| `5xx` | Webex outage | Count consecutive failures, back off 30 s after 5 in a row |

### 4.2 ISS Current Location API

**Primary provider.** Open Notify — `http://api.open-notify.org/iss-now.json`.

**Why it was chosen.** Open Notify is the canonical, no-auth, free
JSON endpoint for the ISS's current geocentric position, maintained by NASA
engineer Nathan Bergey since 2011 (Bergey, 2011). It returns a tiny payload
and requires no API key, which keeps the dependency surface small.

**Endpoint.** A single resource: `GET /iss-now.json`. No parameters.

**Coordinate system.** The latitude and longitude are returned as strings in
**WGS-84 decimal degrees**, the same coordinate system used by GPS and by
Mapbox. Latitude is in the range -90 to +90, longitude -180 to +180. The
bot converts both to `float` before passing them to Mapbox.

**Example response.**

```json
{
  "message": "success",
  "timestamp": 1763222770,
  "iss_position": {
    "latitude": "50.8675",
    "longitude": "85.5124"
  }
}
```

**Update frequency.** The ISS completes one orbit every ~92 minutes,
travelling at ~7.66 km/s. The position changes substantially second to
second, so the bot fetches a fresh reading at the moment the user requests
it rather than caching.

**Why a fallback was added.** `open-notify.org` is a hobbyist deployment
and has been intermittently unavailable since 2023. When it returns no
data or a non-JSON body, the bot falls back to
`https://api.wheretheiss.at/v1/satellites/25544`, an alternative free service
(satellite NORAD ID 25544 is the ISS). The fallback returns the same
information in a slightly different schema, which `get_iss_location` handles
transparently.

### 4.3 Mapbox Geocoding API

**Provider.** Mapbox — `https://api.mapbox.com/geocoding/v5/mapbox.places/`.

**What "reverse geocoding" is.** Reverse geocoding takes a coordinate pair
and returns the human-readable place name that contains it: a town, country,
ocean, region. *Forward* geocoding does the opposite (place name → coords).
The bot only ever needs reverse, because the input is the ISS's lat/lon.

**Why Mapbox and not an alternative.** Three free reverse-geocoders were
considered:

| Provider | Free tier | Reverse accuracy | Verdict |
|---|---|---|---|
| **Mapbox Places** | 100,000 req/month | High | **Chosen** — generous free tier, easy auth, returns clean `place_name` strings (Mapbox, 2024). |
| Google Geocoding | $200 monthly credit | Very high | Requires a billing account even on the free tier — a barrier for a student project. |
| OpenStreetMap Nominatim | Unlimited but rate-limited to 1 req/s | Medium; gaps over oceans | Heavy throttling makes it unsuitable for an interactive demo. |

**Authentication.** Mapbox accepts the access token as a query-string
parameter (`?access_token=...`), not an `Authorization` header. The token is
loaded from the `GEOCODE_API_KEY` environment variable.

**Endpoint format — a coordinate-order trap.** Mapbox uses **`{longitude},{latitude}`**
in the URL, which is the opposite order from how most APIs and humans write
coordinates ("lat, lon"). The bot's `reverse_geocode` function takes
arguments in the conventional `(lat, lon)` order and re-orders them in the URL
string to avoid silent mistakes elsewhere in the code.

**Example request and response.**

```
GET https://api.mapbox.com/geocoding/v5/mapbox.places/85.5124,50.8675.json?access_token=pk....&limit=1

200 OK
{
  "type": "FeatureCollection",
  "features": [
    {
      "place_name": "Onguday, Russia",
      "place_type": ["place"],
      "center": [85.5124, 50.8675]
    }
  ]
}
```

**Edge cases.** The ISS spends ~71 % of its time over open ocean, where
Mapbox returns `features: []`. The bot detects this and substitutes a
synthesised string like `"open ocean near (50.87°, 85.51°)"` so the reply
is still useful.

### 4.4 Epoch-to-Human Time Conversion

**What Unix epoch time is.** A single integer counting seconds since
1970-01-01 T 00:00:00 **UTC**, with no time-zone information attached. Almost
every JSON API on the web — Webex, ISS, SpaceX — returns timestamps in this
format because integers are easier to transport, compare and sort than
formatted date strings.

**The ISS API's timestamp.** The `timestamp` field in the ISS response is
the moment the position reading was taken, in Unix epoch seconds. To present
it to a Webex user it must be converted to a human-readable string.

**Python approaches considered.**

```python
import time
human = time.ctime(1763222770)         # → 'Sun Nov 16 04:46:10 2025' (LOCAL time)

from datetime import datetime, timezone
human = datetime.fromtimestamp(1763222770, tz=timezone.utc) \
                .strftime("%a %b %d %H:%M:%S %Y UTC")
                                       # → 'Sun Nov 16 04:46:10 2025 UTC'
```

The first form (`time.ctime`) is shorter but uses the *local* time-zone of
the machine the bot is running on, which produced confusing output when
testing across different hosts. The bot uses the second form — explicit
UTC with a fixed format string — so the reported time means the same thing
regardless of where the bot runs.

**Why this matters here specifically.** When the previous version of the
bot showed the tutor a timestamp of `Sun Nov 23 11:06:10 2025`, two
contributing factors were at play: (1) the bot was reading the *oldest*
message in the room rather than the newest, and (2) `time.ctime` was
rendering the epoch in the tutor's local time, with no `UTC` marker, making
it look even more wrong. Both have been fixed.

---

## 5. Web Architecture

### 5.1 Client–server model

```
   ┌──────────────┐   message "/5"   ┌─────────────────┐
   │  Webex user  │ ───────────────► │  Webex cloud    │
   └──────────────┘ ◄─────────────── │  (Cisco-hosted) │
                       ISS reply     └─────────────────┘
                                            ▲ ▼
                                  HTTPS poll │ │ HTTPS POST
                                            │ ▼
                                       ┌──────────────┐
                                       │  Space Bot   │
                                       │ (Python on   │
                                       │  your laptop)│
                                       └──────┬───────┘
                                              │ HTTPS GET
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                     ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
                     │ ISS API     │  │  Mapbox     │  │  SpaceX     │
                     │ (no auth)   │  │  Geocoding  │  │  Data       │
                     └─────────────┘  └─────────────┘  └─────────────┘
```

The bot is **both a client (of the four upstream APIs) and a server (of the
Webex room)**. It polls Webex on a 3-second interval — Webex also supports
webhooks for a true push-based model, but webhooks require a publicly
routable HTTPS endpoint, which is out of scope for a script running on a
student laptop.

### 5.2 REST principles applied

* **Resource-oriented URLs.** Each endpoint identifies a resource: a room
  (`/rooms/{id}`), a message (`/messages/{id}`), a coordinate
  (`/geocoding/v5/.../{lon},{lat}.json`). Verbs come from the HTTP method.
* **Statelessness.** No session cookies or server-side context. Every
  request carries the bearer token that authorises it (Fielding, 2000).
* **Standard HTTP methods.** `GET` to read, `POST` to create. The bot does
  not delete or modify messages, so `DELETE`/`PUT` aren't used.
* **JSON over HTTPS.** Every response is JSON, every connection is TLS-
  protected.

### 5.3 Why this architecture suits the brief

The brief asks for a Python program that reacts to text commands and posts
back. A client-side polling architecture is the simplest viable design:

* it requires no inbound network configuration (no firewall holes);
* it works behind NAT and on a typical Wi-Fi network;
* failures are local — if the laptop goes offline the bot stops, no
  half-deployed state is left behind.

In production this design would be replaced by a Webex webhook calling a
Flask endpoint hosted on a cloud provider, eliminating the polling cost.
That extension is documented in the Reflection section but not implemented.

---

## 6. MVC Design Pattern

### 6.1 The pattern

Model-View-Controller (Krasner and Pope, 1988) is a separation of
responsibilities first formalised in Smalltalk:

* **Model** — knows about the data: where it lives, how to fetch it, how it
  is structured. Does *not* know how it is presented.
* **View** — knows how to render the data for the user. Does *not* know
  where the data came from.
* **Controller** — interprets user input, asks the model for what it
  needs, hands the result to the view, and sends the rendered output
  somewhere it will be seen.

### 6.2 Mapping the pattern onto `space_bot.py`

| Layer | Responsible for | Functions in the code |
|---|---|---|
| **Model** | Talking to remote APIs and returning structured data | `http_get`, `http_post_json`, `webex_me`, `webex_list_rooms`, `webex_latest_message`, `webex_post_message`, `get_iss_location`, `reverse_geocode`, `next_spacex_launch` |
| **Controller** | Interpreting a Webex message, deciding what to do, coordinating the model and view | `handle_message`, `parse_delay_command`, `monitor_room`, `acquire_webex_token`, `choose_room`, `main` |
| **View** | Producing the strings the user (or operator) actually sees | `format_iss_reply`, `format_launch_reply`, `HELP_TEXT`, `log` |

Every function lives in exactly one layer, and the layers communicate only
through return values — the view never calls the model, the model never
formats output. That makes each layer independently testable: you could
swap the Webex client for a Slack client by replacing the four
`webex_*` functions without touching the controller or view.

### 6.3 MVC versus alternatives considered

* **MVP (Model-View-Presenter).** The presenter typically owns the view's
  state, which is overkill for a console bot with no GUI.
* **MVVM (Model-View-ViewModel).** Designed for two-way data binding in
  UI frameworks; meaningless without a UI.

Plain MVC was the right fit because the "view" is just a string of text,
and the controller's job is simple message dispatch — exactly what MVC was
originally designed for.

### 6.4 Limitations

MVC for a one-file 350-line console bot is arguably over-engineered. The
benefit appeared during the rewrite, when the `messages[-1]` bug was traced
to a single model function (`webex_latest_message`) without having to read
controller or view code at all. The cost was a slightly longer file than
the original.

---

## 7. Extended Features

1. **SpaceX `/launch` command.** Calls `https://api.spacexdata.com/v5/launches/next`
   and posts the next mission's name, flight number, scheduled UTC date,
   and a short description into the room. SpaceX-Data is a free, no-auth
   community API maintained by r-spacex (r-SpaceX, 2024).
2. **ISS API fallback.** When `api.open-notify.org` is unreachable the bot
   silently falls back to `wheretheiss.at`, normalising the schema.
3. **`/help` command.** Tells the user what commands exist, surfaced
   directly inside the chat room.
4. **Resilience back-off.** Five consecutive polling failures trigger a
   30-second back-off so transient outages don't burn CPU or rate limits.
5. **Self-message filter.** The bot reads its own identity from `/people/me`
   on start-up and ignores any message whose `personEmail` matches, so the
   bot can never trigger itself.

---

## 8. Security Considerations

* **Secrets out of source.** `WEBEX_TOKEN` and `GEOCODE_API_KEY` are loaded
  from environment variables (or a local `.env` file via `python-dotenv`).
  Both files are excluded from Git by `.gitignore`. A `.env.example`
  template is committed so anyone cloning the repo knows which variables
  to set.
* **Token rotation.** The previous version of this code committed a Webex
  token to GitHub. That token has been revoked and a new one issued for
  this resubmission. GitHub's secret-scanning would normally flag and
  invalidate it automatically (GitHub, 2024).
* **Bearer-token best practice.** The token is sent only over HTTPS
  (`https://webexapis.com/v1`), never written to logs, and never echoed
  to the console (the prompt for it is a normal `input()` rather than a
  password input — a known weakness of `input()` is that it is
  shoulder-surfable; in a production deployment `getpass.getpass()` would
  be used).
* **Input validation.** `parse_delay_command` rejects negative numbers,
  zero, numbers above 60, and any non-digit input, eliminating the
  arbitrary-`time.sleep` denial-of-service path that the original code
  contained.
* **No PII storage.** The bot never persists any user identifier or message
  content to disk.

---

## 9. Error Handling Strategy

Every outbound HTTP call routes through `http_get` or `http_post_json`,
which return a `(data, error)` tuple. Callers check the error and decide
what to do — log it, retry, surface it to the user, or back off:

```python
data, err = http_get(url)
if err:
    log("ERR ", f"could not reach API: {err}")
    return None
```

The categories caught are:

* `requests.exceptions.Timeout`
* `requests.exceptions.ConnectionError`
* `requests.exceptions.RequestException` (catch-all)
* non-2xx HTTP status codes
* `json.JSONDecodeError` (non-JSON body)
* `KeyError` / `ValueError` / `TypeError` inside response-parsing blocks
  (handled in `get_iss_location` and `next_spacex_launch`)

The polling loop additionally tracks consecutive failures and backs off
30 seconds after five in a row, then resets the counter.

---

## 10. Testing

The bot was tested manually against five user-visible scenarios.

| ID | Scenario | Expected | Result |
|---|---|---|---|
| T1 | Start with valid token + valid room, type `/5` | Bot posts "Waiting 5 seconds…" then an ISS report within ~5 s | ✅ |
| T2 | Type `/0` | Treated as unknown command, no `sleep(0)` | ✅ |
| T3 | Type `/9999` | Rejected (above `MAX_DELAY`), no long sleep | ✅ |
| T4 | Type `/launch` | SpaceX next-launch summary posted | ✅ |
| T5 | Type `/help` | Help text posted | ✅ |
| T6 | Start with an invalid token | Prompts up to 3 times then exits cleanly | ✅ |
| T7 | Pick an invalid room number | Re-prompts up to 3 times | ✅ |
| T8 | Disconnect Wi-Fi mid-poll | Polling errors logged, back-off after 5 fails, recovers on reconnect | ✅ |
| T9 | Send a message containing no text (file upload) | Bot does not crash; ignored | ✅ |
| T10 | Send a message *from the bot's own account* | Bot ignores it (self-message filter) | ✅ |

---

## 11. References

Bergey, N. (2011) *Open Notify — ISS Current Location*. Available at:
http://open-notify.org/Open-Notify-API/ISS-Location-Now/
(Accessed: 10 May 2026).

Cisco (2024) *Webex for Developers — Messaging API Reference*. Available at:
https://developer.webex.com/docs/api/v1/messages
(Accessed: 10 May 2026).

Fielding, R.T. (2000) *Architectural Styles and the Design of Network-based
Software Architectures*. PhD thesis. University of California, Irvine.

GitHub (2024) *Secret scanning for partner patterns*. Available at:
https://docs.github.com/en/code-security/secret-scanning/about-secret-scanning
(Accessed: 10 May 2026).

Krasner, G.E. and Pope, S.T. (1988) 'A cookbook for using the
model-view-controller user interface paradigm in Smalltalk-80',
*Journal of Object-Oriented Programming*, 1(3), pp. 26–49.

Mapbox (2024) *Geocoding API documentation*. Available at:
https://docs.mapbox.com/api/search/geocoding/
(Accessed: 10 May 2026).

Open Notify (2024) *ISS Current Location*. Available at:
http://api.open-notify.org/iss-now.json
(Accessed: 10 May 2026).

r-SpaceX (2024) *SpaceX-Data — a free, open-source REST API for SpaceX data*.
Available at: https://github.com/r-spacex/SpaceX-API
(Accessed: 10 May 2026).

Where The ISS At? (2024) *ISS tracking API*. Available at:
https://wheretheiss.at/w/developer
(Accessed: 10 May 2026).
