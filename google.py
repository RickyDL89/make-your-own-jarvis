"""EDI's Google route: your calendar through its secret iCal address, your inbox through Gmail's unread feed.

Both are read-only and need no OAuth app, only two values in .env:
  GOOGLE_CALENDAR_ICS   the "secret address in iCal format" from Google Calendar settings (several: comma-separated)
  GMAIL_ADDRESS + GMAIL_APP_PASSWORD   an app password from Google Account > Security (2-step verification on)

Standard library only: the .ics is parsed here, recurring events are expanded for the days asked about,
and the Atom feed is read with xml.etree.
"""
from __future__ import annotations

import base64
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

USER_AGENT = "EDI/1.0 (local assistant)"
_cache: dict[str, tuple[float, str]] = {}


def configured_calendar(cfg: dict[str, str]) -> bool:
    return bool(cfg.get("GOOGLE_CALENDAR_ICS"))


def configured_mail(cfg: dict[str, str]) -> bool:
    return bool(cfg.get("GMAIL_ADDRESS") and cfg.get("GMAIL_APP_PASSWORD"))


def _fetch(url: str, headers: dict | None = None, ttl: int = 300) -> str:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if url.startswith("file://"):  # for tests
        text = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path)).read_text(encoding="utf-8")
    else:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - the user's own feed URLs
            text = response.read().decode("utf-8", "replace")
    _cache[url] = (time.time(), text)
    return text


