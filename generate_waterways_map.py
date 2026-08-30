import argparse
import gzip
import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path

import folium
import networkx as nx
import requests
import yaml
from folium.features import GeoJson, GeoJsonPopup, GeoJsonTooltip

# Load configuration
try:
    import config
except ImportError:
    raise RuntimeError(
        "Configuration file 'config.py' not found.\n"
        "Please copy 'config.example.py' to 'config.py' and fill in your API keys.\n"
        "See config.example.py for details."
    )

GERMANY_BOUNDS = (47.27, 5.87, 55.06, 15.04)
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "german-waterways-map/1.0"
COLORS = ("#d62728", "#ff7f0e", "#2ca02c", "#9467bd", "#17becf", "#e377c2")
CITIES = [
    (52.5200, 13.4050, "Berlin"),
    (53.5511, 9.9937, "Hamburg"),
    (48.1351, 11.5820, "München"),
    (50.1109, 8.6821, "Frankfurt am Main"),
    (51.2277, 6.7735, "Düsseldorf"),
    (50.9375, 6.9603, "Köln"),
    (51.0504, 13.7373, "Dresden"),
    (49.4521, 11.0767, "Nürnberg"),
    (48.7758, 9.1829, "Stuttgart"),
    (51.3397, 12.3731, "Leipzig"),
]


def log(message, started=None):
    duration = f" ({time.perf_counter() - started:.2f}s)" if started is not None else ""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}{duration}", flush=True)


class RateLimiter:
    def __init__(self, requests_per_minute):
        self.interval = 60 / requests_per_minute if requests_per_minute > 0 else 0
        self.last_request = None

    def wait(self, progress):
        if self.last_request is not None:
            remaining = self.interval - (time.monotonic() - self.last_request)
            while remaining > 0:
                log(f"{progress} Rate limit: next request in {remaining:.0f}s")
                time.sleep(min(1, remaining))
                remaining = self.interval - (time.monotonic() - self.last_request)
        self.last_request = time.monotonic()


