import requests
import asyncio
import json
import datetime
from datetime import date, datetime
from typing import Callable, Any, Set
import os
import sqlite3
from typing import Any, List

from O365 import Account
from music_assistant_client.client import MusicAssistantClient
from music_assistant_models.enums import MediaType

import logging

logger = logging.getLogger("user_functions")
global_music_client = None
global_o365_account = None


def set_music_client(client: MusicAssistantClient):
    """
    Sets the global music client object.
    :param client: The MusicAssistantClient instance to set.
    """
    global global_music_client
    global_music_client = client


def set_o365_account(account: Account):
    """
    Sets the global music client object.
    :param client: The MusicAssistantClient instance to set.
    """
    global global_o365_account
    global_o365_account = account


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


def retrieve_person_events(startdate: str, enddate: str, persons: List[str]) -> str:
    """
    Retrieves events occuring between the start date and end date for the persons specified. 
    :param startdate (str): The earliest date to retrieve events, in YYYY-MM-DD format.
    :param enddate (str): The latest date to retrieve events, in YYYY-MM-DD format.
    :param persons (List[str]): A list of firstname of the persons for which to retrieve events.
    :return: A JSON object containing a dictionary of events for each of the persons specified and information of the events, and events related to all persons in the household.
    """

    global global_o365_account  # Ensure we reference the global variable
    if global_o365_account is None:
        return json.dumps({"error": "o365 account not set"})
    
    schedule = global_o365_account.schedule()
    calendar = schedule.get_default_calendar()

    # Convert input date strings to datetime objects
    start_date = datetime.strptime(startdate, "%Y-%m-%d")
    end_date = datetime.strptime(enddate, "%Y-%m-%d")

    q = calendar.new_query('start').greater_equal(start_date)
    q.chain('and').on_attribute('end').less_equal(end_date)

    events = calendar.get_events(query=q, include_recurring=True)

    categorized_events = {person: [] for person in persons}
    categorized_events["All persons"] = []
    
    for event in events:
        if "Private" in event.categories:
            event_details = {
                'subject': event.subject,
                'start': event.start.strftime("%Y-%m-%d %I:%M:%S %p"),
                'end': event.end.strftime("%Y-%m-%d %I:%M:%S %p"),
                'location': event.location["displayName"] if event.location and event.location.get("displayName") else "No location",
            }
        
            matched = False
            for person in persons:
                if person.lower() in event.subject.lower():
                    categorized_events[person].append(event_details)
                    matched = True
            
            if not matched:
                categorized_events["All persons"].append(event_details)
    
    return json.dumps(categorized_events, indent=4)

def get_music_player_queues() -> str:
    """
    Retrieves all music player in the house.
    :return: An array containing all player queues and their details, including id and status.
    :rtype: str
    """

    global global_music_client  # Ensure we reference the global variable
    if global_music_client is None:
        return json.dumps({"error": "Music client not set"})

    # Get the player queues
    player_queues = global_music_client.player_queues

    # Convert results to a list of dictionaries
    player_queues_list = [
        {
            "id": player_queue.queue_id,  # Assuming radio object has an item_id attribute
            "name": player_queue.display_name,
            "state": player_queue.state,  # Assuming radio object has a provider attribute
        }
        for player_queue in player_queues
    ]
    
    return json.dumps({"player_queues": player_queues_list})


async def search_radios(radio_name: str) -> str:
    """
    Searches for radios to play in the house.
    :param radio_name (str): The name of the radio to search.
    :return: Radios found.
    :rtype: str
    """

    global global_music_client  # Ensure we reference the global variable
    if global_music_client is None:
        return json.dumps({"error": "Music client not set"})

    # Get the players
    search_readio_task = asyncio.create_task(global_music_client.music.get_library_radios(search=radio_name, limit=3))
    radios_results = await search_readio_task
    
    # Convert results to a list of dictionaries
    radios_list = [
        {
            "item_id": radio.item_id,
            "name": radio.name,
            "uri": radio.uri,
        }
        for radio in radios_results
    ]
    
    return json.dumps({"radios": radios_list})



