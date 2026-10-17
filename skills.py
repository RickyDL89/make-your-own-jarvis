"""EDI's skills: the things she can do besides talk.

- Launcher: "open Excel", "open my downloads folder", "open youtube.com".
- Memory: "remember that ...", "what do you remember", "forget ...". Survives restarts.
- Live info: weather (Open-Meteo), exchange rates (open.er-api.com), news (Google News RSS),
  facts (Wikipedia) and, with a free BRAVE_API_KEY in .env, real web search (Brave).
- Outlook: today's calendar and unread mail, read from classic Outlook on Windows.

Standard library only, like jarvis.py. handle() looks at what the user said and returns either
a reply EDI can say right away, or live data for the brain to answer with. See jarvis.py.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEMORY_FILE = ROOT / "memory.json"
APPS_FILE = ROOT / "apps.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EDI/1.0"


def log(message: str) -> None:
    print(f"  {time.strftime('%H:%M:%S')}  {message}", flush=True)


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _fetch(url: str, *, headers: dict | None = None, timeout: int = 12) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https APIs below
        return response.read()


def _fetch_json(url: str, timeout: int = 12) -> dict:
    return json.loads(_fetch(url, headers={"Accept": "application/json"}, timeout=timeout))


@dataclass
class Result:
    name: str
    reply: str | None = None    # say this right away, no brain needed
    context: str | None = None  # live data: hand it to the brain and let her answer


# ---------------------------------------------------------------- memory

def load_memory() -> dict:
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"facts": [], "history": []}
    if not isinstance(data, dict):
        return {"facts": [], "history": []}
    data.setdefault("facts", [])
    data.setdefault("history", [])
    return data


def save_memory(memory: dict) -> None:
    tmp = MEMORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MEMORY_FILE)


def load_history() -> list[dict]:
    history = load_memory()["history"]
    return [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")]


def save_history(history: list[dict]) -> None:
    memory = load_memory()
    memory["history"] = history
    save_memory(memory)


def memory_prompt() -> str:
    facts = load_memory()["facts"]
    if not facts:
        return ""
    return "Things the user asked you to remember: " + " | ".join(str(f) for f in facts) + ". "


# "remember that X" and "remember my X" are instructions. "remember when/how/what..." is a question for the brain.
REMEMBER_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?remember(?:\s+that)?\s+(?!(?:when|how|what|where|who|why|if|whether)\b)(.+?)[.!?]*$", re.I)
FORGET_ALL_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?forget\s+(?:everything|it all|all of it|all your memories)", re.I)
FORGET_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?forget\s+(?:that\s+|about\s+)?(.+?)[.!?]*$", re.I)
RECALL_RE = re.compile(r"what (?:do you|have you) remember|what do you know about me|list your memories", re.I)


def memory_skill(text: str, you: str) -> Result | None:
    if RECALL_RE.search(text):
        facts = load_memory()["facts"]
        if not facts:
            return Result("memory", reply=f"Nothing yet, {you}. Tell me to remember something and I will.")
        return Result("memory", context="Things the user asked you to remember, read them back naturally: "
                      + "; ".join(str(f) for f in facts))
    if FORGET_ALL_RE.match(text):
        memory = load_memory()
        memory["facts"] = []
        save_memory(memory)
        return Result("memory", reply=f"Done, {you}. My memory is clear.")
    m = REMEMBER_RE.match(text)
    if m:
        fact = m.group(1).strip()
        memory = load_memory()
        memory["facts"] = ([f for f in memory["facts"] if _words(f) != _words(fact)] + [fact])[-50:]
        save_memory(memory)
        return Result("memory", reply=f"Noted, {you}. I'll remember that.")
    m = FORGET_RE.match(text)
    if m:
        needle = _words(m.group(1))
        memory = load_memory()
        keep = [f for f in memory["facts"] if needle not in _words(f)]
        dropped = len(memory["facts"]) - len(keep)
        if not dropped:
            return Result("memory", reply=f"I don't have anything about that, {you}.")
        memory["facts"] = keep
        save_memory(memory)
        return Result("memory", reply=f"Forgotten, {you}.")
    return None


# ---------------------------------------------------------------- launcher

OPEN_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?(?:can you\s+)?(?:open|launch|start|run|fire up|bring up)\s+(?:up\s+)?(.+?)[.!?]*$", re.I)
KNOWN_FOLDERS = {
    "downloads": "Downloads", "documents": "Documents", "desktop": "Desktop",
    "pictures": "Pictures", "photos": "Pictures", "music": "Music", "videos": "Videos",
}
WINDOWS_APPS = {
    "calculator": "calc.exe", "notepad": "notepad.exe", "paint": "mspaint.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "files": "explorer.exe",
    "settings": "ms-settings:", "task manager": "taskmgr.exe", "command prompt": "cmd.exe",
    "terminal": "wt.exe", "powershell": "powershell.exe", "camera": "microsoft.windows.camera:",
    "outlook": "outlook.exe", "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
    "control panel": "control.exe", "recycle bin": "shell:RecycleBinFolder",
}

_shortcuts: tuple[float, list[Path]] = (0.0, [])


def start_menu_shortcuts() -> list[Path]:
    global _shortcuts
    if time.time() - _shortcuts[0] < 120:
        return _shortcuts[1]
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("AppData", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    found = [p for root in roots if root.is_dir() for p in root.rglob("*.lnk") if "uninstall" not in p.stem.lower()]
    _shortcuts = (time.time(), found)
    return found


def find_shortcut(name: str) -> Path | None:
    want = _words(name)
    best, best_score = None, 0
    for path in start_menu_shortcuts():
        stem = _words(path.stem)
        if stem == want:
            score = 4
        elif stem.startswith(want + " "):
            score = 3
        elif f" {want} " in f" {stem} ":
            score = 2
        elif want in stem.replace(" ", ""):
            score = 1
        else:
            continue
        if score > best_score:
            best, best_score = path, score
    return best


def load_apps() -> dict[str, str]:
    try:
        data = json.loads(APPS_FILE.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {_words(k): str(v) for k, v in data.items() if isinstance(data, dict) and not k.startswith("_") and v}


def resolve_target(name: str) -> tuple[str, str] | None:
    """Turn 'excel' or 'my downloads folder' into (what to open, how to describe it)."""
    clean = re.sub(r"^(?:my|the)\s+", "", name.strip(), flags=re.I)
    clean = re.sub(r"\s+(?:folder|app|application|program|website|site|page)$", "", clean, flags=re.I)
    key = _words(clean)
    if not key:
        return None
    apps = load_apps()
    if key in apps:
        return apps[key], clean
    if key in KNOWN_FOLDERS:
        folder = Path.home() / KNOWN_FOLDERS[key]
        return (str(folder), f"your {clean} folder") if folder.is_dir() else None
    if os.name == "nt" and key in WINDOWS_APPS:
        return WINDOWS_APPS[key], clean
    if re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?", clean):
        url = clean if clean.startswith("http") else f"https://{clean}"
        return url, clean
    if re.fullmatch(r"[a-z]:\\.+|~.+|/.+", clean, flags=re.I) and Path(os.path.expanduser(clean)).exists():
        return str(Path(os.path.expanduser(clean))), Path(clean).name
    if os.name == "nt":
        shortcut = find_shortcut(clean)
        if shortcut:
            return str(shortcut), shortcut.stem
    exe = shutil.which(clean)
    if exe:
        return exe, clean
    return None


def launch(target: str) -> None:
    if re.match(r"https?://", target):
        webbrowser.open(target)
    elif os.name == "nt":
        os.startfile(target)  # noqa: S606 - the user asked to open it
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603


def launcher_skill(text: str, you: str) -> Result | None:
    m = OPEN_RE.match(text)
    if not m:
        return None
    resolved = resolve_target(m.group(1))
    if not resolved:
        return None  # not something we can open, let the brain answer the question
    target, label = resolved
    try:
        launch(target)
    except OSError as err:
        log(f"Couldn't open {target}: {err}")
        return Result("launcher", reply=f"I couldn't open {label}, {you}. Windows wouldn't let me.")
    log(f"Opened {target}")
    return Result("launcher", reply=f"Opening {label}, {you}.")


# ---------------------------------------------------------------- weather (Open-Meteo, free, no key)

WEATHER_RE = re.compile(
    r"\b(weather|forecast|temperature|rain(?:ing|y)?|snow(?:ing)?|umbrella|sunny|windy|humid(?:ity)?|"
    r"how (?:hot|cold|warm) (?:is|will) it|degrees (?:out|outside))\b", re.I)
WEATHER_PLACE_RE = re.compile(r"\b(?:in|for|at)\s+([a-z][a-z\s'.-]{1,40}?)(?:\s+(?:today|tomorrow|tonight|this week|now|right now|later))?[?.!]*$", re.I)
WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}
_home: dict | None = None


def geocode(place: str) -> dict | None:
    data = _fetch_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&name=" + urllib.parse.quote(place))
    hits = data.get("results") or []
    if not hits:
        return None
    hit = hits[0]
    name = ", ".join(x for x in (hit.get("name"), hit.get("admin1"), hit.get("country")) if x)
    return {"lat": hit["latitude"], "lon": hit["longitude"], "name": name}


def home_location(cfg: dict[str, str]) -> dict | None:
    """Where the user is: EDI_LOCATION from .env, or a rough fix from the internet connection."""
    global _home
    if _home:
        return _home
    if cfg.get("EDI_LOCATION"):
        _home = geocode(cfg["EDI_LOCATION"])
        if _home:
            return _home
    try:
        data = _fetch_json("http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if data.get("status") != "success":
        return None
    _home = {"lat": data["lat"], "lon": data["lon"],
             "name": ", ".join(x for x in (data.get("city"), data.get("country")) if x)}
    return _home


def weather_skill(cfg: dict[str, str], text: str, you: str) -> Result | None:
    if not WEATHER_RE.search(text):
        return None
    place = None
    m = WEATHER_PLACE_RE.search(text)
    if m and _words(m.group(1)) not in ("the morning", "the afternoon", "the evening", "the weekend", "the week", "the day", "a bit", "the moment"):
        try:
            place = geocode(m.group(1).strip())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            place = None
    try:
        loc = place or home_location(cfg)
        if not loc:
            return Result("weather", reply=f"I can't tell where you are, {you}. Set EDI_LOCATION in the .env file to your city.")
        data = _fetch_json(
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode({
                "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 4,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            }))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as err:
        log(f"Weather lookup failed: {err}")
        return Result("weather", reply=f"The weather service isn't answering right now, {you}.")
    cur, daily = data["current"], data["daily"]
    lines = [f"Live weather for {loc['name']} right now: {round(cur['temperature_2m'])}°C, "
             f"{WMO.get(cur['weather_code'], 'mixed')}, feels like {round(cur['apparent_temperature'])}°C, "
             f"humidity {cur['relative_humidity_2m']}%, wind {round(cur['wind_speed_10m'])} km/h."]
    labels = ["Today", "Tomorrow", "The day after tomorrow", "In three days"]
    for i, label in enumerate(labels[:len(daily["time"])]):
        lines.append(f"{label} ({daily['time'][i]}): high {round(daily['temperature_2m_max'][i])}°C, "
                     f"low {round(daily['temperature_2m_min'][i])}°C, {WMO.get(daily['weather_code'][i], 'mixed')}, "
                     f"{daily['precipitation_probability_max'][i] or 0}% chance of rain.")
    return Result("weather", context=" ".join(lines) + " Answer the user's actual question from this, briefly.")


# ---------------------------------------------------------------- web search (DuckDuckGo, Wikipedia fallback)

SEARCH_EXPLICIT_RE = re.compile(
    r"^(?:edi,?\s+)?(?:please\s+)?(?:can you\s+)?(?:search(?: the (?:web|internet))?(?: for)?|look up|google|find out(?: about)?|"
    r"what does the internet say about)\s+(.+?)[.!?]*$", re.I)
# Questions the brain can't know the answer to from training alone. Kept narrow on purpose:
# plain chat ("how are you today", "what is the capital of France") must not trigger a search.
LIVE_RE = re.compile(
    r"\b(latest|news|headlines|current(?:ly)?|right now|recent(?:ly)?|this (?:week|month|year)|"
    r"price of|cost of|how much (?:is|does|are)|score|who won|when (?:is|does) the next|release date|"
    r"what(?:'s| is) happening|what happened|stock|share price|exchange rate|bitcoin|update on|"
    r"trending|new(?:est)? (?:version|release|model|phone|update))\b", re.I)
DATE_TIME_RE = re.compile(r"\b(what(?:'s| is) the (?:date|time|day)|what day is it|what time is it)\b", re.I)


def _strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
NEWS_RE = re.compile(r"\b(news|headlines|latest|what(?:'s| is) happening|what happened|update on|trending)\b", re.I)


def _rss_items(xml: str, limit: int) -> list[dict[str, str]]:
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S)[:limit]:
        fields = {}
        for tag in ("title", "description", "pubDate", "source"):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
            fields[tag] = _strip_tags(re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip())) if m else ""
        items.append(fields)
    return items


def brave(query: str, api_key: str) -> list[tuple[str, str]]:
    """Real web search. Needs a free key from https://brave.com/search/api (2,000 searches a month at $0)."""
    data = json.loads(_fetch("https://api.search.brave.com/res/v1/web/search?count=5&text_decorations=0&q=" + urllib.parse.quote(query),
                             headers={"Accept": "application/json", "X-Subscription-Token": api_key}))
    hits = (data.get("web") or {}).get("results") or []
    return [(_strip_tags(h.get("title", "")), _strip_tags(h.get("description", ""))) for h in hits if h.get("title")][:5]


