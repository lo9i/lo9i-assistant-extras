"""Places, addresses and routes, with no key: Nominatim (OpenStreetMap's search) finds places and addresses,
Buenos Aires' USIG finds street addresses in the city and its suburbs (OpenStreetMap has few house numbers in
Argentina), the FOSSGIS OSRM servers route by car, on foot and by bike (openstreetmap.org's directions use them),
and Transitous plans trips by public transport."""

import asyncio
import logging
import math
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org"
USIG = "https://servicios.usig.buenosaires.gob.ar/normalizar/"
OSRM = "https://routing.openstreetmap.de"
TRANSITOUS = "https://api.transitous.org/api/v5/plan"
# Nominatim's usage policy asks for an application that says who it is, a request a second at most, and
# results kept instead of asked again.
USER_AGENT = "lo9i-maps/1.0 (https://github.com/lo9i/lo9i-assistant-extras)"
CACHED = 256
# Each mode's FOSSGIS server (routed-<name>, also openstreetmap.org's engine fossgis_osrm_<name>), and Google
# Maps' name for it. Transit is Transitous'.
MODES = {"driving": "car", "walking": "foot", "cycling": "bike", "transit": ""}
GOOGLE_MODES = {"driving": "driving", "walking": "walking", "cycling": "bicycling", "transit": "transit"}
_ALIASES = {
    "car": "driving", "drive": "driving", "walk": "walking", "foot": "walking", "bike": "cycling",
    "bicycle": "cycling", "public transport": "transit", "bus": "transit", "train": "transit", "subway": "transit",
}  # fmt: skip
# Nominatim ranks what it finds around a place by importance, not distance: the nearest are among the most it
# gives (40).
NEARBY = 40
# The steps a route lists at most; the rest are counted.
STEPS = 15
# The trips by public transport listed; Transitous gives more.
TRIPS = 3
_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
# A house number, or a corner ("Cabildo y Juramento"): what USIG may find.
_STREET_ADDRESS = re.compile(r"\d|\s+y\s+", re.IGNORECASE)
# Words after an address's comma that only say it's in the city of Buenos Aires, which USIG doesn't take: the
# city's names and its barrios.
_IN_THE_CITY = {
    "argentina", "buenos aires", "caba", "c.a.b.a.", "capital", "capital federal", "ciudad de buenos aires",
    "ciudad autonoma de buenos aires", "agronomia", "almagro", "balvanera", "barracas", "belgrano", "boedo",
    "caballito", "chacarita", "coghlan", "colegiales", "constitucion", "flores", "floresta", "la boca",
    "la paternal", "liniers", "mataderos", "monte castro", "montserrat", "monserrat", "nueva pompeya", "nunez",
    "palermo", "parque avellaneda", "parque chacabuco", "parque chas", "parque patricios", "puerto madero",
    "recoleta", "retiro", "saavedra", "san cristobal", "san nicolas", "san telmo", "velez sarsfield", "versalles",
    "villa crespo", "villa del parque", "villa devoto", "villa general mitre", "villa lugano", "villa luro",
    "villa ortuzar", "villa pueyrredon", "villa real", "villa riachuelo", "villa santa rita", "villa soldati",
    "villa urquiza", "microcentro", "centro", "once", "abasto", "barrio norte",
}  # fmt: skip
# Nominatim categories whose type says what a place is (a cafe, a pharmacy); a road's or a house's says little.
_PLAIN_CATEGORIES = {"highway", "place", "boundary", "building", "landuse"}
_TRANSIT_MODES = {
    "BUS": "bus", "COACH": "coach", "TRAM": "tram", "SUBWAY": "subway", "METRO": "metro", "FERRY": "ferry",
    "SUBURBAN": "train", "RAIL": "train", "REGIONAL_RAIL": "train", "REGIONAL_FAST_RAIL": "train",
    "HIGHSPEED_RAIL": "train", "LONG_DISTANCE": "train", "NIGHT_RAIL": "train", "FUNICULAR": "funicular",
    "CABLE_CAR": "cable car", "AERIAL_LIFT": "cable car",
}  # fmt: skip


class MapsError(Exception):
    """A place or a route couldn't be found. The text says why."""


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    lat: float
    lon: float
    kind: str = ""

    @property
    def label(self) -> str:
        if not self.address or self.address.startswith(self.name):
            return self.address or self.name
        return f"{self.name}, {self.address}"

    @property
    def point(self) -> str:
        return f"{self.lat:.5f},{self.lon:.5f}"


