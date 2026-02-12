# Space Bot Assignment

# Student
Aboubakar

# Module
Web Technology

# Description
This Python Space Bot monitors a Webex Space for messages starting with `/seconds` and responds with the ISS current location using the ISS API and Mapbox Geocoding API.

# Instructions
1. Run `space_iss.py` in Python 3.14+
2. Enter Webex token (or use hard-coded)
3. Select a Webex Space to monitor
4. Post messages like `/5` to get ISS location after 5 seconds

# APIs Used
- Webex Messaging API
- ISS Current Location API
- Mapbox Geocoding API

# Video Demonstration
- Record a 3-minute screen capture showing:
  - Running the bot
  - Posting `/seconds` messages
  - Bot replying with ISS location
  - Explanation of API choices and security considerations

  SECTION 1 – WEBEX MESSAGING API

API Base URL:
https://webexapis.com/v1

Authentication Method:
Bearer Token using Authorization header

Endpoints Used:
/rooms
/messages

HTTP Methods:
GET, POST

Required Headers:
Authorization: Bearer <WEBEX_ACCESS_TOKEN>
Content-Type: application/json

Example Request:
GET https://webexapis.com/v1/messages?roomId=ROOM_ID


SECTION 2 – ISS CURRENT LOCATION API

API Base URL:
http://api.open-notify.org

Endpoint:
/iss-now.json

Example Response:
{
  "message": "success",
  "timestamp": 1730918742,
  "iss_position": {
    "latitude": "40.73061",
    "longitude": "-73.935242"
  }
}

SECTION 3 – GEOCODING API (MAPBOX)

API Provider:
Mapbox

API Base URL:
https://api.mapbox.com/geocoding/v5/mapbox.places

Endpoint Format:
{longitude},{latitude}.json

Authentication Method:
Access token passed as query parameter

Example Request:
https://api.mapbox.com/geocoding/v5/mapbox.places/-73.935242,40.73061.json?access_token=MAPBOX_TOKEN

Example Response:
{
  "features": [
    {
      "place_name": "New York, United States"
    }
  ]
}

SECTION 4 – TIME CONVERSION

Python Module Used:
time

Function Used:
time.ctime()

Example Code:
import time
human_time = time.ctime(1730918742)

Example Output:
Wed Nov 06 17:45:42 2025

SECTION 5 – WEB ARCHITECTURE AND MVC

Web Architecture:
Client–Server Architecture

Explanation:
The Webex application acts as the client. The Space Bot is the server. The client sends messages through the Webex API, the Space Bot processes the request, retrieves ISS data from external APIs, and sends the response back to the client.

REST Principles:
Uses HTTP GET and POST methods, stateless communication, and JSON formatted responses.

MVC Pattern:

Model:
ISS location data retrieved from the ISS API and Mapbox API

View:
Messages displayed in the Webex Space

Controller:
Python code that handles user commands, API requests, and message responses

