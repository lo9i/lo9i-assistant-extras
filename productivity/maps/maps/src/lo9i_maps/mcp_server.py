"""MCP server over stdio: places, addresses and routes from OpenStreetMap. lo9i starts it (see server.yaml)."""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_maps import osm

INSTRUCTIONS = """\
Finds places and addresses (OpenStreetMap), and routes with their distance and travel time by car, on foot, by
bike and by public transport. Give the user the place or the route with its links. It needs no account.
"""

mcp = MCPServer("maps", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=READ_ONLY)
async def search_places(query: str, near: str | None = None, limit: int = 5) -> str:
    """Find places or an address: a name ("Café Tortoni"), a kind of place ("farmacia", "cafe"), or an address
    ("Av. Corrientes 1234, CABA", "Cabildo y Juramento"). Add the city to a name or an address that could be
    anywhere. `near` (a place, an address or "lat,lon") looks around it, nearest first, with distances.
    `limit` up to 10. Each has its address, coordinates and OpenStreetMap and Google Maps links."""
    try:
        return await osm.search_places(query, near, limit)
    except osm.MapsError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def reverse_geocode(lat: float, lon: float) -> str:
    """The address at a point, with its links."""
    try:
        return await osm.reverse_geocode(lat, lon)
    except osm.MapsError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def route(origin: str, destination: str, mode: str = "driving", depart_at: str | None = None) -> str:
    """The route and travel time between two places, addresses or "lat,lon" points. `mode`: driving, walking,
    cycling or transit. Driving, walking and cycling give the distance, the time (driving's without traffic)
    and the steps; transit gives the next trips with their lines and times, from `depart_at` (an ISO time,
    2026-10-30T08:30) when given. With OpenStreetMap and Google Maps links."""
    try:
        return await osm.route(origin, destination, mode, depart_at)
    except osm.MapsError as e:
        raise ToolError(str(e)) from None


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