def google_news(query: str) -> list[tuple[str, str]]:
    xml = _fetch("https://news.google.com/rss/search?hl=en&q=" + urllib.parse.quote(query)).decode("utf-8", "replace")
    out = []
    for item in _rss_items(xml, 6):
        title = re.sub(r"\s+-\s+[^-]+$", "", item["title"])  # Google appends " - Source" to the headline
        when = item["pubDate"][:16]  # "Fri, 16 Oct 2026"
        out.append((title, f"{item['source'] or 'news'}, {when}".strip(", ")))
    return out


def wikipedia(query: str) -> list[tuple[str, str]]:
    data = _fetch_json("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3&srsearch="
                       + urllib.parse.quote(query))
    return [(hit["title"], _strip_tags(hit.get("snippet", ""))) for hit in data.get("query", {}).get("search", [])]


def search_skill(cfg: dict[str, str], text: str, you: str) -> Result | None:
    explicit = SEARCH_EXPLICIT_RE.match(text)
    if explicit:
        query = explicit.group(1).strip()
    elif LIVE_RE.search(text) and not DATE_TIME_RE.search(text) and len(text.split()) >= 3:
        query = text.strip()
    else:
        return None
    results: list[tuple[str, str]] = []
    source = ""
    # The big search engines block scripts without a key, so: Brave if the user added a key,
    # Google News for anything current (it covers sport, prices and events too), Wikipedia for facts.
    backends: list[tuple[str, object]] = []
    if cfg.get("BRAVE_API_KEY"):
        backends.append(("Brave", lambda q: brave(q, cfg["BRAVE_API_KEY"])))
    backends += [("Google News", google_news), ("Wikipedia", wikipedia)]
    if NEWS_RE.search(text):
        backends.sort(key=lambda b: b[0] != "Google News")  # headlines first when they ask for news
    for source, backend in backends:
        try:
            results = backend(query)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            log(f"{source} failed: {err}")
        if results:
            break
    if not results:
        if explicit:
            return Result("search", reply=f"The search came back empty, {you}. Try different words.")
        return None  # let the brain answer from what it knows
    log(f"Searched {source} for: {query}")
    body = " ".join(f"{i}. {title} ({snippet})" if source == "Google News" else f"{i}. {title}: {snippet}"
                    for i, (title, snippet) in enumerate(results, 1))
    return Result("search", context=f"Live results from {source} for \"{query}\": {body} "
                  "Answer the user's question from these in one or two spoken sentences, like a briefing. Never read out URLs. "
                  "If the results don't answer it, say what you do know and that the search didn't help.")