class _Pace:
    """One request at a time, `interval` seconds apart."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *_: object) -> None:
        self._last = time.monotonic()
        self._lock.release()


_pace = _Pace(1.0)
_cache: OrderedDict[tuple, Any] = OrderedDict()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT})


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
    host = httpx.URL(url).host
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise MapsError(f"Couldn't reach {host}: {e}") from None
    try:
        found = response.json()
    except ValueError:
        found = None
    if response.is_error:
        said = found.get("message") if isinstance(found, dict) else None
        raise MapsError(f"{host} answered {response.status_code}: {said or response.reason_phrase}")
    if found is None:
        raise MapsError(f"{host} answered something that isn't JSON")
    return found


async def _nominatim(client: httpx.AsyncClient, endpoint: str, **params: Any) -> Any:
    params |= {"format": "jsonv2", "addressdetails": 1}
    key = (endpoint, *sorted(params.items()))
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    async with _pace:
        found = await _get(client, f"{NOMINATIM}/{endpoint}", params)
    _cache[key] = found
    if len(_cache) > CACHED:
        _cache.popitem(last=False)
    return found


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().strip().lower()


def _from_nominatim(item: dict[str, Any]) -> Place:
    address = item.get("address") or {}
    street = address.get("road") or address.get("pedestrian") or address.get("footway") or ""
    parts = [
        " ".join(filter(None, (street, address.get("house_number")))),
        address.get("suburb") or address.get("neighbourhood") or address.get("city_district"),
        address.get("city") or address.get("town") or address.get("village") or address.get("state"),
        address.get("country"),
    ]
    short = ", ".join(dict.fromkeys(p for p in parts if p)) or item.get("display_name", "")
    plain = item.get("category") in _PLAIN_CATEGORIES or item.get("type") == "yes"
    return Place(
        name=item.get("name") or short,
        address=short,
        lat=float(item["lat"]),
        lon=float(item["lon"]),
        kind="" if plain else str(item.get("type", "")).replace("_", " "),
    )


async def _usig(client: httpx.AsyncClient, text: str) -> Place | None:
    """The street address or corner in Buenos Aires or its suburbs (USIG knows the metropolitan area), from the
    city's USIG; None when it isn't one or USIG can't be reached. A street with no town is the city's."""
    if not _STREET_ADDRESS.search(text):
        return None
    street, _, rest = text.partition(",")
    in_the_city = bool(rest) and all(_plain(part) in _IN_THE_CITY for part in rest.split(","))
    params = {"direccion": street if in_the_city else text, "geocodificar": "TRUE", "srid": 4326, "maxOptions": 5}
    try:
        found = await _get(client, USIG, params)
    except MapsError as e:
        log.warning("Looking %r up in USIG: %s; trying Nominatim", text, e)
        return None
    options = [o for o in found.get("direccionesNormalizadas") or [] if o.get("coordenadas")]
    if in_the_city:
        options = [o for o in options if o.get("cod_partido") == "caba"]
    if not options:
        return None
    where = options[0]["coordenadas"]
    return Place(name=options[0]["direccion"], address="", lat=float(where["y"]), lon=float(where["x"]))


async def _locate(client: httpx.AsyncClient, text: str) -> Place:
    if match := _COORDS.match(text):
        lat, lon = float(match[1]), float(match[2])
        return Place(name=f"{lat:.5f}, {lon:.5f}", address="", lat=lat, lon=lon)
    if place := await _usig(client, text):
        return place
    found = await _nominatim(client, "search", q=text, limit=1)
    if not found:
        raise MapsError(f"Found nothing for {text!r}. Add the city, or the street and its number.")
    return _from_nominatim(found[0])


def _box(center: Place, km: float) -> str:
    lat, lon = km / 111.0, km / (111.0 * max(math.cos(math.radians(center.lat)), 0.01))
    return f"{center.lon - lon},{center.lat + lat},{center.lon + lon},{center.lat - lat}"


def _meters_between(a: Place, b: Place) -> float:
    la, lb = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((lb - la) / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _distance(meters: float) -> str:
    return f"{max(10, round(meters / 10) * 10)} m" if meters < 1000 else f"{meters / 1000:.1f} km"


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _links(place: Place) -> str:
    osm = f"https://www.openstreetmap.org/?mlat={place.lat:.5f}&mlon={place.lon:.5f}#map=18/{place.lat:.5f}/{place.lon:.5f}"
    google = "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": place.point})
    return f"{osm}\n   {google}"


def _describe(place: Place, center: Place | None = None) -> str:
    text = place.label
    if place.kind:
        text = text.replace(place.name, f"{place.name} ({place.kind})", 1)
    text += f". At {place.lat:.5f}, {place.lon:.5f}"
    if center:
        text += f", {_distance(_meters_between(center, place))} away"
    return f"{text}.\n   {_links(place)}"


async def search_places(query: str, near: str | None = None, limit: int = 5) -> str:
    """Places or addresses matching `query`, nearest first around `near` (a place, an address or "lat,lon")."""
    limit = max(1, min(limit, 10))
    async with _client() as client:
        center = await _locate(client, near) if near else None
        if center:
            found = []
            for km in (2, 10):
                found = await _nominatim(client, "search", q=query, limit=NEARBY, viewbox=_box(center, km), bounded=1)
                if found:
                    break
            places = sorted(map(_from_nominatim, found), key=lambda p: _meters_between(center, p))[:limit]
        elif place := await _usig(client, query):
            places = [place]
        else:
            places = [_from_nominatim(item) for item in await _nominatim(client, "search", q=query, limit=limit)]
    if not places:
        around = f" within 10 km of {center.label}" if center else ""
        return f"Found nothing for {query!r}{around}. Try other words, or add the city or the neighbourhood."
    heading = f"Near {center.label}:\n" if center else ""
    return heading + "\n".join(f"{i}. {_describe(p, center)}" for i, p in enumerate(places, 1))


async def reverse_geocode(lat: float, lon: float) -> str:
    async with _client() as client:
        found = await _nominatim(client, "reverse", lat=lat, lon=lon, zoom=18)
    if not found or "error" in found:
        raise MapsError(f"OpenStreetMap knows no address at {lat}, {lon}.")
    place = _from_nominatim(found)
    return _describe(Place(place.name, place.address, lat, lon, place.kind))


def _mode(mode: str) -> str:
    mode = mode.strip().lower()
    mode = _ALIASES.get(mode, mode)
    if mode not in MODES:
        raise MapsError(f"Unknown mode {mode!r}; use one of {', '.join(MODES)}.")
    return mode


def _compass(bearing: float) -> str:
    return ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")[
        round(bearing / 45) % 8
    ]


def _instruction(step: dict[str, Any]) -> str:
    maneuver = step["maneuver"]
    kind, modifier = maneuver["type"], maneuver.get("modifier", "")
    name = step.get("name") or step.get("ref") or ""
    onto = f" onto {name}" if name else ""
    if kind == "depart":
        return f"Head {_compass(maneuver.get('bearing_after', 0))}" + (f" on {name}" if name else "")
    if kind in ("roundabout", "rotary"):
        return f"At the roundabout take exit {maneuver.get('exit', 1)}{onto}"
    if kind == "on ramp":
        return f"Take the ramp{onto}"
    if kind == "off ramp":
        return f"Take the exit{onto}"
    if kind == "fork":
        return f"Keep {modifier or 'straight'}{onto}"
    if kind == "merge":
        return f"Merge{onto}"
    if modifier == "uturn":
        return f"Make a U-turn{onto}"
    if kind == "new name" or modifier in ("straight", ""):
        return f"Continue{onto}"
    return f"Turn {modifier}{onto}"


def _steps(steps: list[dict[str, Any]]) -> list[str]:
    """The route's steps, short: a step on a street with no name, or on the same street, adds to the one before."""
    merged: list[list[Any]] = []
    for step in steps:
        kind = step["maneuver"]["type"]
        if kind == "arrive":
            continue
        name = step.get("name") or step.get("ref") or ""
        if merged and kind not in ("roundabout", "rotary") and (not name or name == merged[-1][1]):
            merged[-1][2] += step.get("distance", 0)
            continue
        merged.append([_instruction(step), name, step.get("distance", 0)])
    lines = [f"{i}. {text} ({_distance(meters)})" for i, (text, _, meters) in enumerate(merged[:STEPS], 1)]
    if len(merged) > STEPS:
        lines.append(f"… and {len(merged) - STEPS} more steps.")
    return lines