# ---------------------------------------------------------------- calendar (.ics)

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str, params: dict[str, str], local_tz) -> tuple[datetime, bool]:
    """An iCal date or date-time -> (local naive datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        d = datetime.strptime(value, "%Y%m%d")
        return d, True
    utc = value.endswith("Z")
    stamp = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if utc:
        return stamp.replace(tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None), False
    tzid = params.get("TZID")
    if tzid and ZoneInfo is not None:
        try:
            return stamp.replace(tzinfo=ZoneInfo(tzid)).astimezone(local_tz).replace(tzinfo=None), False
        except Exception:  # noqa: BLE001 - no tz database on this machine: treat as local time
            pass
    return stamp, False


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, _, value = line.partition(":")
    # parameters may contain quoted strings with colons, which Google doesn't use; keep it simple
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


def parse_ics(text: str, local_tz) -> tuple[str, list[dict]]:
    """Returns (calendar name, raw events). Each event: start, end, all_day, summary, location, rrule, exdates, uid, recurrence_id, status."""
    name = ""
    events: list[dict] = []
    current: dict | None = None
    for line in _unfold(text):
        prop = _split_prop(line)
        if not prop:
            continue
        key, params, value = prop
        if key == "X-WR-CALNAME":
            name = value.strip()
        elif key == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {"exdates": set(), "status": "", "rrule": "", "location": "", "summary": "(no title)", "recurrence_id": None}
        elif key == "END" and value.strip().upper() == "VEVENT" and current is not None:
            if "start" in current:
                current.setdefault("end", current["start"] + (timedelta(days=1) if current["all_day"] else timedelta(hours=1)))
                events.append(current)
            current = None
        elif current is None:
            continue
        elif key == "DTSTART":
            current["start"], current["all_day"] = _parse_dt(value, params, local_tz)
        elif key == "DTEND":
            current["end"], _ = _parse_dt(value, params, local_tz)
        elif key == "SUMMARY":
            current["summary"] = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip() or "(no title)"
        elif key == "LOCATION":
            current["location"] = value.replace("\\,", ",").replace("\\n", " ").strip()
        elif key == "RRULE":
            current["rrule"] = value.strip()
        elif key == "EXDATE":
            for v in value.split(","):
                try:
                    current["exdates"].add(_parse_dt(v, params, local_tz)[0])
                except ValueError:
                    pass
        elif key == "RECURRENCE-ID":
            try:
                current["recurrence_id"] = _parse_dt(value, params, local_tz)[0]
            except ValueError:
                pass
        elif key == "UID":
            current["uid"] = value.strip()
        elif key == "STATUS":
            current["status"] = value.strip().upper()
    return name, events


WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _nth_weekday_of_month(d: date) -> int:
    return (d.day - 1) // 7 + 1


def _rule_matches(day: date, start: date, rule: dict) -> bool:
    freq = rule.get("FREQ", "")
    interval = int(rule.get("INTERVAL", "1") or 1)
    bydays = [b for b in rule.get("BYDAY", "").split(",") if b]
    if freq == "DAILY":
        return (day - start).days % interval == 0
    if freq == "WEEKLY":
        weeks = ((day - timedelta(days=day.weekday())) - (start - timedelta(days=start.weekday()))).days // 7
        if weeks % interval:
            return False
        wanted = {WEEKDAYS[b[-2:]] for b in bydays if b[-2:] in WEEKDAYS} or {start.weekday()}
        return day.weekday() in wanted
    if freq == "MONTHLY":
        months = (day.year - start.year) * 12 + day.month - start.month
        if months % interval:
            return False
        if bydays:  # e.g. 2TU = second Tuesday, -1FR = last Friday (approximated as the 4th or 5th)
            for b in bydays:
                m = re.match(r"(-?\d+)?([A-Z]{2})", b)
                if not m or WEEKDAYS.get(m.group(2)) != day.weekday():
                    continue
                n = int(m.group(1) or 0)
                if n == 0 or n == _nth_weekday_of_month(day) or (n == -1 and (day + timedelta(days=7)).month != day.month):
                    return True
            return False
        monthday = int(rule.get("BYMONTHDAY", start.day) or start.day)
        return day.day == monthday
    if freq == "YEARLY":
        return (day.year - start.year) % interval == 0 and day.month == start.month and day.day == start.day
    return False


def expand(events: list[dict], window_start: datetime, window_end: datetime) -> list[dict]:
    """Occurrences inside the window, recurring ones expanded, cancelled and overridden ones removed."""
    overrides = {(e.get("uid"), e["recurrence_id"]) for e in events if e.get("recurrence_id")}
    out: list[dict] = []
    for e in events:
        if e["status"] == "CANCELLED":
            continue
        duration = e["end"] - e["start"]
        if not e["rrule"]:
            if e["start"] < window_end and e["end"] > window_start:
                out.append({**e, "start": e["start"], "end": e["end"]})
            continue
        rule = dict(p.split("=", 1) for p in e["rrule"].split(";") if "=" in p)
        until = None
        if rule.get("UNTIL"):
            try:
                until = _parse_dt(rule["UNTIL"], {}, timezone.utc)[0]
            except ValueError:
                until = None
        count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
        start_date = e["start"].date()
        # walk day by day from the first occurrence (needed to honour COUNT) or from the window, whichever is later
        day = start_date if count else max(start_date, (window_start - duration).date())
        seen = 0
        last_day = (window_end + timedelta(days=1)).date()
        while day <= last_day:
            if _rule_matches(day, start_date, rule):
                occ = datetime.combine(day, e["start"].time())
                if until and occ > until:
                    break
                seen += 1
                if count and seen > count:
                    break
                if occ not in e["exdates"] and (e.get("uid"), occ) not in overrides and occ < window_end and occ + duration > window_start:
                    out.append({**e, "start": occ, "end": occ + duration})
            day += timedelta(days=1)
            if day > start_date + timedelta(days=366 * 5) and not count:
                break
    out.sort(key=lambda x: (x["start"], x["summary"]))
    return out


def calendar_events(cfg: dict[str, str], window_start: datetime, window_end: datetime) -> list[dict]:
    local_tz = datetime.now().astimezone().tzinfo
    found: list[dict] = []
    for url in [u.strip() for u in cfg.get("GOOGLE_CALENDAR_ICS", "").split(",") if u.strip()]:
        name, events = parse_ics(_fetch(url), local_tz)
        for occ in expand(events, window_start, window_end):
            occ["calendar"] = name
            found.append(occ)
    found.sort(key=lambda x: (x["start"], x["summary"]))
    return found


def describe_events(events: list[dict], when: str) -> str:
    if not events:
        return f"The user's calendar for {when} is empty."
    parts = []
    for e in events:
        if e["all_day"]:
            span = "all day" + (f" {e['start'].strftime('%a')}" if when != "today" else "")
        else:
            span = e["start"].strftime("%a %H:%M") + "-" + e["end"].strftime("%H:%M") if when not in ("today", "tomorrow") else e["start"].strftime("%H:%M") + "-" + e["end"].strftime("%H:%M")
        parts.append(f"{span} {e['summary']}" + (f" at {e['location']}" if e.get("location") else "") + (f" [{e['calendar']}]" if e.get("calendar") else ""))
    return f"The user's calendar for {when} ({len(events)} items): " + "; ".join(parts) + "."


# ---------------------------------------------------------------- mail (Gmail Atom feed)

ATOM = "{http://purl.org/atom/ns#}"


def gmail_unread(cfg: dict[str, str], label: str = "") -> dict:
    url = "https://mail.google.com/mail/feed/atom" + (f"/{label}" if label else "")
    token = base64.b64encode(f"{cfg['GMAIL_ADDRESS']}:{cfg['GMAIL_APP_PASSWORD']}".encode()).decode()
    return parse_atom(_fetch(url, headers={"Authorization": f"Basic {token}"}, ttl=60))


def parse_atom(text: str) -> dict:
    root = ET.fromstring(text)
    count_el = root.find(ATOM + "fullcount")
    messages = []
    for entry in root.findall(ATOM + "entry"):
        author = entry.find(ATOM + "author")
        messages.append({
            "subject": (entry.findtext(ATOM + "title") or "(no subject)").strip(),
            "snippet": (entry.findtext(ATOM + "summary") or "").strip(),
            "from": ((author.findtext(ATOM + "name") if author is not None else "") or "").strip(),
            "email": ((author.findtext(ATOM + "email") if author is not None else "") or "").strip(),
            "when": (entry.findtext(ATOM + "issued") or entry.findtext(ATOM + "modified") or "").strip(),
        })
    return {"unread": int(count_el.text) if count_el is not None and (count_el.text or "").isdigit() else len(messages), "messages": messages}


def describe_mail(feed: dict, sender: str = "") -> str:
    messages = feed["messages"]
    if sender:
        key = sender.lower()
        messages = [m for m in messages if key in m["from"].lower() or key in m["email"].lower()]
        if not messages:
            return f"There is no unread mail from {sender} in the user's Gmail inbox."
        head = f"Unread mail from {sender} ({len(messages)}):"
    else:
        head = f"The user's Gmail inbox has {feed['unread']} unread message{'s' if feed['unread'] != 1 else ''}." + (" Newest:" if messages else "")
    lines = []
    for m in messages[:8]:
        when = ""
        try:
            when = datetime.strptime(m["when"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone().strftime("%a %H:%M")
        except ValueError:
            pass
        lines.append(f"{m['from'] or m['email'] or 'unknown'}: {m['subject']}" + (f" ({when})" if when else "") + (f" - {m['snippet'][:120]}" if m["snippet"] else ""))
    return head + " " + "; ".join(lines) + ("." if lines else "")