async def search_albums(album_name: str, artist_name:str = None) -> str:
    """
    Search for albums to play in the house, returns the songs of the album found.
    :param album_name (str): The name of the album to search.
    :param artist_name (str): The name of the artist to search an album for (Optional).
    :return: Tracks of the album found.
    :rtype: str
    """

    global global_music_client  # Ensure we reference the global variable
    if global_music_client is None:
        return json.dumps({"error": "Music client not set"})

    # Search for albums
    search_album_task = asyncio.create_task(global_music_client.music.search(
        search_query=album_name,
        media_types=[MediaType.ALBUM],
    ))

    album_results = await search_album_task
    
    if not album_results.albums:
        return json.dumps({"error": "No albums found"})
    
    album = album_results.albums[0]  # Assuming the first result is the most relevant

    # Search for album tracks
    album_tracks_task = asyncio.create_task( global_music_client.music.get_album_tracks(
        item_id=album.item_id,
        provider_instance_id_or_domain=album.provider,
    ))

    album_tracks = await album_tracks_task

    album_tracks_list = [
        {
            "item_id": track.item_id, 
            "name": track.name,
            "uri": track.uri,  
        }
        for track in album_tracks
    ]

    return json.dumps({"album": album.name, "tracks": album_tracks_list})



async def search_artist(artist_name: str) -> str:
    """
    Searches for an artist to play in the house, returns the most popular songs of the artist found.
    :param artist_name (str): The name of the artist to search.
    :return: Most popular tracks of the artist.
    :rtype: str
    """

    global global_music_client  # Ensure we reference the global variable
    if global_music_client is None:
        return json.dumps({"error": "Music client not set"})

    search_artist_task = asyncio.create_task(global_music_client.music.search(
        search_query=artist_name,
        media_types=[MediaType.ARTIST],
    ))
    
    artist_results = await search_artist_task

    if not artist_results.artists:
        return json.dumps({"error": "No artists found"})
    
    artist = artist_results.artists[0]  # Assuming the first result is the most relevant
    top_tracks = await global_music_client.music.get_artist_tracks(
        item_id=artist.item_id,
        provider_instance_id_or_domain=artist.provider,
    )
    
    top_tracks_list = [
        {
            "item_id": track.item_id, 
            "name": track.name,
            "uri": track.uri,  
        }
        for track in top_tracks
    ]

    return json.dumps({"artist": artist.name, "top_tracks": top_tracks_list})


async def search_song(song_name: str, artist_name: str = None) -> str:
    """
    Searches for a song to play in the house, returns the song.
    :param search_name (str): The name of the song to search for.
    :param artist_name (str): The name of the artist to search a song for (Optional).  
    :return: Track information.
    :rtype: str
    """

    global global_music_client  # Ensure we reference the global variable
    if global_music_client is None:
        return json.dumps({"error": "Music client not set"})

    search_song_task = asyncio.create_task(global_music_client.music.search(
        search_query=song_name,
        media_types=[MediaType.TRACK],
    ))

    results = await search_song_task
    
    if not results.tracks:
        return json.dumps({"error": "No songs found"})
    
    song = results.tracks[0]  # Assuming the first result is the most relevant
    
    return json.dumps({"song": song.name, "uri" : song.uri, "artist": song.artists[0].name if song.artists else "Unknown"})


async def play_media(uri: str, player_queue_id: str):
    """
    Plays a media from a URI in a music player in the house.
    :param uri (str): The URI of the media to play.
    :param player_queue_id (str): The ID of the player queue of the player where to play the media.
    :return: Confirmation message.
    :rtype: str
    """

    global global_music_client  # Ensure we reference the global variable
    if global_music_client is None:
        return json.dumps({"error": "Music client not set"})

    player_queues = global_music_client.player_queues
    play_music_task = asyncio.create_task(player_queues.play_media(queue_id=player_queue_id, media=uri))
    await play_music_task

    return "Media playing"

# Statically defined user functions for fast reference
user_functions_set: Set[Callable[..., Any]] = {
    fetch_current_datetime,
    fetch_weather,
    store_person_info,
    retrieve_person_info,
    retrieve_person_events,
    get_music_player_queues,
    search_radios,
    search_albums,
    search_artist,
    play_media
}