def load_routes(path):
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise ValueError("Input must contain a top-level 'routes' list.")
    routes = []
    for index, item in enumerate(data["routes"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Route {index} must be an object.")
        missing = {"waterway", "start", "end"} - item.keys()
        if missing:
            raise ValueError(f"Route {index} is missing: {', '.join(sorted(missing))}.")
        via = item.get("via", [])
        if not isinstance(via, list) or not all(isinstance(place, str) and place.strip() for place in via):
            raise ValueError(f"Route {index} field 'via' must be a list of place names.")
        values = [item["waterway"], item["start"], item["end"]]
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"Route {index} waterway, start, and end must be non-empty strings.")
        places = [item["start"].strip(), *(place.strip() for place in via), item["end"].strip()]
        routes.append({
            "waterway": item["waterway"].strip(),
            "places": places,
            "label": item.get("label") or f"{item['waterway']} from {item['start']} to {item['end']}",
            "color": item.get("color"),
        })
    return routes


def load_cache(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def geocode_places(routes, cache_path):
    started = time.perf_counter()
    cache = load_cache(cache_path)
    changed = False
    requests_made = 0
    for route in routes:
        route["waypoints"] = []
        for place in route["places"]:
            key = place.casefold()
            if key not in cache:
                log(f"Geocoding {place}")
                response = requests.get(
                    NOMINATIM_URL,
                    params={"q": place, "format": "jsonv2", "limit": 1},
                    headers={"User-Agent": USER_AGENT},
                    timeout=30,
                )
                response.raise_for_status()
                results = response.json()
                if not results:
                    raise RuntimeError(f"Could not geocode place: {place}")
                cache[key] = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"]), "display_name": results[0]["display_name"]}
                changed = True
                requests_made += 1
                time.sleep(1.05)
            route["waypoints"].append({"input_name": place, **cache[key]})
    if changed:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Geocoding complete: {requests_made} request(s), {sum(len(route['waypoints']) for route in routes)} cache entries used", started)


def escape_overpass(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_waterway_query(name, waypoints):
    latitudes = [point["lat"] for point in waypoints]
    longitudes = [point["lon"] for point in waypoints]
    south, north = min(latitudes) - 0.35, max(latitudes) + 0.35
    west, east = min(longitudes) - 0.5, max(longitudes) + 0.5
    name = escape_overpass(name)
    selectors = "\n".join(
        f'  way["waterway"~"^(river|canal|fairway)$"]["{key}"="{name}"]({south},{west},{north},{east});\n'
        f'  rel["type"="waterway"]["{key}"="{name}"];\n  way(r)({south},{west},{north},{east});'
        for key in ("name", "name:de", "name:en")
    )
    return f"[out:json][timeout:180];\n(\n{selectors}\n);\nout skel geom;"


def request_overpass(query, cache_dir, rate_limiter, progress, retries, retry_backoff, refresh=False):
    cache_key = hashlib.sha256(query.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json.gz"
    if cache_path.exists() and not refresh:
        started = time.perf_counter()
        with gzip.open(cache_path, "rt", encoding="utf-8") as source:
            data = json.load(source)
        log(f"{progress} Loaded waterway data from cache: {cache_path.name}", started)
        return data
    cache_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    total_attempts = retries * len(OVERPASS_URLS)
    attempt = 0
    for retry_round in range(retries):
        for url in OVERPASS_URLS:
            attempt += 1
            rate_limiter.wait(progress)
            started = time.perf_counter()
            log(f"{progress} Requesting {url} (attempt {attempt}/{total_attempts}, round {retry_round + 1}/{retries})")
            try:
                response = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=210)
                response.raise_for_status()
                data = response.json()
                with gzip.open(cache_path, "wt", encoding="utf-8") as target:
                    json.dump(data, target, separators=(",", ":"))
                log(f"{progress} Downloaded {len(response.content) / 1024:.0f} KiB and cached {len(data.get('elements', []))} ways", started)
                return data
            except (requests.RequestException, ValueError) as error:
                errors.append(f"attempt {attempt}, {url}: {error}")
                log(f"{progress} Provider failed: {error}", started)
                response = getattr(error, "response", None)
                retry_after = response.headers.get("Retry-After") if response is not None else None
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = 0
                    if delay > 0:
                        log(f"{progress} Server requested a {delay:.0f}s retry delay")
                        time.sleep(delay)
        if retry_round < retries - 1:
            delay = retry_backoff * (2 ** retry_round)
            if delay > 0:
                log(f"{progress} All providers failed; retrying next round in {delay:.0f}s")
                time.sleep(delay)
    raise RuntimeError(f"Overpass failed after {total_attempts} attempts:\n" + "\n".join(errors))


def distance_meters(first, second):
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(value))


def waterway_graph(data):
    graph = nx.Graph()
    for element in data.get("elements", []):
        node_ids = element.get("nodes", [])
        geometry = element.get("geometry", [])
        if len(node_ids) != len(geometry):
            continue
        for node_id, point in zip(node_ids, geometry):
            graph.add_node(node_id, lat=point["lat"], lon=point["lon"])
        for first_id, second_id, first_point, second_point in zip(node_ids, node_ids[1:], geometry, geometry[1:]):
            graph.add_edge(first_id, second_id, length=distance_meters((first_point["lat"], first_point["lon"]), (second_point["lat"], second_point["lon"])))
    if not graph:
        raise RuntimeError("No matching waterway geometry was returned by OpenStreetMap.")
    component = max(nx.connected_components(graph), key=len)
    return graph.subgraph(component).copy()


def nearest_node(graph, waypoint):
    return min(graph.nodes, key=lambda node: distance_meters((graph.nodes[node]["lat"], graph.nodes[node]["lon"]), (waypoint["lat"], waypoint["lon"])))


def point_segment_distance(point, start, end):
    reference_latitude = math.radians((start[1] + end[1]) / 2)
    scale_x = 111_320 * math.cos(reference_latitude)
    px, py = (point[0] - start[0]) * scale_x, (point[1] - start[1]) * 110_540
    ex, ey = (end[0] - start[0]) * scale_x, (end[1] - start[1]) * 110_540
    length_squared = ex * ex + ey * ey
    if length_squared == 0:
        return math.hypot(px, py)
    fraction = max(0, min(1, (px * ex + py * ey) / length_squared))
    return math.hypot(px - fraction * ex, py - fraction * ey)


def simplify_coordinates(coordinates, tolerance):
    if tolerance <= 0 or len(coordinates) <= 2:
        return coordinates
    keep = {0, len(coordinates) - 1}
    stack = [(0, len(coordinates) - 1)]
    while stack:
        start, end = stack.pop()
        distances = [(point_segment_distance(coordinates[index], coordinates[start], coordinates[end]), index) for index in range(start + 1, end)]
        if distances:
            distance, index = max(distances)
            if distance > tolerance:
                keep.add(index)
                stack.extend(((start, index), (index, end)))
    return [coordinates[index] for index in sorted(keep)]


def route_waterway(route, color, overpass_cache, route_cache, rate_limiter, progress, retries, retry_backoff, refresh, resolution):
    started = time.perf_counter()
    cache_input = {
        "version": 1,
        "waterway": route["waterway"].casefold(),
        "waypoints": [[round(point["lat"], 7), round(point["lon"], 7)] for point in route["waypoints"]],
        "resolution": resolution,
    }
    cache_key = hashlib.sha256(json.dumps(cache_input, sort_keys=True).encode("utf-8")).hexdigest()
    cache_path = route_cache / f"{cache_key}.json.gz"
    if cache_path.exists() and not refresh:
        with gzip.open(cache_path, "rt", encoding="utf-8") as source:
            cached = json.load(source)
        log(f"{progress} Loaded routed segment from cache: {route['label']}", started)
        return {
            "type": "Feature",
            "properties": {"name": route["waterway"], "label": route["label"], "color": color, "snap_km": cached["snap_km"]},
            "geometry": {"type": "LineString", "coordinates": cached["coordinates"]},
        }
    log(f"{progress} Routing {route['label']}")
    data = request_overpass(
        build_waterway_query(route["waterway"], route["waypoints"]), overpass_cache,
        rate_limiter, progress, retries, retry_backoff, refresh,
    )
    graph_started = time.perf_counter()
    graph = waterway_graph(data)
    log(f"Built graph with {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges", graph_started)
    snapped_nodes = [nearest_node(graph, waypoint) for waypoint in route["waypoints"]]
    path = []
    for source, target in zip(snapped_nodes, snapped_nodes[1:]):
        leg = nx.shortest_path(graph, source, target, weight="length")
        path.extend(leg if not path else leg[1:])
    full_coordinates = [[graph.nodes[node]["lon"], graph.nodes[node]["lat"]] for node in path]
    coordinates = simplify_coordinates(full_coordinates, resolution)
    log(f"Route complete: {len(full_coordinates)} points simplified to {len(coordinates)}", started)
    snap_distances = [
        round(distance_meters((graph.nodes[node]["lat"], graph.nodes[node]["lon"]), (point["lat"], point["lon"])) / 1000, 1)
        for node, point in zip(snapped_nodes, route["waypoints"])
    ]
    snap_km = ", ".join(map(str, snap_distances))
    route_cache.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_path, "wt", encoding="utf-8") as target:
        json.dump({"coordinates": coordinates, "snap_km": snap_km}, target, separators=(",", ":"))
    log(f"Cached routed segment: {cache_path.name}")
    return {
        "type": "Feature",
        "properties": {"name": route["waterway"], "label": route["label"], "color": color, "snap_km": snap_km},
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def load_geojson_segments(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON input must be a FeatureCollection.")
    return data.get("features", [])


def add_highlight_layer(map_object, features):
    for index, feature in enumerate(features, start=1):
        properties = feature.setdefault("properties", {})
        properties.setdefault("label", properties.get("name", "Highlighted waterway"))
        properties.setdefault("name", properties["label"])
        color = properties.setdefault("color", COLORS[(index - 1) % len(COLORS)])
        GeoJson(
            feature,
            name=f"Route {index}: {properties['label']}",
            style_function=lambda _, color=color: {"color": color, "weight": 6, "opacity": 0.95},
            highlight_function=lambda _: {"weight": 9},
            tooltip=GeoJsonTooltip(fields=["label"], aliases=["Route"]),
            popup=GeoJsonPopup(fields=["label", "name"], aliases=["Route", "Waterway"], localize=True),
        ).add_to(map_object)


def create_map(features, routes, output_path):
    south, west, north, east = GERMANY_BOUNDS
    map_object = folium.Map(tiles=None, control_scale=True, zoom_start=6)
    # Determine tile source: prefer config.TILE_SOURCE (a dict), otherwise
    # fall back to the legacy CARTO_API_KEY-based URL if present.
    # Require a TILE_SOURCE dict in config.py. This must contain at least
    # a 'tiles' URL template. See config.example.py for examples.
    tile_cfg = getattr(config, "TILE_SOURCE", None)
    if not tile_cfg:
        raise RuntimeError("Please configure `TILE_SOURCE` in config.py (see config.example.py) and include a 'tiles' entry.")
    tiles = tile_cfg.get("tiles")
    if not tiles:
        raise RuntimeError("config.TILE_SOURCE must contain a 'tiles' entry (URL template).")
    attr = tile_cfg.get("attr")
    name = tile_cfg.get("name", "Base map")
    # Pass remaining keys as TileLayer options (detect_retina, max_zoom, subdomains, etc.)
    options = {k: v for k, v in tile_cfg.items() if k not in ("tiles", "attr", "name")}

    folium.TileLayer(tiles=tiles, name=name, control=False, attr=attr, **options).add_to(map_object)
    
    #for latitude, longitude, name in CITIES:
    #    folium.CircleMarker((latitude, longitude), radius=4, color="#202020", fill=True, fill_color="#ffffff", fill_opacity=1, tooltip=name).add_to(map_object)
    for route in routes:
        color = route["map_color"]
        for position, waypoint in enumerate(route.get("waypoints", [])):
            role = "Start" if position == 0 else "End" if position == len(route["waypoints"]) - 1 else "Via"
            folium.CircleMarker(
                (waypoint["lat"], waypoint["lon"]), radius=6, color=color, fill=True, fill_color=color,
                tooltip=f"{role}: {waypoint['input_name']}",
            ).add_to(map_object)
    add_highlight_layer(map_object, features)
    map_object.fit_bounds([(south, west), (north, east)])
    folium.LayerControl(collapsed=False).add_to(map_object)
    map_object.save(output_path)


def main():
    parser = argparse.ArgumentParser(description="Route along named waterways and create an interactive map.")
    parser.add_argument("input", type=Path, help="YAML/JSON route file or exact GeoJSON geometry")
    parser.add_argument("--format", choices=["routes", "geojson"], help="Defaults to geojson for .geojson files, otherwise routes")
    parser.add_argument("--output", type=Path, default=Path("german_waterways_map.html"), help="HTML output path")
    parser.add_argument("--cache", type=Path, default=Path("geocoding_cache.json"), help="Geocoding cache path")
    parser.add_argument("--waterway-cache", type=Path, default=Path("waterway_cache"), help="Cached Overpass response directory")
    parser.add_argument("--route-cache", type=Path, default=Path("route_cache"), help="Cached completed route directory")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached Overpass responses and completed routes")
    parser.add_argument("--resolution", type=float, default=200, metavar="METERS", help="Route simplification tolerance; 0 keeps every OSM point")
    parser.add_argument("--overpass-rate-limit", type=float, default=30, metavar="REQUESTS_PER_MINUTE", help="Maximum Overpass request rate; 0 disables throttling")
    parser.add_argument("--overpass-retries", type=int, default=3, metavar="ROUNDS", help="Number of rounds through all Overpass providers")
    parser.add_argument("--overpass-retry-backoff", type=float, default=15, metavar="SECONDS", help="Initial delay between retry rounds; doubles each round")
    args = parser.parse_args()
    if args.resolution < 0:
        parser.error("--resolution must be zero or greater")
    if args.overpass_rate_limit < 0:
        parser.error("--overpass-rate-limit must be zero or greater")
    if args.overpass_retries < 1:
        parser.error("--overpass-retries must be at least 1")
    if args.overpass_retry_backoff < 0:
        parser.error("--overpass-retry-backoff must be zero or greater")
    input_format = args.format or ("geojson" if args.input.suffix.lower() == ".geojson" else "routes")
    overall_started = time.perf_counter()
    log(f"Reading {args.input}")
    routes = []
    if input_format == "routes":
        routes = load_routes(args.input)
        if not routes:
            raise ValueError("The route file contains no routes.")
        geocode_places(routes, args.cache)
        for index, route in enumerate(routes):
            route["map_color"] = route["color"] or COLORS[index % len(COLORS)]
        rate_limiter = RateLimiter(args.overpass_rate_limit)
        features = []
        for index, route in enumerate(routes, start=1):
            progress = f"[Waterway {index}/{len(routes)}]"
            features.append(route_waterway(
                route, route["map_color"], args.waterway_cache, args.route_cache,
                rate_limiter, progress, args.overpass_retries, args.overpass_retry_backoff,
                args.refresh, args.resolution,
            ))
    else:
        features = load_geojson_segments(args.input)
    map_started = time.perf_counter()
    log("Rendering HTML map")
    create_map(features, routes, args.output)
    log(f"Wrote {args.output.resolve()}", map_started)
    log("All work complete", overall_started)


if __name__ == "__main__":
    main()