# ---------------------------------------------------------------- exchange rates (open.er-api.com, free, no key)

CURRENCIES = {
    "rand": "ZAR", "rands": "ZAR", "zar": "ZAR", "dollar": "USD", "dollars": "USD", "usd": "USD", "bucks": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "gbp": "GBP",
    "yen": "JPY", "yuan": "CNY", "rupee": "INR", "rupees": "INR", "australian dollar": "AUD", "canadian dollar": "CAD",
    "swiss franc": "CHF", "dirham": "AED", "pula": "BWP", "naira": "NGN", "shilling": "KES", "kwacha": "ZMW",
}
FX_RE = re.compile(r"\b(exchange rate|forex|how (?:strong|weak) is the (?:rand|dollar|euro|pound)|"
                   r"(?:rand|dollar|euro|pound|yen|yuan|rupee|franc|dirham|pula|naira|shilling|kwacha)s?\s+(?:to|in|against|versus|vs)\s+"
                   r"(?:the\s+)?(?:rand|dollar|euro|pound|yen|yuan|rupee|franc|dirham|pula|naira|shilling|kwacha)|"
                   r"how much is (?:a|one|the) (?:rand|dollar|euro|pound|yen|yuan|rupee|franc)|what(?:'s| is) the (?:rand|dollar|euro|pound) (?:doing|at|trading at))\b", re.I)
