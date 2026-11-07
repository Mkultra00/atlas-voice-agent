"""Live web lane for emergency turns — keyless sources only, no cloud LLM.

Inference stays 100% local (track 01); this module only fetches DATA:
  - static emergency-number table (offline, instant)
  - Nominatim/OSM  -> nearest hospital / police / pharmacy / embassy
  - Wikipedia API  -> emergency procedures
  - USGS FDSN      -> recent significant earthquakes
Everything is injectable + degrades to "" on any failure. Short timeouts so a
demo turn never hangs on the web.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

FETCH_TIMEOUT = 5.0  # per source, seconds
# Wikimedia rejects demo-style UAs; their UA policy wants a contact point.
UA = ("AtlasVoiceAgent/1.0 "
      "(https://github.com/Mkultra00/atlas-voice-agent; kovyu@agentmail.to)")

EMERGENCY_RE = re.compile(
    r"\b(emergenc\w+|ambulance|hospital|er\b|urgent care|clinic|police|officer|"
    r"fire\b|smoke|burnt?|poison(ing)?|overdose|heart attack|stroke|seizure|"
    r"bleeding|blood|injur\w+|wound\w*|unconscious|not breathing|cpr|"
    r"earthquake|tremor|tsunami|hurricane|typhoon|cyclone|tornado|wildfire|"
    r"flood(ing)?|storm|landslide|"
    r"embass\w+|consulate|passport|robbery|rob(bed)?|stole|stolen|steal|theft|"
    r"pickpocket|assault|attack(ed)?|missing|"
    r"stranded|lock(ed)? out|arrest(ed)?|detained|quarantine|outbreak|"
    r"help me|need help|what (do|should) i do)\b",
    re.I,
)

# Offline-first emergency numbers. Key = lookup token; 112 covers EU + GSM.
NUMBERS: dict[str, str] = {
    "usa": "911",
    "united states": "911",
    "america": "911",
    "canada": "911",
    "mexico": "911",
    "brazil": "190 (police), 192 (ambulance), 193 (fire)",
    "uk": "999 or 112",
    "united kingdom": "999 or 112",
    "britain": "999 or 112",
    "england": "999 or 112",
    "scotland": "999 or 112",
    "ireland": "112 or 999",
    "france": "112 (or 15 ambulance, 17 police, 18 fire)",
    "germany": "112 (or 110 police)",
    "spain": "112",
    "portugal": "112",
    "italy": "112 (or 118 ambulance)",
    "netherlands": "112",
    "greece": "112",
    "turkey": "112",
    "australia": "000",
    "new zealand": "111",
    "japan": "119 (ambulance/fire), 110 (police)",
    "south korea": "119 (ambulance/fire), 112 (police)",
    "china": "120 (ambulance), 110 (police), 119 (fire)",
    "india": "112 (all-in-one)",
    "singapore": "999 (police), 995 (ambulance/fire)",
    "thailand": "191 (police), 1669 (ambulance)",
    "vietnam": "113 (police), 115 (ambulance)",
    "indonesia": "112",
    "philippines": "911",
    "south africa": "10111 (police), 10177 (ambulance)",
    "uae": "999 (police), 998 (ambulance)",
    "dubai": "999 (police), 998 (ambulance)",
    "egypt": "123 (ambulance), 122 (police)",
    "morocco": "19 (police), 15 (ambulance)",
    "kenya": "999 or 112",
    "nigeria": "199 (emergency)",
    "israel": "100 (police), 101 (ambulance)",
    "switzerland": "112",
    "austria": "112",
    "belgium": "112",
    "sweden": "112",
    "norway": "112",
    "denmark": "112",
    "poland": "112",
    "czech": "112",
    "romania": "112",
    "hungary": "112",
    "russia": "112",
    "ukraine": "112",
    "argentina": "911",
    "chile": "133 (police), 131 (ambulance)",
    "colombia": "123",
    "peru": "105 (police), 106 (ambulance)",
}

DEFAULT_NUMBERS = (
    "universal GSM: 112 (works across EU and most of the world); "
    "US/Canada: 911; UK: 999; Australia: 000"
)

FACILITY_KINDS: dict[str, str] = {
    "hospital": "hospital",
    "emergency room": "hospital",
    "er ": "hospital",
    "clinic": "clinic",
    "pharmacy": "pharmacy",
    "pharmacist": "pharmacy",
    "police": "police",
    "embassy": "embassy",
    "consulate": "embassy",
}

_PLACE_IN = re.compile(
    r"\b(?:in|near|around|at|from)\s+"
    r"([A-Z][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’\-]+){0,2})", re.S)

# city → country-token aliases (demo travelers say city names)
CITY_ALIAS: dict[str, str] = {
    "tokyo": "japan", "osaka": "japan", "kyoto": "japan",
    "lisbon": "portugal", "lisboa": "portugal", "porto": "portugal",
    "paris": "france", "london": "uk", "nyc": "usa",
    "new york": "usa", "berlin": "germany", "rome": "italy",
    "madrid": "spain", "sydney": "australia", "seoul": "south korea",
    "bangkok": "thailand", "amsterdam": "netherlands", "barcelona": "spain",
    "hong kong": "999", "istanbul": "turkey", "cairo": "egypt",
}

TRAILING_STOP = {"and", "or", "right", "now", "please"}
# Sentence-opening words the caps fallback would wrongly read as places.
CAPS_STOP = {"there", "this", "that", "what", "help", "please", "hello", "hey",
             "atlas", "okay", "ok", "yes", "sorry", "thank", "thanks", "quick",
             "quickly", "urgent", "im", "its", "call"}


def is_emergency(text: str) -> bool:
    return bool(EMERGENCY_RE.search(text))


def numbers_for(text: str) -> str | None:
    """Static table lookup — country/city token match, longest token wins."""
    t = text.lower()
    # city aliases first (longest alias wins), then country tokens directly
    best: tuple[int, str] | None = None

    def consider(token_len: int, numbers: str) -> None:
        nonlocal best
        if best is None or token_len > best[0]:
            best = (token_len, numbers)

    for city, token in CITY_ALIAS.items():
        if re.search(rf"\b{re.escape(city)}\b", t):
            target = NUMBERS.get(token, token)
            consider(len(city), target)
    for token, numbers in NUMBERS.items():
        if re.search(rf"\b{re.escape(token)}\b", t):
            consider(len(token), numbers)
    return best[1] if best else None


def place_in(text: str) -> str | None:
    """Cheap place extraction: 'in Lisbon', 'near Tokyo Station'."""
    m = _PLACE_IN.search(text)
    if m:
        words = m.group(1).split()
        while words and words[-1].lower() in TRAILING_STOP:
            words.pop()
        if words:
            return " ".join(words)
    caps = re.findall(r"\b[A-Z][a-zà-ÿ]{2,}(?:\s+[A-Z][a-zà-ÿ]{2,}){0,1}", text)
    caps = [c for c in caps if c.split()[0].lower() not in CAPS_STOP]
    return caps[0] if caps else None


def kind_in(text: str) -> str | None:
    t = " " + text.lower() + " "
    for token, kind in FACILITY_KINDS.items():
        if token in t:
            return kind
    return None


def _csv_fields(row: str) -> list[str]:
    fields, buf, quoted = [], "", False
    for ch in row:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            fields.append(buf)
            buf = ""
        else:
            buf += ch
    fields.append(buf)
    return fields


# ---- fetchers (client passed in; trivially injectable for tests) -----------

async def facilities(client: httpx.AsyncClient, place: str, kind: str) -> list[str]:
    async def _query(q: str) -> list[Any]:
        r = await client.get("https://nominatim.openstreetmap.org/search",
                             params={"q": q, "format": "json", "limit": 3},
                             headers={"User-Agent": UA})
        return list(r.json())
    # '<kind> <place>' geocodes near the place directly; 'kind in place' can
    # drift to far-flung namesake towns (Lisbon, ND) or return nothing (Tokyo).
    rows = (await _query(f"{kind} {place}")) or (await _query(f"{kind}, {place}"))
    out: list[str] = []
    for row in rows[:3]:
        name = (row.get("display_name") or "").split(",")[0].strip()
        if name:
            out.append(f"{name} ({row.get('lat')},{row.get('lon')})")
    return out


async def wiki(client: httpx.AsyncClient, query: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", query.lower()).strip()[:120]
    url = (f"https://en.wikipedia.org/w/api.php?action=query&list=search"
           f"&format=json&srlimit=3&srsearch={cleaned}")
    r = await client.get(url, headers={"User-Agent": UA})
    data = r.json()
    out: list[str] = []
    for s in data.get("query", {}).get("search", []):
        snippet = re.sub(r"<[^>]+>", "", s.get("snippet", ""))
        if snippet:
            out.append(f"{s.get('title')}: {snippet[:140]}")
    return out


async def recent_quakes(client: httpx.AsyncClient) -> list[str]:
    url = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv"
           "&minmagnitude=4.5&limit=5&orderby=time")
    r = await client.get(url, headers={"User-Agent": UA})
    rows = r.text.strip().splitlines()[1:]
    out: list[str] = []
    for row in rows[:4]:
        parts = _csv_fields(row)
        if len(parts) < 15:
            continue
        out.append(f"M{parts[4]} {parts[14]} at {parts[0][:16].replace('T', ' ')} UTC")
    return out


# ---- orchestrator -----------------------------------------------------------

async def emergency_block(user_text: str) -> str:
    """Build a LIVE WEB CONTEXT block for emergency turns, or '' otherwise.

    Never raises; caps web work at ~5 s wall; every source failure degrades
    independently so a demo turn always answers.
    """
    if not is_emergency(user_text):
        return ""

    import asyncio

    async def _maybe(coro: Any, out: list) -> None:  # noqa: ANN401
        try:
            res = await coro
            if res:
                out.append(res)
            elif isinstance(res, list):
                out.append(None)
        except Exception:
            out.append(None)

    lines: list[str] = [f'CURRENT QUESTION: "{user_text[:220]}" — answer THIS '
                        "turn's question; do not continue earlier topics."]
    nums = numbers_for(user_text)
    lines.append("EMERGENCY NUMBERS: " + (nums or DEFAULT_NUMBERS))

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            tasks: list = []
            place, kind = place_in(user_text), kind_in(user_text)
            facs: list[Any] = []
            wiki_hits: list[Any] = []
            quakes: list[Any] = []
            if place and kind:
                tasks.append(_maybe(facilities(client, place, kind), facs))
            if re.search(r"\b(earthquake|tremor|tsunami)\b", user_text, re.I):
                tasks.append(_maybe(recent_quakes(client), quakes))
            if not (place and kind) or len(user_text) > 90:
                tasks.append(_maybe(wiki(client, user_text[:120]), wiki_hits))
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.5)
                except asyncio.TimeoutError:
                    pass
            if facs and facs[0] and kind is not None:
                lines.append(f"NEAREST {kind.upper()} near {place} (live OSM): "
                             + "; ".join(facs[0]))
            if quakes and quakes[0]:
                lines.append("RECENT SIGNIFICANT EARTHQUAKES (live USGS): "
                             + "; ".join(quakes[0]))
            if wiki_hits and wiki_hits[0]:
                lines.append("PROCEDURE REFERENCES (live Wikipedia): "
                             + " | ".join(wiki_hits[0]))
    except Exception:
        pass

    if len(lines) <= 2:  # only CURRENT QUESTION + numbers made it through
        return (
            "LIVE WEB CHECK: web sources unreachable this turn.\n"
            + "EMERGENCY NUMBERS: "
            + (nums or DEFAULT_NUMBERS)
            + "\nAnswer from training knowledge; say live sources "
            "are unreachable and keep it short."
        )

    return (
        "LIVE WEB CONTEXT (fetched this turn — you have actually checked, so you "
        "may say 'live sources show...' — cite it concretely, do not invent):\n"
        + "\n".join("- " + ln for ln in lines)
        + "\nPrefer these live facts over training memory; give next actions in "
        "priority order. Do NOT ask clarifying questions before delivering "
        "these facts — open with the most useful next action. "
        "No operator directive this turn."
    )
