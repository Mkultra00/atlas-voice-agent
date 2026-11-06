"""Network helpers and spoken formatters for Atlas (TDD section 4).

Every network call has a 2.5 s total timeout and a fallback chain:
live -> in-memory cache -> data/demo_cache.json -> None (the caller of the
helper then says an honest canned line). Never invent data in a fallback.

Third parties get coordinates and search terms only: never names, phone
numbers or the code word. Logs carry tool, latency, outcome and coordinates
rounded to 3 decimals, never the caller's words.

Endpoint checks, run 2026-11-06 from the build laptop (U4/U5):
- router.project-osrm.org ignores the profile in the URL: /foot/ and /driving/
  returned the identical car route (2009 m, 339.7 s) between Boulevard de
  Magenta and the 10e commissariat. So it is CAR ONLY.
- routing.openstreetmap.de/routed-foot/route/v1/driving/... (FOSSGIS) returned a
  real walking route for the same pair (1093 m, 874.6 s = 1.25 m/s, plausible).
  So walk -> FOSSGIS foot, drive -> FOSSGIS car (same answers as the demo
  server). Walking minutes are still recomputed from distance at 1.2 m/s.
- overpass-api.de answered, but took 25-36 s for the Paris queries, far over
  the 2.5 s budget. The prefetch after confirm_location gets a longer
  background timeout (PREFETCH_TIMEOUT); the foreground call falls back to the
  demo cache.
- Nominatim answered in well under a second with the User-Agent below.
- Tavily: not checked, no TAVILY_API_KEY on the build laptop (U6 still open).
  Request uses time_range "day" per the current docs; "days" is the older form.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp

from voice_guard import speak_number  # re-exported: TDD lists it here

__all__ = [
    "compass",
    "format_ops_text",
    "geocode",
    "haversine_m",
    "osrm_route",
    "overpass_nearby",
    "post_ops_webhook",
    "speak_distance",
    "speak_minutes",
    "speak_number",
    "speak_route",
    "tavily_news",
]

logger = logging.getLogger("safety-tools")

DATA = Path(__file__).resolve().parent.parent / "data"
TIMEOUT_S = 2.5
PREFETCH_TIMEOUT_S = 12.0
WALK_SPEED_MPS = 1.2
DEMO_RADIUS_M = 3000  # demo nearby entries only used near the demo anchor
DEMO_ROUTE_RADIUS_M = 500  # cached routes only valid if the caller is this close

KINDS = ("police", "hospital", "embassy", "hotel")


def _env(name: str, default: str) -> str:
    return os.getenv(name, "") or default


def nominatim_url() -> str:
    return _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")


def overpass_url() -> str:
    return _env("OVERPASS_URL", "https://overpass-api.de/api/interpreter")


def osrm_url(mode: str) -> str:
    if mode == "drive":
        return _env("OSRM_CAR_URL", "https://routing.openstreetmap.de/routed-car")
    return _env("OSRM_FOOT_URL", "https://routing.openstreetmap.de/routed-foot")


def tavily_url() -> str:
    return _env("TAVILY_URL", "https://api.tavily.com/search")


def user_agent() -> str:
    contact = os.getenv("NOMINATIM_CONTACT", "") or "team contact unset"
    return f"Atlas-Hackathon/0.1 (+{contact})"


# ---- demo cache ---------------------------------------------------------------

_demo: dict[str, Any] | None = None


def demo_cache() -> dict[str, Any]:
    global _demo
    if _demo is None:
        try:
            _demo = json.loads((DATA / "demo_cache.json").read_text())
        except Exception:
            logger.warning("demo_cache.json missing or invalid")
            _demo = {}
    return _demo


def norm(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Hôpital' -> 'hopital'."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy_get(table: dict[str, Any], query: str) -> Any | None:
    q = norm(query)
    if not q:
        return None
    best = None
    best_len = 0
    for key, val in table.items():
        if key.startswith("_"):
            continue
        k = norm(key)
        if k and (k in q or q in k) and len(k) > best_len:
            best, best_len = val, len(k)
    return best


# ---- HTTP -----------------------------------------------------------------------


@asynccontextmanager
async def _session():
    """The job's shared session when inside a job, else a short-lived one."""
    from livekit.agents.utils.http_context import http_session

    try:
        shared = http_session()
    except RuntimeError:
        shared = None
    if shared is not None:
        yield shared
    else:
        async with aiohttp.ClientSession() as s:
            yield s