_fx_cache: dict[str, tuple[float, dict]] = {}


def fx_rates(base: str) -> dict:
    cached = _fx_cache.get(base)
    if cached and time.time() - cached[0] < 1800:
        return cached[1]
    data = _fetch_json(f"https://open.er-api.com/v6/latest/{base}")
    if data.get("result") != "success":
        raise ValueError(str(data.get("error-type") or "no rates"))
    _fx_cache[base] = (time.time(), data)
    return data


def fx_skill(text: str, you: str) -> Result | None:
    if not FX_RE.search(text):
        return None
    lowered = text.lower()
    mentioned = []
    for name in sorted(CURRENCIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", lowered) and CURRENCIES[name] not in mentioned:
            mentioned.append(CURRENCIES[name])
    if len(mentioned) >= 2:
        base, quote = mentioned[0], mentioned[1]
    elif mentioned == ["ZAR"]:
        base, quote = "USD", "ZAR"
    elif mentioned:
        base, quote = mentioned[0], "ZAR"
    else:
        base, quote = "USD", "ZAR"
    try:
        data = fx_rates(base)
        rate = data["rates"][quote]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as err:
        log(f"Exchange rate lookup failed: {err}")
        return Result("fx", reply=f"The exchange rate service isn't answering right now, {you}.")
    # "Rand dollar rate" means how many rand one dollar buys: always quote the stronger currency as 1.
    if rate < 1:
        base, quote, rate = quote, base, 1 / rate
    updated = data.get("time_last_update_utc", "")[:16]
    spoken = {"ZAR": "rand", "USD": "US dollar", "EUR": "euro", "GBP": "British pound", "JPY": "Japanese yen", "CNY": "Chinese yuan",
              "INR": "Indian rupee", "AUD": "Australian dollar", "CAD": "Canadian dollar", "CHF": "Swiss franc", "AED": "dirham",
              "BWP": "pula", "NGN": "naira", "KES": "Kenyan shilling", "ZMW": "kwacha"}
    return Result("fx", context=f"Live exchange rate from open.er-api.com, updated {updated}: one {spoken.get(base, base)} "
                  f"is {rate:.2f} {spoken.get(quote, quote)}. Tell the user exactly that figure, in one short sentence. "
                  "Do not invert it, do not use currency codes.")


# ---------------------------------------------------------------- Google: calendar (.ics) and Gmail (Atom feed)

import google  # noqa: E402 - local module, kept next to the skill that uses it

NEXT_MEETING_RE = re.compile(r"\b(next (?:meeting|appointment|event|thing)|what(?:'s| is) next (?:on my calendar|today|up)|when(?:'s| is) my next)\b", re.I)
FREE_RE = re.compile(r"\b(am i free|do i have (?:anything|something) on|is my (?:morning|afternoon|evening|day) free|any (?:gaps|free time))\b", re.I)
MAIL_FROM_RE = re.compile(r"\b(?:anything|any ?thing|mail|e-?mails?|messages?|news|heard|word)\s+from\s+([A-Za-z][\w.'-]*(?:\s+[A-Z][\w.'-]*)?)", re.I)
LATEST_MAIL_RE = re.compile(r"\b(latest|last|newest|most recent|top)\s+(?:e-?mail|message|mail)\b|\bread (?:me )?(?:my|the) (?:latest|last|newest) (?:e-?mail|message)", re.I)


def _calendar_window(text: str) -> tuple[datetime, datetime, str]:
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lowered = text.lower()
    if "tomorrow" in lowered:
        return today + timedelta(days=1), today + timedelta(days=2), "tomorrow"
    if "this week" in lowered or "the week" in lowered or "next 7 days" in lowered or "next seven days" in lowered:
        return now, today + timedelta(days=7), "the next seven days"
    if "next week" in lowered:
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=7), "next week"
    if "weekend" in lowered:
        start = today + timedelta(days=(5 - today.weekday()) % 7)
        return start, start + timedelta(days=2), "the weekend"
    for name, idx in (("monday", 0), ("tuesday", 1), ("wednesday", 2), ("thursday", 3), ("friday", 4), ("saturday", 5), ("sunday", 6)):
        if name in lowered:
            start = today + timedelta(days=(idx - today.weekday()) % 7 or (0 if idx == today.weekday() and "next" not in lowered else 7))
            return start, start + timedelta(days=1), name.capitalize()
    if "afternoon" in lowered:
        return today.replace(hour=12), today.replace(hour=18), "this afternoon"
    if "evening" in lowered or "tonight" in lowered:
        return today.replace(hour=17), today + timedelta(days=1), "this evening"
    if "morning" in lowered:
        return today, today.replace(hour=12), "this morning"
    return now if NEXT_MEETING_RE.search(text) else today, today + timedelta(days=1), "today"


