import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import Client

from lo9i_maps import mcp_server, osm

TORTONI = {
    "lat": "-34.6087",
    "lon": "-58.3787",
    "category": "amenity",
    "type": "cafe",
    "name": "Café Tortoni",
    "address": {"road": "Avenida de Mayo", "house_number": "825", "suburb": "Monserrat", "city": "Buenos Aires",
                "country": "Argentina"},
}  # fmt: skip
OBELISCO = {
    "lat": "-34.6037",
    "lon": "-58.3816",
    "category": "tourism",
    "type": "attraction",
    "name": "Obelisco",
    "address": {"road": "Avenida 9 de Julio", "suburb": "San Nicolás", "city": "Buenos Aires", "country": "Argentina"},
}
FAR = {
    "lat": "-34.6100",
    "lon": "-58.3700",
    "category": "amenity",
    "type": "cafe",
    "name": "Far Cafe",
    "address": {"city": "Buenos Aires", "country": "Argentina"},
}


@pytest.fixture(autouse=True)
def fresh():
    osm._pace = osm._Pace(0)
    osm._cache.clear()


def serve(monkeypatch, answer):
    """Route the plugin's requests to `answer(request)`, which returns (status, json); returns the requests."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = answer(request)
        return httpx.Response(status, json=body)

    headers = osm._client().headers
    monkeypatch.setattr(
        osm, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle), headers=headers)
    )
    return seen


def query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_every_tool_is_read_only_and_answers_in_text(monkeypatch):
    serve(monkeypatch, lambda r: (200, [TORTONI]))
    tools, result = await _call("search_places", query="Café Tortoni, Buenos Aires")
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
    assert set(tools) == {"search_places", "reverse_geocode", "route"}
    assert result.content[0].text.startswith("1. Café Tortoni (cafe), Avenida de Mayo 825, Monserrat, Buenos Aires")


async def test_nominatim_is_asked_as_its_policy_says(monkeypatch):
    seen = serve(monkeypatch, lambda r: (200, [TORTONI]))
    await osm.search_places("Café Tortoni")
    await osm.search_places("Café Tortoni")
    await osm.search_places("Obelisco")
    assert len(seen) == 2, "the same search again is answered from the cache"
    assert seen[0].headers["User-Agent"] == osm.USER_AGENT
    assert query(seen[0])["format"] == "jsonv2"


async def test_nominatim_requests_are_a_second_apart(monkeypatch):
    stamps: list[float] = []

    def answer(request):
        stamps.append(time.monotonic())
        return 200, [TORTONI]

    serve(monkeypatch, answer)
    osm._pace = osm._Pace(0.3)
    await osm.search_places("one")
    await osm.search_places("two")
    assert stamps[1] - stamps[0] >= 0.29


async def test_near_searches_around_the_place_nearest_first(monkeypatch):
    def answer(request):
        q = query(request)
        if q["q"] == "Obelisco, Buenos Aires":
            return 200, [OBELISCO]
        return 200, [FAR, TORTONI]

    seen = serve(monkeypatch, answer)
    text = await osm.search_places("cafe", near="Obelisco, Buenos Aires")
    around = query(seen[1])
    assert around["bounded"] == "1" and len(around["viewbox"].split(",")) == 4
    lines = text.splitlines()
    assert lines[0] == "Near Obelisco, Avenida 9 de Julio, San Nicolás, Buenos Aires, Argentina:"
    assert lines[1].startswith("1. Café Tortoni (cafe)") and "m away" in lines[1]
    assert "https://www.openstreetmap.org/?mlat=-34.60870&mlon=-58.37870" in lines[2]
    assert lines[3] == "   https://www.google.com/maps/search/?api=1&query=-34.60870%2C-58.37870"
    assert lines[4].startswith("2. Far Cafe (cafe)")
    nearest = await osm.search_places("cafe", near="Obelisco, Buenos Aires", limit=1)
    assert query(seen[-1])["limit"] == str(osm.NEARBY)
    assert "Café Tortoni" in nearest and "Far Cafe" not in nearest


async def test_near_widens_the_search_when_nothing_is_close(monkeypatch):
    def answer(request):
        q = query(request)
        if "viewbox" not in q:
            return 200, [OBELISCO]
        return 200, [TORTONI] if float(q["viewbox"].split(",")[2]) - float(q["viewbox"].split(",")[0]) > 0.1 else []

    seen = serve(monkeypatch, answer)
    text = await osm.search_places("cafe", near="-34.6037,-58.3816")
    assert len(seen) == 2, "coordinates aren't looked up"
    assert "Café Tortoni" in text


async def test_a_street_address_in_buenos_aires_comes_from_usig(monkeypatch):
    def answer(request):
        assert request.url.host == "servicios.usig.buenosaires.gob.ar"
        assert query(request)["direccion"] == "Corrientes 1234", "the city's name is left out"
        moreno = {"direccion": "Corrientes 1234, Moreno", "cod_partido": "moreno",
                  "coordenadas": {"x": -58.77, "y": -34.65, "srid": 4326}}  # fmt: skip
        caba = {"direccion": "CORRIENTES AV. 1234, CABA", "cod_partido": "caba",
                "coordenadas": {"x": "-58.384222", "y": "-34.603939", "srid": 4326}}  # fmt: skip
        return 200, {"direccionesNormalizadas": [moreno, caba]}

    serve(monkeypatch, answer)
    text = await osm.search_places("Corrientes 1234, Ciudad Autónoma de Buenos Aires")
    assert text.startswith("1. CORRIENTES AV. 1234, CABA. At -34.60394, -58.38422.")


async def test_an_address_usig_doesnt_know_goes_to_nominatim(monkeypatch):
    def answer(request):
        if request.url.host == "servicios.usig.buenosaires.gob.ar":
            assert query(request)["direccion"] == "San Martín 500, Mendoza"
            return 200, {"direccionesNormalizadas": [], "errorMessage": "Calle inexistente"}
        return 200, [{**TORTONI, "name": "", "category": "building", "type": "house"}]

    seen = serve(monkeypatch, answer)
    text = await osm.search_places("San Martín 500, Mendoza")
    assert [r.url.host for r in seen] == ["servicios.usig.buenosaires.gob.ar", "nominatim.openstreetmap.org"]
    assert text.startswith("1. Avenida de Mayo 825, Monserrat, Buenos Aires, Argentina. At")


async def test_a_route_on_foot(monkeypatch):
    steps = [
        {"maneuver": {"type": "depart", "bearing_after": 266}, "name": "", "distance": 97.8},
        {"maneuver": {"type": "end of road", "modifier": "right"}, "name": "", "distance": 48.5},
        {"maneuver": {"type": "turn", "modifier": "left"}, "name": "Carabelas", "distance": 182.1},
        {"maneuver": {"type": "new name", "modifier": "straight"}, "name": "Carabelas", "distance": 20},
        {"maneuver": {"type": "turn", "modifier": "right"}, "name": "Florida", "distance": 1250},
        {"maneuver": {"type": "arrive"}, "name": "Florida", "distance": 0},
    ]

    def answer(request):
        if request.url.host == "routing.openstreetmap.de":
            return 200, {"code": "Ok", "routes": [{"distance": 1548.6, "duration": 1251.1, "legs": [{"steps": steps}]}]}
        return 200, [TORTONI] if query(request)["q"] == "Café Tortoni" else [OBELISCO]

    seen = serve(monkeypatch, answer)
    text = await osm.route("Obelisco", "Café Tortoni", mode="walk")
    assert seen[-1].url.path == "/routed-foot/route/v1/driving/-58.3816,-34.6037;-58.3787,-34.6087"
    assert text.splitlines() == [
        "Walking from Obelisco, Avenida 9 de Julio, San Nicolás, Buenos Aires, Argentina to Café Tortoni, "
        "Avenida de Mayo 825, Monserrat, Buenos Aires, Argentina: 1.5 km, about 21 min.",
        "1. Head west (150 m)",
        "2. Turn left onto Carabelas (200 m)",
        "3. Turn right onto Florida (1.2 km)",
        "OpenStreetMap: https://www.openstreetmap.org/directions?engine=fossgis_osrm_foot"
        "&route=-34.60370%2C-58.38160%3B-34.60870%2C-58.37870",
        "Google Maps: https://www.google.com/maps/dir/?api=1&origin=-34.60370%2C-58.38160"
        "&destination=-34.60870%2C-58.37870&travelmode=walking",
    ]


async def test_no_route_says_why(monkeypatch):
    serve(monkeypatch, lambda r: (400, {"code": "NoRoute", "message": "Impossible route between points"}))
    _, result = await _call("route", origin="-34.6,-58.4", destination="-33.9,18.4", mode="cycling")
    assert result.is_error
    assert "Impossible route between points" in result.content[0].text


async def test_an_unknown_mode_is_refused():
    with pytest.raises(osm.MapsError, match="Unknown mode 'teleport'"):
        await osm.route("a", "b", mode="teleport")


async def test_transit_lists_the_trips_in_local_time(monkeypatch):
    berlin = {"tz": "Europe/Berlin"}
    trip = {
        "duration": 660,
        "startTime": "2026-10-29T11:58:00Z",
        "endTime": "2026-10-29T12:09:00Z",
        "transfers": 1,
        "legs": [
            {"mode": "WALK", "duration": 60, "from": {"name": "START"}, "to": {"name": "Hauptbahnhof", **berlin}},
            {"mode": "SUBURBAN", "displayName": "S7", "headsign": "Ahrensfelde", "from": {"name": "Hauptbahnhof"},
             "to": {"name": "Friedrichstr."}, "startTime": "2026-10-29T11:59:00Z", "endTime": "2026-10-29T12:00:00Z",
             "duration": 60},
            {"mode": "WALK", "duration": 180, "from": {"name": "Friedrichstr."}, "to": {"name": "END"}},
        ],
    }  # fmt: skip

    def answer(request):
        assert request.url.host == "api.transitous.org"
        assert query(request)["time"] == "2026-10-30T08:30:00-03:00"
        return 200, {"itineraries": [trip]}

    serve(monkeypatch, answer)
    text = await osm.route("52.5251,13.3694", "52.5076,13.3904", mode="transit", depart_at="2026-10-30T08:30-03:00")
    lines = text.splitlines()
    assert lines[1] == (
        "1. 12:58 to 13:09, 11 min, 1 change: walk 1 min to Hauptbahnhof; train S7 toward Ahrensfelde from "
        "Hauptbahnhof at 12:59 to Friedrichstr. at 13:00; walk 3 min."
    )
    assert "travelmode=transit" in lines[2]


async def test_transit_without_timetables_gives_google_maps(monkeypatch):
    serve(monkeypatch, lambda r: (200, {"itineraries": []}))
    text = await osm.route("-34.6037,-58.3816", "-34.5889,-58.4306", mode="bus")
    assert text.startswith("Transitous found no public transport")
    assert text.splitlines()[-1].startswith("Google Maps: https://www.google.com/maps/dir/?api=1")


async def test_reverse_geocode(monkeypatch):
    seen = serve(monkeypatch, lambda r: (200, TORTONI))
    _, result = await _call("reverse_geocode", lat=-34.60871, lon=-58.37872)
    assert seen[0].url.path == "/reverse"
    assert result.content[0].text.startswith(
        "Café Tortoni (cafe), Avenida de Mayo 825, Monserrat, Buenos Aires, Argentina. At -34.60871, -58.37872."
    )
