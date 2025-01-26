import requests
import json
import datetime
from datetime import date, datetime
from typing import Callable, Any, Set
from pathlib import Path
import io, os, base64
import sqlite3

from O365 import Account


# These are the user-defined functions that can be called by the agent.

def fetch_current_datetime() -> str:
    """
    Get the current time as a JSON string.

    :return: The current time in JSON format.
    :rtype: str
    """
    current_time = datetime.now()
    time_json = json.dumps({"current_time": current_time.strftime("%Y-%m-%d %H:%M:%S")})
    return time_json

def fetch_weather(latitude: float, longitude: float, date: str) -> dict:
    """
    Fetches the weather forecast for the specified latitude and longitude and date using the Open-Meteo API.

    :param latitude (float): The latitude to fetch weather for.
    :param longitude (float): The longitude to fetch weather for.
    :param date (str): The date to fetch weather for in YYYY-MM-DD format.
    :return: A JSON object with weather conditions, rain/snow info, and high/low temperatures.
    """
    # Fetch weather data
    weather_url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "snowfall_sum"],
        "timezone": "auto",
        "start_date": date,
        "end_date": date
    }
    response = requests.get(weather_url, params=params)
    weather_data = response.json()
    
    # Extract relevant weather details
    daily = weather_data.get("daily", {})
    if not daily:
        return {"error": f"Weather data not available for coordinates ({latitude}, {longitude}) on {date}."}
    
    temp_max = daily["temperature_2m_max"][0]
    temp_min = daily["temperature_2m_min"][0]
    precipitation = daily["precipitation_sum"][0]
    snowfall = daily["snowfall_sum"][0]
    
    rain = precipitation > 0
    snow = snowfall > 0
    
    result = {
        "latitude": latitude,
        "longitude": longitude,
        "date": date,
        "high_temp": temp_max,
        "low_temp": temp_min,
        "will_rain": rain,
        "will_snow": snow
    }
    
    return result

def store_person_info(person: str, information: str):
    """
    Stores information about a person in an SQLite database.
    :param person (str): The name of the person.
    :param information (str): A general information about the person, such as interest, preference or knowledge bit, in string format.
    """
    # Connect to SQLite database
    conn = sqlite3.connect("person_data.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS information (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT,
            information TEXT,
            date TEXT
        )
    """)

    # Get today's date
    today = date.today().strftime("%Y-%m-%d")

    # Insert information if provided
    if information:
        cursor.execute("INSERT INTO information (person, information, date) VALUES (?, ?, ?)", (person, information, today))

    # Commit changes and close the connection
    conn.commit()
    conn.close()

    print(f"Data successfully stored for {person} on {today}")


def retrieve_person_info(person: str) -> str:
    """
    Retrieves all information about a person from the SQLite database.
    :param person (str): The name of the person.
    :return: A JSON object containing information such as interests, preferences or knowledge bits about the person.
    """
    conn = sqlite3.connect("person_data.db")
    cursor = conn.cursor()

    cursor.execute("SELECT information, date FROM information WHERE person = ?", (person,))
    information = [{"information": row[0], "date": row[1]} for row in cursor.fetchall()]

    conn.close()

    result = {
        "person": person,
        "information": information
    }

    return json.dumps(result, indent=4)


def retrieve_person_events(startdate: str, enddate: str, person: str = None) -> str:
    """
    Retrieves events occuring between the start date and end date. 
    If no person is specified, all events of all persons are returned. 
    If a person is specified, only events categorized under that person's name are returned. 
    :param startdate (str): The earliest date to retrieve events, in YYYY-MM-DD format.
    :param enddate (str): The latest date to retrieve events, in YYYY-MM-DD format.
    :param person (str, optional): The firstname of the person having the event.
    :return: A JSON object containing information of the events.
    """

    MS365_CLIENT_ID = os.environ.get("MS365_CLIENT_ID")
    MS365_SECRET = os.environ.get("MS365_SECRET")
    MS365_TENANT_ID = os.environ.get("MS365_TENANT_ID")

    credentials = (MS365_CLIENT_ID, MS365_SECRET)
    scopes = ['Calendars.ReadWrite']
    account = Account(credentials, tenant_id=MS365_TENANT_ID)

    if not account.is_authenticated:
        account.authenticate(scopes=scopes)

    schedule = account.schedule()
    calendar = schedule.get_default_calendar()

    # Convert input date strings to datetime objects
    start_date = datetime.strptime(startdate, "%Y-%m-%d")
    end_date = datetime.strptime(enddate, "%Y-%m-%d")

    q = calendar.new_query('start').greater_equal(start_date)
    q.chain('and').on_attribute('end').less_equal(end_date)
    # q.chain('and').any(collection='categories', operation='eq', word='Private')

    events = calendar.get_events(query=q, include_recurring=True)

    event_list = []

    for event in events:
        if "Private" in event.categories:
            if person is None or person in event.categories or person.lower() in event.subject.lower():
                event_list.append({
                    'subject': event.subject,
                    'start': event.start.strftime("%Y-%m-%d %H:%M:%S"),
                    'end': event.end.strftime("%Y-%m-%d %H:%M:%S"),
                    'location': event.location["displayName"] if event.location["displayName"] else "No location",
                })

    return json.dumps(event_list, indent=4)


def get_music_players() -> str:
    """
    Retrieves all music players in the house.
    :return: An array containing all players, their areas and their status.
    :rtype: str
    """

    return json.dumps([{"id":"living_room","area":"Living room","status":"playing"},{"id":"kitchen_speaker","area":"Kitchen","status":"off"}])


def play_radio(url: str, player: str):
    """
    Plays a radio from a URL in a music player in the house.
    :param url (str): The URL of the radio to play.
    :param player (str): The ID of the player where to play the radio.
    :return: Confirmation message.
    :rtype: str
    """
    return "Radio playing"


# Statically defined user functions for fast reference
user_functions: Set[Callable[..., Any]] = {
    fetch_current_datetime,
    fetch_weather,
    store_person_info,
    retrieve_person_info,
    get_music_players,
    play_radio,
    retrieve_person_events
}