def google_skill(cfg: dict[str, str], text: str, you: str) -> Result | None:
    wants_cal = bool(CALENDAR_RE.search(text) or NEXT_MEETING_RE.search(text) or FREE_RE.search(text))
    wants_mail = bool(MAIL_RE.search(text) or LATEST_MAIL_RE.search(text) or MAIL_FROM_RE.search(text))
    if not (wants_cal or wants_mail):
        return None
    have_cal, have_mail = google.configured_calendar(cfg), google.configured_mail(cfg)
    if not ((wants_cal and have_cal) or (wants_mail and have_mail)):
        return None  # not set up: let the Outlook skill have a go, or say what's missing
    parts = []
    try:
        if wants_cal and have_cal:
            start, end, when = _calendar_window(text)
            events = google.calendar_events(cfg, start, end, fresh=bool(FRESH_RE.search(text)))
            if NEXT_MEETING_RE.search(text):
                upcoming = [e for e in events if e["end"] > datetime.now()][:3]
                parts.append(google.describe_events(upcoming, "the rest of today") if upcoming else "Nothing more is on the user's calendar today.")
            else:
                parts.append(google.describe_events(events, when))
        if wants_mail and have_mail:
            feed = google.gmail_unread(cfg)
            m = MAIL_FROM_RE.search(text)
            parts.append(google.describe_mail(feed, m.group(1).strip() if m else ""))
    except urllib.error.HTTPError as err:
        log(f"Google: HTTP {err.code} {err.reason} ({err.url[:60] if err.url else ''}...)")
        if err.code == 404 and "/calendar/ical/" in (err.url or ""):
            if "/public/" in err.url:
                return Result("google", reply=f"That calendar address is the public one, {you}, which Google won't serve. In Google Calendar settings, copy the Secret address in iCal format instead, the one with private in it, and put that in the .env file.")
            return Result("google", reply=f"Google says that calendar address doesn't exist, {you}. Copy the Secret address in iCal format again from Google Calendar settings into the .env file.")
        if err.code in (401, 403):
            if "mail.google.com" in (err.url or ""):
                return Result("google", reply=f"Gmail turned me away, {you}. Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in the .env file; the app password needs 2-step verification on.")
            return Result("google", reply=f"Google turned me away, {you}. Check the calendar address in the .env file.")
        return Result("google", reply=f"Google isn't answering right now, {you}.")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        log(f"Google: {err!r}")
        return Result("google", reply=f"I couldn't reach Google just now, {you}.")
    return Result("google", context="Live data from the user's Google account. " + " ".join(parts)
                  + " Answer the question from this, briefly and naturally, in local time, without reading out brackets or calendar names unless asked.")