def _google_route(start: Place, end: Place, mode: str) -> str:
    params = {"api": 1, "origin": start.point, "destination": end.point, "travelmode": GOOGLE_MODES[mode]}
    return "https://www.google.com/maps/dir/?" + urlencode(params)


async def route(origin: str, destination: str, mode: str = "driving", depart_at: str | None = None) -> str:
    """The route from `origin` to `destination` (places, addresses or "lat,lon") with its distance, travel time
    and steps; by transit, the next trips, from `depart_at` (an ISO time) when given."""
    mode = _mode(mode)
    async with _client() as client:
        start = await _locate(client, origin)
        end = await _locate(client, destination)
        if mode == "transit":
            return await _transit(client, start, end, depart_at)
        where = f"{start.lon},{start.lat};{end.lon},{end.lat}"
        found = await _get(
            client, f"{OSRM}/routed-{MODES[mode]}/route/v1/driving/{where}", {"overview": "false", "steps": "true"}
        )
    if found.get("code") != "Ok" or not found.get("routes"):
        raise MapsError(f"No {mode} route from {start.label} to {end.label}: {found.get('message', found.get('code'))}")
    best = found["routes"][0]
    traffic = " without traffic" if mode == "driving" else ""
    lines = [
        f"{mode.capitalize()} from {start.label} to {end.label}: {_distance(best['distance'])}, "
        f"about {_duration(best['duration'])}{traffic}.",
        *_steps(best["legs"][0]["steps"]),
        "OpenStreetMap: https://www.openstreetmap.org/directions?"
        + urlencode({"engine": f"fossgis_osrm_{MODES[mode]}", "route": f"{start.point};{end.point}"}),
        f"Google Maps: {_google_route(start, end, mode)}",
    ]
    return "\n".join(lines)


