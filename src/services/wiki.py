"""Wikipedia GeoSearch — достопримечательности и интересные места рядом.
Без ключа, без регистрации. Полезно для свиданий, прогулок, идей."""

import httpx
import structlog

log = structlog.get_logger()

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
USER_AGENT = "personal_agent_telegram_bot/0.1 (single-user)"


async def geo_search(
    lat: float, lon: float, radius_m: int = 5000, limit: int = 10, lang: str = "en"
) -> list[dict]:
    """Возвращает [{title, distance_m, page_url}, ...]."""
    radius_m = max(10, min(radius_m, 10000))  # API max 10km
    limit = max(1, min(limit, 30))

    params = {
        "action": "query",
        "list": "geosearch",
        "gscoord": f"{lat}|{lon}",
        "gsradius": radius_m,
        "gslimit": limit,
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            r = await client.get(WIKI_API.format(lang=lang), params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("wiki.failed", error=str(e), lang=lang)
        return []

    items = data.get("query", {}).get("geosearch", [])
    results: list[dict] = []
    for it in items:
        title = it.get("title", "")
        results.append(
            {
                "title": title,
                "distance_m": it.get("dist"),
                "page_url": f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
            }
        )
    return results
