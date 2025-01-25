import requests
import json
import datetime
from typing import Callable, Any, Set
from pathlib import Path
import io, os, base64


# These are the user-defined functions that can be called by the agent.

def fetch_current_datetime() -> str:
    """
    Get the current time as a JSON string.

    :return: The current time in JSON format.
    :rtype: str
    """
    current_time = datetime.datetime.now()
    time_json = json.dumps({"current_time": current_time.strftime("%Y-%m-%d %H:%M:%S")})
    return time_json

def fetch_weather(latitude: float, longitude: float, date: str) -> dict:
    """
    Fetches the weather forecast for the specified latitude and longitude and date using the Open-Meteo API.

    :param latitude: The latitude to fetch weather for.
    :param longitude: The longitude to fetch weather for.
    :param date: The date to fetch weather for in YYYY-MM-DD format.
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

# Statically defined user functions for fast reference
user_functions: Set[Callable[..., Any]] = {
    fetch_current_datetime,
    fetch_weather
}