async def _request_json(
    method: str, url: str, *, timeout: float = TIMEOUT_S, **kw: Any
) -> Any:
    """One HTTP call, hard-capped at `timeout` seconds end to end (session
    setup, DNS, TLS and body included, not just aiohttp's own timer)."""
    headers = {"User-Agent": user_agent(), **kw.pop("headers", {})}

    async def go() -> Any:
        async with (
            _session() as s,
            s.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                **kw,
            ) as resp,
        ):
            resp.raise_for_status()
            return await resp.json(content_type=None)

    return await asyncio.wait_for(go(), timeout)


def _log(tool: str, t0: float, outcome: str, lat: float | None = None, lon=None):
    where = f" at={round(lat, 3)},{round(lon, 3)}" if lat is not None else ""
    logger.info(
        "tool=%s latency_ms=%d outcome=%s%s",
        tool,
        (time.monotonic() - t0) * 1000,
        outcome,
        where,
    )


# ---- geometry and spoken formatters -------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_COMPASS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)


def bearing_to_compass(bearing: float) -> str:
    return _COMPASS[int(((bearing % 360) + 22.5) // 45) % 8]


def compass(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """8-point direction from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return bearing_to_compass(math.degrees(math.atan2(x, y)))


_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = [
    "_",
    "_",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]


def int_words(n: int) -> str:
    """0..9999 in words. Spoken numbers keep digit runs out of TTS text, so
    the number guard (which blocks 3+ digit runs) never trips on distances."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f"-{_ONES[o]}" if o else "")
    if n < 1000:
        h, rest = divmod(n, 100)
        return f"{_ONES[h]} hundred" + (f" {int_words(rest)}" if rest else "")
    th, rest = divmod(n, 1000)
    return f"{int_words(th)} thousand" + (f" {int_words(rest)}" if rest else "")


def speak_distance(meters: float) -> str:
    """Under 1 km: rounded to 50 m. Otherwise one decimal km."""
    if meters < 1000:
        m = max(50, round(meters / 50.0) * 50)
        if m >= 1000:
            return "one kilometer"
        return f"{int_words(m)} meters"
    km = round(meters / 1000.0, 1)
    whole, tenth = int(km), round((km - int(km)) * 10)
    if tenth == 10:
        whole, tenth = whole + 1, 0
    unit = "kilometer" if (whole == 1 and tenth == 0) else "kilometers"
    if tenth:
        return f"{int_words(whole)} point {_ONES[tenth]} {unit}"
    return f"{int_words(whole)} {unit}"


def speak_minutes(minutes: int) -> str:
    minutes = max(1, int(minutes))
    return f"{int_words(minutes)} minute" + ("" if minutes == 1 else "s")


def _step_text(step: dict[str, Any], dest_name: str) -> str | None:
    m = step.get("maneuver", {})
    typ = m.get("type", "")
    mod = m.get("modifier", "")
    name = (step.get("name") or "").strip()
    if typ == "arrive":
        side = mod if mod in ("left", "right") else ""
        target = dest_name or "your destination"
        return (
            f"Then {target} is on your {side}." if side else f"Then you reach {target}."
        )
    if not name:
        # Footpaths often have no name. Long unnamed legs still get a turn +
        # distance, from the route itself, so the caller isn't left guessing.
        dist = float(step.get("distance", 0))
        if typ != "depart" and mod and mod != "straight" and dist >= 100:
            return f"Turn {mod.replace('slight ', 'slightly ')} and go {speak_distance(dist)}."
        return None
    if typ == "depart":
        b = m.get("bearing_after")
        heading = bearing_to_compass(b) if isinstance(b, (int, float)) else ""
        return f"Head {heading} on {name}." if heading else f"Start on {name}."
    if typ in ("roundabout", "rotary"):
        return f"At the roundabout, take the exit onto {name}."
    if mod in ("straight",) or typ in ("continue", "new name"):
        return f"Continue onto {name}."
    if mod:
        turn = mod.replace("slight ", "slightly ").replace("sharp ", "sharp ")
        return f"Turn {turn} onto {name}."
    return f"Continue onto {name}."


def speak_route(route: dict[str, Any], mode: str = "walk", dest_name: str = "") -> str:
    """'Four hundred meters, about five minutes on foot. First, head west ...'.

    `route` is the shape osrm_route returns: distance_m, duration_s, steps
    (OSRM step dicts). At most three steps, duplicates and nameless skipped.
    """
    dist = float(route.get("distance_m", 0))
    if mode == "walk":
        minutes = math.ceil(dist / WALK_SPEED_MPS / 60) if dist else 1
        how = "on foot"
    else:
        minutes = math.ceil(float(route.get("duration_s", 0)) / 60) or 1
        how = "by car"
    lines: list[str] = []
    prev_name = ""
    for st in route.get("steps", []):
        name = (st.get("name") or "").strip()
        typ = st.get("maneuver", {}).get("type", "")
        if name and name == prev_name and typ != "arrive":
            continue  # "continue onto the same street" is noise
        txt = _step_text(st, dest_name)
        if txt and (not lines or lines[-1] != txt):
            lines.append(txt)
            prev_name = name or prev_name
    # keep the first two moves plus arrival, max three
    if len(lines) > 3:
        arrive = lines[-1] if lines[-1].startswith("Then") else None
        lines = [*lines[:2], arrive] if arrive else lines[:3]
    if lines:
        lines[0] = "First, " + lines[0][0].lower() + lines[0][1:]
    head = f"{speak_distance(dist)}, about {speak_minutes(minutes)} {how}."
    head = head[0].upper() + head[1:]
    return " ".join([head, *lines])


# ---- geocode (Nominatim) -------------------------------------------------------

_geo_cache: dict[str, dict[str, Any]] = {}
_GEO_CACHE_MAX = 256
_nominatim_locks: dict[int, asyncio.Lock] = {}
_last_nominatim = 0.0
NOMINATIM_SPACING_S = 1.0
_clock = time.monotonic  # patched in tests
_sleep = asyncio.sleep  # patched in tests


def _nominatim_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _nominatim_locks.get(id(loop))
    if lock is None:
        lock = _nominatim_locks[id(loop)] = asyncio.Lock()
    return lock


async def _nominatim_wait() -> None:
    """Keep >= 1 s between Nominatim requests (usage policy). Call under lock."""
    global _last_nominatim
    gap = _clock() - _last_nominatim
    if _last_nominatim and gap < NOMINATIM_SPACING_S:
        await _sleep(NOMINATIM_SPACING_S - gap)
    _last_nominatim = _clock()


def _label_from_nominatim(item: dict[str, Any]) -> str:
    addr = item.get("address", {})
    parts: list[str] = []
    name = item.get("name") or ""
    house = addr.get("house_number", "")
    road = addr.get("road", "")
    for p in (
        name,
        f"{house} {road}".strip() if house else road,
        addr.get("suburb", ""),
        addr.get("city") or addr.get("town") or addr.get("village", ""),
    ):
        if p and p not in parts:
            parts.append(p)
    label = ", ".join(parts) or item.get("display_name", "")
    words = label.split()
    return " ".join(words[:14])


def parse_nominatim(item: dict[str, Any]) -> dict[str, Any]:
    addr = item.get("address", {})
    return {
        "label": _label_from_nominatim(item),
        "lat": float(item["lat"]),
        "lon": float(item["lon"]),
        "country_code": (addr.get("country_code") or "").upper(),
        "city": addr.get("city") or addr.get("town") or addr.get("village", ""),
    }


async def geocode(query: str) -> dict[str, Any] | None:
    """Place text -> {label, lat, lon, country_code, city, source} or None."""
    t0 = time.monotonic()
    key = query.strip().lower()
    if not key:
        return None
    if key in _geo_cache:
        r = _geo_cache[key]
        _log("geocode", t0, "cache", r["lat"], r["lon"])
        return {**r, "source": "cache"}

    async def live() -> Any:
        async with _nominatim_lock():
            await _nominatim_wait()
            return await _request_json(
                "GET",
                nominatim_url(),
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": "1",
                    "addressdetails": "1",
                },
            )

    try:
        # the 1 s spacing wait counts against the same 2.5 s budget
        data = await asyncio.wait_for(live(), TIMEOUT_S)
        if data:
            r = parse_nominatim(data[0])
            if len(_geo_cache) >= _GEO_CACHE_MAX:
                _geo_cache.pop(next(iter(_geo_cache)))
            _geo_cache[key] = r
            _log("geocode", t0, "live", r["lat"], r["lon"])
            return {**r, "source": "live"}
    except Exception as e:
        logger.info("geocode live failed: %s", type(e).__name__)
    hit = _fuzzy_get(demo_cache().get("geocode", {}), query)
    if hit:
        _log("geocode", t0, "demo", hit["lat"], hit["lon"])
        return {**hit, "source": "demo"}
    _log("geocode", t0, "fallback")
    return None


# ---- nearby places (Overpass) ----------------------------------------------------


def overpass_query(lat: float, lon: float, radius_m: int, nationality: str = "") -> str:
    """One query for all kinds. With a known passport, only that country's
    missions, searched wider (10 km): another country's embassy is no help,
    and an unfiltered diplomatic search in Paris returns 80+ elements, which
    got the US embassy truncated out of the result on 2026-11-06."""
    r = int(radius_m)
    a = f"{lat:.5f},{lon:.5f}"
    nat = re.sub(r"[^A-Z]", "", nationality.upper())[:2]
    if nat:
        emb = (
            f'nwr(around:10000,{a})["office"="diplomatic"]["country"="{nat}"];'
            f'nwr(around:10000,{a})["amenity"="embassy"]["country"="{nat}"];'
        )
    else:
        emb = (
            f'nwr(around:{max(r, 4000)},{a})["amenity"="embassy"];'
            f'nwr(around:{max(r, 4000)},{a})["office"="diplomatic"];'
        )
    return (
        "[out:json][timeout:10];("
        f'nwr(around:{r},{a})["amenity"="police"];'
        f'nwr(around:{r},{a})["amenity"="hospital"];'
        f"{emb}"
        f'nwr(around:{min(r, 500)},{a})["tourism"="hotel"];'
        ");out center 150;"
    )


def _kind_of(tags: dict[str, str]) -> str | None:
    if tags.get("amenity") == "police":
        return "police"
    if tags.get("amenity") == "hospital":
        return "hospital"
    if tags.get("amenity") == "embassy" or tags.get("office") == "diplomatic":
        if tags.get("diplomatic", "embassy") in ("embassy", "consulate", ""):
            return "embassy"
        return None
    if tags.get("tourism") == "hotel":
        return "hotel"
    return None


def group_places(
    elements: list[dict[str, Any]],
    lat: float,
    lon: float,
    nationality: str = "",
    keep: int = 4,
) -> dict[str, list[dict[str, Any]]]:
    """Keeps up to `keep` per kind; the tool reads out two, the rest stay in
    the call's cache so get_route can match a place the caller names."""
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for e in elements:
        tags = e.get("tags", {})
        kind = _kind_of(tags)
        if kind is None:
            continue
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if c.get("lat") is None:
            continue
        name = tags.get("name") or tags.get("official_name") or ""
        if not name:
            continue  # unnamed places can't be read out or routed to by name
        out[kind].append(
            {
                "name": name,
                "lat": float(c["lat"]),
                "lon": float(c["lon"]),
                "country": tags.get("country", ""),
                "emergency": tags.get("emergency", ""),
            }
        )
    for kind, places in out.items():
        for p in places:
            p["distance_m"] = round(haversine_m(lat, lon, p["lat"], p["lon"]))
            p["direction"] = compass(lat, lon, p["lat"], p["lon"])
        nat = nationality.upper()

        if kind == "embassy":
            # the ambassador's residence is not a public service point
            places = [p for p in places if not norm(p["name"]).startswith("residence")]
            if nat:
                places = [p for p in places if p.get("country") == nat]
        # Hospitals: ones with an emergency department first (a day clinic is
        # no use to an injured caller), then by distance.
        places.sort(
            key=lambda p, kind=kind: (
                kind == "hospital" and p.get("emergency") != "yes",
                p["distance_m"],
            )
        )
        out[kind] = places[:keep]
    return out


def demo_nearby(lat: float, lon: float, nationality: str = "") -> dict[str, list]:
    """Demo cache places, re-measured from the caller, only near the demo anchor."""
    dc = demo_cache()
    anchor = dc.get("anchor")
    if not anchor:
        return {}
    if haversine_m(lat, lon, anchor["lat"], anchor["lon"]) > DEMO_RADIUS_M:
        return {}
    elements = []
    for kind, places in dc.get("nearby", {}).items():
        if kind.startswith("_"):
            continue
        for p in places:
            tags = {
                "police": {"amenity": "police"},
                "hospital": {"amenity": "hospital"},
                "embassy": {"office": "diplomatic", "diplomatic": "embassy"},
                "hotel": {"tourism": "hotel"},
            }[kind] | {
                "name": p["name"],
                "country": p.get("country", ""),
                "emergency": p.get("emergency", ""),
            }
            elements.append({"tags": tags, "lat": p["lat"], "lon": p["lon"]})
    return group_places(elements, lat, lon, nationality)


async def overpass_nearby(
    lat: float,
    lon: float,
    radius_m: int = 2000,
    nationality: str = "",
    timeout: float = TIMEOUT_S,
) -> dict[str, list[dict[str, Any]]] | None:
    """{kind: [{name, lat, lon, distance_m, direction, country}]} or None."""
    t0 = time.monotonic()
    try:
        data = await _request_json(
            "POST",
            overpass_url(),
            data={"data": overpass_query(lat, lon, radius_m, nationality)},
            timeout=timeout,
        )
        grouped = group_places(data.get("elements", []), lat, lon, nationality)
        _log("overpass_nearby", t0, "live", lat, lon)
        return grouped
    except Exception as e:
        logger.info("overpass live failed: %s", type(e).__name__)
    demo = demo_nearby(lat, lon, nationality)
    if any(demo.values()):
        _log("overpass_nearby", t0, "demo", lat, lon)
        return demo
    _log("overpass_nearby", t0, "fallback", lat, lon)
    return None


# ---- routes (OSRM) -----------------------------------------------------------------


def parse_osrm(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    r = data["routes"][0]
    steps = []
    for leg in r.get("legs", []):
        for s in leg.get("steps", []):
            m = s.get("maneuver", {})
            steps.append(
                {
                    "name": s.get("name", ""),
                    "distance": s.get("distance", 0),
                    "maneuver": {
                        k: m[k] for k in ("type", "modifier", "bearing_after") if k in m
                    },
                }
            )
    return {
        "distance_m": float(r.get("distance", 0)),
        "duration_s": float(r.get("duration", 0)),
        "steps": steps,
    }


def demo_route(
    src: tuple[float, float], dest_name: str, mode: str
) -> dict[str, Any] | None:
    dc = demo_cache()
    anchor = dc.get("anchor")
    if not anchor:
        return None
    if haversine_m(src[0], src[1], anchor["lat"], anchor["lon"]) > DEMO_ROUTE_RADIUS_M:
        return None
    entry = _fuzzy_get(dc.get("routes", {}), dest_name) if dest_name else None
    if not entry:
        return None
    return entry.get(mode) or entry.get("walk")


async def osrm_route(
    src: tuple[float, float],
    dst: tuple[float, float],
    mode: str = "walk",
    dest_name: str = "",
) -> dict[str, Any] | None:
    """{distance_m, duration_s, steps, source} or None. src/dst are (lat, lon)."""
    t0 = time.monotonic()
    base = osrm_url(mode).rstrip("/")
    url = f"{base}/route/v1/driving/{src[1]:.6f},{src[0]:.6f};{dst[1]:.6f},{dst[0]:.6f}"
    try:
        data = await _request_json(
            "GET", url, params={"steps": "true", "overview": "false"}
        )
        r = parse_osrm(data)
        if r:
            _log("osrm_route", t0, "live", src[0], src[1])
            return {**r, "source": "live"}
    except Exception as e:
        logger.info("osrm live failed: %s", type(e).__name__)
    r = demo_route(src, dest_name, mode)
    if r:
        _log("osrm_route", t0, "demo", src[0], src[1])
        return {**r, "source": "demo"}
    _log("osrm_route", t0, "fallback", src[0], src[1])
    return None


# ---- news (Tavily) -------------------------------------------------------------------

_URL = re.compile(r"https?://\S+|www\.\S+")


def _first_sentence(text: str) -> str:
    text = _URL.sub("", text).strip()
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


def news_text(answer: str = "", title: str = "", content: str = "") -> str:
    """Two sentences, <= 40 words, always starting 'Unconfirmed reports say'."""
    if answer:
        body = " ".join(
            _first_sentence(s) for s in re.split(r"(?<=[.!?])\s+", answer)[:2]
        )
    else:
        body = " ".join(x for x in (title.rstrip("."), _first_sentence(content)) if x)
    body = _URL.sub("", body)
    body = re.sub(r"\d{3,}", "", body)  # keep stray digit runs out of TTS
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return ""
    words = body.split()
    if len(words) > 36:
        body = " ".join(words[:36]).rstrip(",;:") + "."
    first = body[0].lower() + body[1:]
    return f"Unconfirmed reports say {first}"


def demo_news(query: str) -> str | None:
    """Placeholder demo news. Only used when ATLAS_DEMO_NEWS=1, because these
    entries are hand-written demo_data, not real reports."""
    if os.getenv("ATLAS_DEMO_NEWS", "0") != "1":
        return None
    q = set(norm(query).split())
    for item in demo_cache().get("news", []):
        if q & set(item.get("keywords", [])):
            return item["text"]
    return None


async def tavily_news(query: str) -> str | None:
    """Spoken 'Unconfirmed reports say ...' text, or None."""
    t0 = time.monotonic()
    key = os.getenv("TAVILY_API_KEY", "")
    if key:
        try:
            data = await _request_json(
                "POST",
                tavily_url(),
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "query": query,
                    "topic": "news",
                    "search_depth": "basic",
                    "max_results": 3,
                    "time_range": "day",
                    "include_answer": True,
                },
            )
            results = data.get("results") or []
            text = news_text(
                data.get("answer") or "",
                results[0].get("title", "") if results else "",
                results[0].get("content", "") if results else "",
            )
            if text:
                _log("tavily_news", t0, "live")
                return text
        except Exception as e:
            logger.info("tavily live failed: %s", type(e).__name__)
    text = demo_news(query)
    if text:
        _log("tavily_news", t0, "demo")
        return text
    _log("tavily_news", t0, "fallback")
    return None


# ---- ops webhook ------------------------------------------------------------------------


def ops_webhook_url() -> str:
    return os.getenv("ATLAS_OPS_WEBHOOK", "") or os.getenv("OPS_WEBHOOK_URL", "")


def ops_format(url: str) -> str:
    fmt = os.getenv("OPS_WEBHOOK_FORMAT", "").strip().lower()
    if fmt in ("slack", "discord", "json"):
        return fmt
    if "hooks.slack.com" in url:
        return "slack"
    if "discord.com/api/webhooks" in url or "discordapp.com" in url:
        return "discord"
    return "json"


def format_ops_text(p: dict[str, Any]) -> str:
    """One-paragraph summary for Slack/Discord cards."""
    loc = p.get("location") or {}
    caller = p.get("caller") or {}
    kind = p.get("type", "atlas.escalation").split(".")[-1]
    bits = [
        f"[Atlas {kind}] incident {p.get('incident_id', '?')}",
        f"urgency {p.get('urgency', '-')}, source {p.get('source', '-')}"
        + (" (update)" if p.get("update") else ""),
        f"reason: {p.get('reason', '-')}",
        f"caller: {caller.get('name') or 'unknown'} ({caller.get('nationality') or '-'})",
    ]
    if loc:
        bits.append(
            f"at {loc.get('label', '')} ({loc.get('lat')}, {loc.get('lon')}) "
            f"{loc.get('country_code', '')}"
        )
    for k in ("situation", "decision", "plan", "check_in_deadline"):
        if p.get(k):
            bits.append(f"{k.replace('_', ' ')}: {p[k]}")
    if p.get("quiet_mode"):
        bits.append("QUIET MODE: caller may not be able to speak")
    return ". ".join(bits)


def wrap_payload(payload: dict[str, Any], fmt: str) -> dict[str, Any]:
    if fmt == "slack":
        return {"text": format_ops_text(payload)}
    if fmt == "discord":
        return {"content": format_ops_text(payload)[:1900]}
    # json: the full object, plus the flat fields the local ops desk
    # (frontend ops_desk.py: event, call_id, message, ts, location) requires.
    kind = str(payload.get("type", "atlas.escalation")).split(".")[-1]
    loc = payload.get("location") or {
        "label": payload.get("hotel_on_file") or "location not confirmed",
        "lat": None,
        "lon": None,
    }
    return {
        **payload,
        "event": kind,
        "call_id": payload.get("incident_id", ""),
        "message": format_ops_text(payload),
        "ts": payload.get("at", ""),
        "location": loc,
    }


def ops_log_path() -> Path:
    return Path(os.getenv("ATLAS_OPS_LOG", "") or "ops_log.jsonl")


def append_ops_log(payload: dict[str, Any], status: str) -> None:
    """Local demo record of alerts that did not reach the desk (gitignored)."""
    try:
        with ops_log_path().open("a") as f:
            f.write(json.dumps({"status": status, **payload}) + "\n")
    except Exception:
        logger.warning("could not write ops log")


async def _post_once(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> int:
    async with (
        _session() as s,
        s.post(
            url,
            json=body,
            headers={"User-Agent": user_agent(), **headers},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp,
    ):
        return resp.status


async def post_ops_webhook(
    payload: dict[str, Any],
    *,
    attempts: int = 3,
    timeout: float = TIMEOUT_S,
    deadline_s: float = 4.0,
    backoff_s: float = 0.25,
) -> bool:
    """POST the payload to ATLAS_OPS_WEBHOOK. True only on an HTTP 2xx.

    Up to `attempts` tries within `deadline_s` total, so a tool call never
    blocks the caller for long. The same Idempotency-Key header is sent on
    every retry of one alert, so a receiver that honors it won't duplicate.
    No URL set -> honest log-only mode, returns False.
    """
    url = ops_webhook_url()
    if not url:
        logger.warning(
            "ops webhook unset: log-only mode incident_id=%s",
            payload.get("incident_id"),
        )
        return False
    body = wrap_payload(payload, ops_format(url))
    headers = {"Idempotency-Key": str(payload.get("idempotency_key", ""))}
    start = time.monotonic()
    for i in range(max(1, attempts)):
        left = deadline_s - (time.monotonic() - start)
        if i > 0 and left <= 0.05:
            break
        per_try = min(timeout, max(left, 0.1))
        try:
            status = await asyncio.wait_for(
                _post_once(url, body, headers, per_try), per_try
            )
            if 200 <= status < 300:
                logger.info(
                    "ops webhook delivered incident_id=%s attempt=%d",
                    payload.get("incident_id"),
                    i + 1,
                )
                return True
            logger.warning("ops webhook status=%d attempt=%d", status, i + 1)
        except Exception as e:
            logger.warning("ops webhook error=%s attempt=%d", type(e).__name__, i + 1)
        if i + 1 < attempts:
            await asyncio.sleep(backoff_s * (2**i))
    return False
