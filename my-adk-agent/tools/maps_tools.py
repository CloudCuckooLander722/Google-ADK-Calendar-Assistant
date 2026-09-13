import json
import os
import urllib.parse
import urllib.request

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def resolve_address(place_or_address: str) -> str:
    """
    Resolve a place name, landmark, or partial address to a formatted
    real-world address using the Google Maps Geocoding API.

    Args:
        place_or_address: A place name, landmark, or partial address
            (e.g. "Mission San Jose High School").

    Returns:
        The best-matching formatted address, or a message saying none
        was found.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise ValueError(
            "GOOGLE_MAPS_API_KEY is not configured; cannot resolve addresses."
        )

    query = urllib.parse.urlencode({"address": place_or_address, "key": api_key})
    try:
        with urllib.request.urlopen(f"{GEOCODE_URL}?{query}", timeout=10) as response:
            data = json.loads(response.read().decode())
    except Exception as error:
        raise ValueError(
            f"Failed to resolve address '{place_or_address}': {error}"
        ) from error

    if data.get("status") != "OK" or not data.get("results"):
        return f"Could not resolve '{place_or_address}' to a real address."

    return data["results"][0]["formatted_address"]
