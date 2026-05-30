"""Поиск мест вокруг точки через OpenStreetMap Overpass API.

Бесплатно, без ключа. Polite usage:
- timeout 25s в QL
- разумный radius (рекомендую <= 10км)
- User-Agent с контактом
"""

import httpx
import structlog

log = structlog.get_logger()

OVERPASS_PRIMARY = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACKS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "telegram_ai_companion_bot/0.1 (single-user)"


# Высокоуровневые категории → списки (key, value) для Overpass
CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "badminton": [("sport", "badminton")],
    "tennis": [("sport", "tennis")],
    "table_tennis": [("sport", "table_tennis")],
    "swimming": [("sport", "swimming"), ("leisure", "swimming_pool")],
    "gym": [("leisure", "fitness_centre")],
    "sports_centre": [("leisure", "sports_centre")],
    "sauna": [("leisure", "sauna"), ("amenity", "public_bath")],
    "park": [("leisure", "park"), ("leisure", "garden")],
    "cafe": [("amenity", "cafe")],
    "restaurant": [("amenity", "restaurant")],
    "bar": [("amenity", "bar"), ("amenity", "pub")],
    "cinema": [("amenity", "cinema")],
    "museum": [("tourism", "museum")],
    "viewpoint": [("tourism", "viewpoint")],
    "ice_cream": [("amenity", "ice_cream")],
    "yoga": [("sport", "yoga")],
    "climbing": [("sport", "climbing"), ("leisure", "sports_centre")],
}


async def find_around(
    lat: float,
    lon: float,
    radius_m: int,
    category: str,
    limit: int = 15,
) -> list[dict]:
    """Возвращает список мест: [{name, lat, lon, tags}, ...]."""
    tag_pairs = CATEGORY_TAGS.get(category)
    if not tag_pairs:
        # Кастомная категория формата "key=value"
        if "=" in category:
            k, v = category.split("=", 1)
            tag_pairs = [(k.strip(), v.strip())]
        else:
            return []

    if radius_m > 15000:
        radius_m = 15000  # защита от слишком жирного запроса
    limit = max(1, min(limit, 30))

    # Собираем QL-запрос: union по всем парам тегов
    parts = []
    for k, v in tag_pairs:
        parts.append(f'nwr(around:{radius_m},{lat},{lon})["{k}"="{v}"];')
    union = "(\n" + "\n".join(parts) + "\n);"
    query = f"[out:json][timeout:25];\n{union}\nout center {limit};"

    endpoints = [OVERPASS_PRIMARY] + OVERPASS_FALLBACKS
    for ep in endpoints:
        try:
            async with httpx.AsyncClient(
                timeout=30.0, headers={"User-Agent": USER_AGENT}
            ) as client:
                r = await client.post(ep, data={"data": query})
                r.raise_for_status()
                data = r.json()
                break
        except Exception as e:
            log.warning("overpass.endpoint_failed", endpoint=ep, error=str(e))
            data = None

    if not data:
        return []

    elements = data.get("elements", [])
    results: list[dict] = []
    for el in elements:
        # nwr элементы могут быть node/way/relation; центр для way/relation в .center
        if el.get("type") == "node":
            elat, elon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            elat, elon = center.get("lat"), center.get("lon")
        if elat is None or elon is None:
            continue

        tags = el.get("tags", {}) or {}
        name = tags.get("name") or tags.get("operator") or "(unnamed)"
        results.append(
            {
                "name": name,
                "lat": elat,
                "lon": elon,
                "tags": {k: v for k, v in tags.items() if k in (
                    "name", "opening_hours", "phone", "website", "addr:street",
                    "addr:housenumber", "cuisine", "sport", "leisure", "amenity"
                )},
                "osm_url": f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
            }
        )
    return results