# ---------------------------------------------------------------- Outlook (classic Outlook on Windows, via COM)

CALENDAR_RE = re.compile(
    r"\b(calendar|schedule|meetings?|appointments?|agenda|"
    r"what(?:'s| is| do i have|'ve i got| have i got)\s+(?:on|planned|happening|scheduled|lined up|coming up)(?:\s+for)?\s+(?:today|tomorrow|tonight|this (?:week|morning|afternoon|evening|weekend)|next week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|my plate)|"
    r"anything (?:on|planned|happening|scheduled)(?:\s+for)?\s+(?:today|tomorrow|tonight|this week|the weekend)|"
    r"(?:how does|what does|how's) my (?:day|week) look|my day|free (?:today|tomorrow|this afternoon|tonight))\b", re.I)
FRESH_RE = re.compile(r"\b(again|re-?check|refresh|double[- ]check|look again|just added|just put|just created|re-?load)\b", re.I)
MAIL_RE = re.compile(r"\b(e-?mails?|inbox|unread|mail|messages)\b", re.I)

OUTLOOK_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
try {
  $ol = New-Object -ComObject Outlook.Application
  $ns = $ol.GetNamespace('MAPI')
  $out = @{}
  if ($mode -ne 'mail') {
    $cal = $ns.GetDefaultFolder(9).Items
    $cal.IncludeRecurrences = $true
    $cal.Sort('[Start]')
    $start = (Get-Date).Date.AddDays($offset)
    $end = $start.AddDays($days)
    $items = $cal.Restrict("[Start] < '" + $end.ToString('g') + "' AND [End] > '" + $start.ToString('g') + "'")
    $list = @(); $n = 0
    foreach ($i in $items) {
      if ($n -ge 30) { break }
      $list += @{ subject = $i.Subject; start = $i.Start.ToString('ddd HH:mm'); end = $i.End.ToString('HH:mm'); location = $i.Location; allday = $i.AllDayEvent }
      $n++
    }
    $out.calendar = $list
  }
  if ($mode -ne 'calendar') {
    $inbox = $ns.GetDefaultFolder(6)
    $out.unread = $inbox.UnReadItemCount
    $items = $inbox.Items.Restrict('[UnRead] = true')
    $items.Sort('[ReceivedTime]', $true)
    $list = @(); $n = 0
    foreach ($m in $items) {
      if ($n -ge 8) { break }
      $list += @{ from = $m.SenderName; subject = $m.Subject; received = $m.ReceivedTime.ToString('ddd HH:mm') }
      $n++
    }
    $out.mail = $list
  }
  $out | ConvertTo-Json -Depth 4 -Compress
} catch {
  @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""


_outlook_failed_at = 0.0


def outlook_signed_in() -> bool:
    """Classic Outlook needs a mail profile. Without one, COM hangs for a minute and then says 'not connected'."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Office\16.0\Outlook\Profiles") as key:
            return winreg.QueryInfoKey(key)[0] > 0
    except OSError:
        return False


def outlook_query(mode: str, offset: int, days: int) -> dict:
    global _outlook_failed_at
    if os.name != "nt":
        return {"error": "Outlook summaries only work on Windows with classic Outlook installed."}
    if not outlook_signed_in():
        return {"error": "not signed in", "hint": "Classic Outlook has no account on this computer. Open classic Outlook once and sign in, then I can read your calendar and inbox."}
    if time.time() - _outlook_failed_at < 600:
        return {"error": "Outlook failed a moment ago, not retrying yet."}
    script = f"$mode = '{mode}'; $offset = {offset}; $days = {days}\n" + OUTLOOK_SCRIPT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(  # noqa: S603 - our own script, no user input in it
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"error": "Outlook took too long to answer."}
    except OSError as err:
        return {"error": str(err)}
    raw = proc.stdout.decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
    except json.JSONDecodeError:
        data = {}
    if not data:
        data = {"error": (proc.stderr.decode("utf-8", "replace").strip() or "Outlook gave no answer.")[:200]}
    if data.get("error"):
        _outlook_failed_at = time.time()
    return data


def outlook_skill(text: str, you: str) -> Result | None:
    wants_cal = bool(CALENDAR_RE.search(text))
    wants_mail = bool(MAIL_RE.search(text))
    if not (wants_cal or wants_mail):
        return None
    lowered = text.lower()
    offset, days, when = 0, 1, "today"
    if "tomorrow" in lowered:
        offset, when = 1, "tomorrow"
    elif "this week" in lowered or "the week" in lowered:
        days, when = 7, "the next seven days"
    mode = "both" if (wants_cal and wants_mail) else "calendar" if wants_cal else "mail"
    data = outlook_query(mode, offset, days)
    if data.get("error"):
        log(f"Outlook: {data['error']}")
        if data.get("hint"):
            return Result("outlook", reply=f"I can't see your calendar or mail yet, {you}. Put your Google Calendar's secret address and a Gmail app password in the .env file, or sign in to classic Outlook, and I can.")
        return Result("outlook", reply=f"I couldn't reach Outlook, {you}. The reason is in the EDI window.")
    parts = []
    if "calendar" in data:
        items = data["calendar"] or []
        if not items:
            parts.append(f"The user's Outlook calendar for {when} is empty.")
        else:
            entries = "; ".join(
                f"{'all day' if i.get('allday') else i.get('start', '') + '-' + i.get('end', '')} {i.get('subject') or 'untitled'}"
                + (f" at {i['location']}" if i.get("location") else "") for i in items)
            parts.append(f"The user's Outlook calendar for {when} ({len(items)} items): {entries}.")
    if "mail" in data:
        items = data["mail"] or []
        unread = data.get("unread", len(items))
        newest = "; ".join(f"{m.get('from') or 'unknown'}: {m.get('subject') or 'no subject'} ({m.get('received', '')})" for m in items)
        parts.append(f"The user's Outlook inbox has {unread} unread emails." + (f" Newest unread: {newest}." if newest else ""))
    return Result("outlook", context="Live data from the user's Outlook. " + " ".join(parts)
                  + " Summarise it the way a good assistant would, briefly, mentioning what matters most.")


# ---------------------------------------------------------------- the briefing

def briefing_context(cfg: dict[str, str]) -> str:
    """Everything the morning briefing is built from, fetched live. Each part fails on its own."""
    you = cfg["JARVIS_CALLS_YOU"]
    parts = [f"It is {time.strftime('%A, %B %d, %Y, %I:%M %p')}."]
    for name, fetch in (
        ("weather", lambda: weather_skill(cfg, "what's the weather today and tomorrow", you)),
        ("rand", lambda: fx_skill("rand dollar exchange rate", you)),
        ("news", lambda: search_skill(cfg, "latest news in South Africa today", you)),
        ("calendar", lambda: google_skill(cfg, "what's on my calendar today", you) if google.configured_calendar(cfg)
         else outlook_skill("what's on my calendar today", you) if outlook_signed_in() else None),
        ("mail", lambda: google_skill(cfg, "any unread mail", you) if google.configured_mail(cfg) else None),
    ):
        try:
            result = fetch()
            if result and result.context:
                parts.append(result.context)
            elif result and result.reply and name == "calendar":
                pass  # not signed in: leave the calendar out rather than apologise every morning
        except Exception as err:  # noqa: BLE001 - one missing piece must not sink the briefing
            log(f"Briefing: {name} failed: {err!r}")
    facts = load_memory()["facts"]
    if facts:
        parts.append("Things the user asked you to remember, in case any are relevant today: " + "; ".join(str(f) for f in facts[-10:]) + ".")
    return " ".join(parts)


# ---------------------------------------------------------------- start with Windows

AUTOSTART_ON_RE = re.compile(r"\b(start|run|launch|open)\s+(?:yourself\s+)?(?:automatically\s+)?(?:with|at|on)\s+(?:windows|start-?up|log-?on|login|boot)\b|\bautostart\b|\balways on\b", re.I)
AUTOSTART_OFF_RE = re.compile(r"\b(stop|don'?t|do not|no longer|remove|disable)\b.*\b(start|run|launch)\w*\s+(?:yourself\s+)?(?:with|at|on)\s+(?:windows|start-?up|log-?on|login|boot)\b|\bremove (?:yourself )?from start-?up\b", re.I)
TASK_NAME = "EDI"


def autostart_skill(text: str, you: str) -> Result | None:
    on, off = bool(AUTOSTART_ON_RE.search(text)), bool(AUTOSTART_OFF_RE.search(text))
    if not (on or off):
        return None
    if os.name != "nt":
        return Result("autostart", reply=f"Starting with the system is only wired up for Windows, {you}.")
    startup_link = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup" / "EDI.cmd"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if off:
        removed = False
        proc = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True, creationflags=flags)  # noqa: S603
        removed = proc.returncode == 0
        if startup_link.exists():
            startup_link.unlink()
            removed = True
        if removed:
            return Result("autostart", reply=f"Done, {you}. I won't start with Windows any more.")
        return Result("autostart", reply=f"I wasn't set to start with Windows, {you}.")
    launcher = ROOT / "autostart.cmd"
    # Task Scheduler first (starts even before you open anything); the Startup folder if Windows refuses,
    # which needs no permissions at all.
    proc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", f'"{launcher}"', "/RL", "LIMITED"],  # noqa: S603
                          capture_output=True, text=True, creationflags=flags)
    if proc.returncode == 0:
        log(f"Autostart: scheduled task '{TASK_NAME}' -> {launcher}")
        return Result("autostart", reply=f"Done, {you}. I'll start with Windows from now on, minimised. Pin my page in your browser and I'm always a tab away.")
    log(f"Autostart: Task Scheduler refused ({(proc.stderr or proc.stdout).strip()[:160]}); using the Startup folder instead")
    try:
        startup_link.parent.mkdir(parents=True, exist_ok=True)
        startup_link.write_text(f'@echo off\r\nstart "" /min "{launcher}"\r\n', encoding="utf-8")
    except OSError as err:
        log(f"Autostart failed: {err}")
        return Result("autostart", reply=f"Windows wouldn't let me register the start-up task, {you}. The reason is in the EDI window.")
    log(f"Autostart: {startup_link}")
    return Result("autostart", reply=f"Done, {you}. I'll start with Windows from now on, minimised, from your Startup folder. Pin my page in your browser and I'm always a tab away.")


# ---------------------------------------------------------------- dispatcher

def live_context(cfg: dict[str, str], text: str) -> str | None:
    """Only the look-things-up skills, for agents: never opens apps or touches memory."""
    you = cfg["JARVIS_CALLS_YOU"]
    for skill in (lambda t: weather_skill(cfg, t, you), lambda t: fx_skill(t, you), lambda t: google_skill(cfg, t, you),
                  lambda t: outlook_skill(t, you), lambda t: search_skill(cfg, t, you)):
        result = skill(text)
        if result:
            return result.context or result.reply
    return None


def handle(cfg: dict[str, str], text: str) -> Result | None:
    """Decide whether EDI can do something with what was said. None means: just chat."""
    you = cfg["JARVIS_CALLS_YOU"]
    for skill in (lambda t, y: memory_skill(t, y),
                  lambda t, y: autostart_skill(t, y),
                  lambda t, y: launcher_skill(t, y),
                  lambda t, y: weather_skill(cfg, t, y),
                  lambda t, y: fx_skill(t, y),
                  lambda t, y: google_skill(cfg, t, y),
                  lambda t, y: outlook_skill(t, y),
                  lambda t, y: search_skill(cfg, t, y)):
        try:
            result = skill(text, you)
        except Exception as err:  # noqa: BLE001 - a broken skill must never take EDI down
            log(f"Skill error: {err!r}")
            continue
        if result:
            return result
    return None


ABILITIES = (
    "You can do these things for the user, and they happen automatically when asked, so never say you can't: "
    "open apps, files, folders and websites on their computer (\"open Excel\", \"open my downloads folder\"); "
    "remember facts across restarts (\"remember that...\", \"what do you remember\", \"forget...\"); "
    "report live weather and forecasts; give live exchange rates; search the web and the news for current information; "
    "and read their calendar and unread inbox (Google Calendar and Gmail when set up, otherwise classic Outlook). "
    "Agents: the system spawns a helper agent when the user says something like \"spawn a research agent to ...\" "
    "or \"get a researcher to ...\", and you are told when that happens. You cannot spawn one yourself, so never say "
    "you are spawning or have spawned an agent; if the user seems to want one, tell them to say "
    "\"get a researcher to\" followed by the task. "
    "Eyes: when the user asks what is on their screen, what an error or window says, or to read or summarise what "
    "they are looking at, the system takes a screenshot and you answer from it. "
    "Scheduling: timers (\"set a timer for ten minutes\"), reminders (\"remind me at three to call the bank\"), standing "
    "orders (\"every weekday at seven, give me my briefing\"), \"what's scheduled\" and \"cancel the timer\" are handled by "
    "the system; the morning briefing (\"give me my briefing\") gathers weather, headlines, the rand and the day's schedule; "
    "\"start with Windows\" makes you start at logon. "
    "Taking over: when the user says \"take over:\" followed by a task, the system hands it to Claude Code, which "
    "can read, write and edit files, search, and run allowed programs on this computer, and you are told the result. "
    "You cannot start that yourself either; if the user wants something done on the computer, tell them to say "
    "\"take over\" and then the task. "
)
