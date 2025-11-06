"""
Space Bot Starter Script
Author: Aboubakar
Module: Web Technology
Description: Starter template for Space Bot assignment.
"""

import requests
import json
import time

GEOCODE_API_KEY = "pk.eyJ1IjoiYWIxN2hzIiwiYSI6ImNtaG5wZDV0ODAycGcya3FveDFiYnlpOHYifQ.EJ01LfNeCMM1xL4ZB2r4tQ"
WEBEX_HARDCODED_TOKEN = "M2YwOTZiOTItNzdjMy00MmJkLTlkYWEtOTJlMzQ1ZWY4ZTQ3OTA3NzA0YWMtM2Nh_P0A1_636b97a0-b0af-4297-b0e7-480dd517b3f9"

WEBEX_ROOMS_URL = "https://webexapis.com/v1/rooms"
WEBEX_MESSAGES_URL = "https://webexapis.com/v1/messages"
ISS_API_URL = "http://api.open-notify.org/iss-now.json"

def get_webex_token():
    choice = input("Do you wish to use the hard-coded Webex token? (y/n): ")
    if choice.lower() == "y":
        accessToken = WEBEX_HARDCODED_TOKEN
    else:
        accessToken = input("Enter your Webex access token: ")
    return accessToken

def test_mapbox():
    lat = 40.730610
    lon = -73.935242
    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{lon},{lat}.json?access_token={GEOCODE_API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        print("Mapbox API response:")
        print(json.dumps(data, indent=2))
    else:
        print("Error:", response.status_code)

def list_webex_rooms(token):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(WEBEX_ROOMS_URL, headers=headers)
    if response.status_code == 200:
        rooms = response.json().get("items", [])
        print("Webex Rooms:")
        for room in rooms:
            print(f"Type: {room.get('type')} | Name: {room.get('title')}")
        return rooms
    else:
        print("Error accessing Webex API:", response.status_code)
        return []

def get_iss_location():
    response = requests.get(ISS_API_URL)
    if response.status_code == 200:
        data = response.json()
        lat = data["iss_position"]["latitude"]
        lon = data["iss_position"]["longitude"]
        timestamp = data["timestamp"]
        readable_time = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(timestamp))
        return lat, lon, readable_time
    else:
        print("Error accessing ISS API:", response.status_code)
        return None, None, None

def get_geocode(lat, lon):
    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{lon},{lat}.json?access_token={GEOCODE_API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if data["features"]:
            place_name = data["features"][0]["place_name"]
            return place_name
        else:
            return "Unknown location"
    else:
        print("Error accessing Mapbox API:", response.status_code)
        return "Unknown location"

def monitor_webex_room(token, room_id):
    headers = {"Authorization": f"Bearer {token}"}
    last_message_id = None
    while True:
        response = requests.get(f"{WEBEX_MESSAGES_URL}?roomId={room_id}", headers=headers)
        if response.status_code == 200:
            messages = response.json().get("items", [])
            if messages:
                latest = messages[-1]
                if latest["id"] != last_message_id:
                    text = latest["text"]
                    print("Received message:", text)
                    if text.startswith("/") and text[1:].isdigit():
                        delay = int(text[1:])
                        print(f"Waiting {delay} seconds...")
                        time.sleep(delay)
                        lat, lon, readable_time = get_iss_location()
                        location_name = get_geocode(lat, lon)
                        message_text = f"On {readable_time}, the ISS is over {location_name} ({lat}, {lon})"
                        payload = {"roomId": room_id, "text": message_text}
                        post_response = requests.post(WEBEX_MESSAGES_URL, headers=headers, json=payload)
                        if post_response.status_code == 200:
                            print("Sent message:", message_text)
                        else:
                            print("Error sending message:", post_response.status_code)
                    last_message_id = latest["id"]
        else:
            print("Error accessing Webex Messages API:", response.status_code)
        time.sleep(1)

def main():
    token = get_webex_token()
    print("Access token received.\n")
    print("--- Testing Mapbox ---")
    test_mapbox()
    print("\n--- Testing Webex Rooms ---")
    rooms = list_webex_rooms(token)
    if rooms:
        room_name = input("\nEnter the exact Webex room name to monitor: ")
        selected_room = next((r for r in rooms if r["title"] == room_name), None)
        if selected_room:
            print(f"Monitoring room: {room_name}")
            monitor_webex_room(token, selected_room["id"])
        else:
            print("Room not found.")

if __name__ == "__main__":
    main()



