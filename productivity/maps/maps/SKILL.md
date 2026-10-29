---
name: maps
description: Maps (the maps MCP server) - finding places and addresses, what's nearby, the address at a point, and routes with distance and travel time by car, on foot, by bike or by public transport. Use it for where something is, how to get somewhere, or how long it takes.
---

The tools are `mcp__maps__search_places`, `mcp__maps__reverse_geocode` and `mcp__maps__route`. They only read OpenStreetMap's public services; no account.

- **Where is a place or an address**: `search_places`. Add the city when it could be anywhere ("Plaza de Mayo, Buenos Aires"); a street and number with no city is taken as one in Buenos Aires. Corners work ("Cabildo y Juramento").
- **What's nearby** (a pharmacy, a cafe): `search_places` with `near` set to where the user is or names, nearest first. Use the user's address from what you know of them when they say "near me" or "near home"; ask when you don't know it.
- **How to get there, how long it takes**: `route`, `mode` driving, walking, cycling or transit. Driving times don't count traffic; say so. Transit covers only cities whose timetables Transitous has (not Buenos Aires today); when it finds nothing, give the Google Maps link it returns.
- **What's at these coordinates**: `reverse_geocode`.

Answer in plain text: the place or the route's distance and time first, then a few steps when asked how to get there, then the links (OpenStreetMap and Google Maps).