def _when(depart_at: str) -> str:
    try:
        moment = datetime.fromisoformat(depart_at)
    except ValueError:
        raise MapsError(f"{depart_at!r} isn't an ISO time like 2026-10-30T08:30.") from None
    return (moment if moment.tzinfo else moment.astimezone()).isoformat()


def _clock(stamp: str, zone: ZoneInfo | None) -> str:
    moment = datetime.fromisoformat(stamp)
    return (moment.astimezone(zone) if zone else moment.astimezone()).strftime("%H:%M")


def _leg(leg: dict[str, Any], zone: ZoneInfo | None) -> str:
    stop = leg["to"].get("name", "")
    if leg["mode"] == "WALK":
        return f"walk {_duration(leg['duration'])}" + (f" to {stop}" if stop and stop != "END" else "")
    line = next((n for n in (leg.get("displayName"), leg.get("routeShortName")) if n and n != "?"), "")
    what = " ".join(filter(None, (_TRANSIT_MODES.get(leg["mode"], ""), line))) or "transport"
    toward = f" toward {leg['headsign']}" if leg.get("headsign") else ""
    return (
        f"{what}{toward} from {leg['from'].get('name', '')} at {_clock(leg['startTime'], zone)} "
        f"to {stop} at {_clock(leg['endTime'], zone)}"
    )


async def _transit(client: httpx.AsyncClient, start: Place, end: Place, depart_at: str | None) -> str:
    params: dict[str, Any] = {"fromPlace": start.point, "toPlace": end.point, "numItineraries": TRIPS}
    if depart_at:
        params["time"] = _when(depart_at)
    found = await _get(client, TRANSITOUS, params)
    trips = [t for t in found.get("itineraries") or [] if any(leg["mode"] != "WALK" for leg in t["legs"])][:TRIPS]
    google = f"Google Maps: {_google_route(start, end, 'transit')}"
    if not trips:
        return (
            f"Transitous found no public transport from {start.label} to {end.label}: it may have no timetables "
            f"for this area, or nothing runs then.\n{google}"
        )
    lines = [f"Public transport from {start.label} to {end.label} (Transitous):"]
    for i, trip in enumerate(trips, 1):
        zones = [p["tz"] for leg in trip["legs"] for p in (leg["from"], leg["to"]) if p.get("tz")]
        zone = ZoneInfo(zones[0]) if zones else None
        changes = trip.get("transfers", 0)
        lines.append(
            f"{i}. {_clock(trip['startTime'], zone)} to {_clock(trip['endTime'], zone)}, "
            f"{_duration(trip['duration'])}, {changes} change{'' if changes == 1 else 's'}: "
            + "; ".join(_leg(leg, zone) for leg in trip["legs"])
            + "."
        )
    lines.append(google)
    return "\n".join(lines)
