"""Open-Meteo (free, no key) for geocoding, forecasts and historical weather."""
from datetime import date, datetime, timezone

import httpx

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def geocode(name: str) -> tuple[float, float] | None:
    for q in (name, name.split(",")[0]):
        try:
            r = httpx.get(GEO, params={"name": q, "count": 1}, timeout=10).json()
            if r.get("results"):
                return r["results"][0]["latitude"], r["results"][0]["longitude"]
        except httpx.HTTPError:
            return None
    return None


def hourly(lat: float, lon: float, start: date, end: date, archive: bool = False) -> dict[datetime, float]:
    """UTC hour -> air temperature (C)."""
    params = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m", "timezone": "UTC",
              "start_date": start.isoformat(), "end_date": end.isoformat()}
    r = httpx.get(ARCHIVE if archive else FORECAST, params=params, timeout=20)
    r.raise_for_status()
    h = r.json()["hourly"]
    return {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): v
            for t, v in zip(h["time"], h["temperature_2m"]) if v is not None}
