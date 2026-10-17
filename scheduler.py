"""EDI's scheduler: timers, reminders and standing orders, managed by voice.

  "Set a timer for ten minutes."            "Remind me in twenty minutes to take the bread out."
  "Remind me at three to call the bank."    "Every weekday at seven, give me my briefing."
  "Every Friday at six, take over: back up my repos folder."
  "What's scheduled?"                        "Cancel the timer."  "Cancel all reminders."

Jobs live in schedules.json. When one comes due its action runs through EDI's normal pipeline
(respond), so a job can say something, ask EDI a question, run the briefing, spawn agents or run a
take-over task, and the result is announced on the HUD, or queued for the next time someone is there.
Simple phrases are parsed here; anything else is handed to the brain to turn into a job.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = Path(os.environ.get("EDI_SCHEDULES") or ROOT / "schedules.json")  # overridable, so a test instance keeps its own

DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
         "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5, "quarter": 0.25}
UNIT = r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
TIME = r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?|o'?clock|in the (?:morning|afternoon|evening)|tonight)?"

BRIEFING_RE = re.compile(r"\b(?:(?:give me|read me|run|do|what's|whats|what is)\s+(?:my\s+|the\s+)?(?:morning\s+|daily\s+)?briefing|brief me|morning briefing|daily briefing)\b", re.I)
TIMER_RE = re.compile(rf"\b(?:set|start)\s+(?:a\s+)?(?:timer\s+for\s+(?P<n>[\w.\-]+)\s+{UNIT}|(?P<n2>[\w.\-]+)[\s-](?P<unit2>second|minute|hour)s?\s+timer)", re.I)
REMIND_IN_RE = re.compile(rf"\bremind me\s+(?:in\s+(?P<n>[\w.\-]+)\s+{UNIT}\s+)?(?:to\s+|that\s+|about\s+)?(?P<what>.+?)(?:\s+in\s+(?P<n2>[\w.\-]+)\s+(?P<unit2>seconds?|secs?|minutes?|mins?|hours?|hrs?))?[.!?]*$", re.I)
REMIND_AT_RE = re.compile(rf"\bremind me\s+(?:at\s+{TIME}\s+(?:tomorrow\s+)?)?(?:to\s+|that\s+|about\s+)?(?P<what>.+?)(?:\s+at\s+(?P<h2>\d{{1,2}})(?::(?P<m2>\d{{2}}))?\s*(?P<ap2>a\.?m\.?|p\.?m\.?|o'?clock|tonight)?)?(?P<tomorrow>\s+tomorrow)?[.!?]*$", re.I)
EVERY_RE = re.compile(rf"\bevery\s+(?P<when>day|morning|evening|night|weekday|week\s?days?|weekend|weekends|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
                      rf"(?:\s+(?:morning|evening|night))?(?:\s+at\s+{TIME})?\s*[,:]?\s*(?P<what>.+?)[.!?]*$", re.I)
EVERY_N_RE = re.compile(rf"\bevery\s+(?P<n>[\w.\-]+)?\s*{UNIT}\s*[,:]?\s*(?P<what>.+?)[.!?]*$", re.I)
LIST_RE = re.compile(r"\b(what(?:'s| is| do you have| have you got)\s+(?:on the\s+)?scheduled|list (?:my |the )?(?:reminders|timers|schedules?|jobs)|"
                     r"any (?:reminders|timers) (?:set|running)|what reminders|what timers|show (?:my )?schedule)\b", re.I)
CANCEL_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?(?:cancel|clear|delete|remove|stop)\s+(?:the\s+|my\s+|that\s+)?(?P<what>all (?:reminders|timers|schedules|jobs)|everything scheduled|"
                       r"timers?|reminders?|briefing|schedules?|jobs?|(?:the\s+)?.+?)(?:\s+(?:reminder|timer|job|schedule))?[.!?]*$", re.I)
SCHEDULE_HINT_RE = re.compile(r"\b(remind|timer|every (?:day|morning|evening|night|week|hour|minute|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                              r"schedule|at \d{1,2}(?::\d{2})?\s*(?:a\.?m|p\.?m|o'?clock)|tomorrow at|tonight at|in \d+ (?:minutes|hours))\b", re.I)

PLAN_SYSTEM = (
    "You turn what a user said to EDI, a voice assistant, into one scheduled job. Reply with JSON only, no prose, no code "
    'fences: {"job": {"kind": "once|daily|weekly|interval", "run_at": "YYYY-MM-DD HH:MM" (once only), "at": "HH:MM" '
    '(daily/weekly), "days": [0-6, Monday is 0] (weekly), "minutes": N (interval), "action": {"type": "speak|ask|briefing|task|agents", '
    '"text": "..."}, "label": "short name"}}. '
    "Action types: speak = say a reminder in the user's words; ask = put a question to EDI at that time (weather, news, "
    "calendar) and say the answer; briefing = the morning briefing; task = something to do on the computer with Claude Code "
    "(the text is the instruction); agents = spawn agents (the text is what to spawn). "
    'If it is not a request to schedule anything, reply {"job": null}.'
)


def _num(token: str | None, default: float = 1) -> float:
    if not token:
        return default
    token = token.lower().strip()
    if token in WORDS:
        return WORDS[token]
    try:
        return float(token)
    except ValueError:
        return default


def _seconds(n: str | None, unit: str) -> int:
    amount = _num(n)
    unit = unit.lower()
    if unit.startswith(("sec",)):
        return int(amount)
    if unit.startswith(("hour", "hr")):
        return int(amount * 3600)
    return int(amount * 60)


def _clock(h: str, m: str | None, ap: str | None, default_pm_after: int = 7) -> tuple[int, int]:
    """'3', '15', 'pm' -> (15, 15). A bare hour under 7 is taken as afternoon, since nobody books 3 a.m. by voice."""
    hour, minute = int(h), int(m or 0)
    ap = (ap or "").lower()
    if "p" in ap or "evening" in ap or "afternoon" in ap or "tonight" in ap:
        if hour < 12:
            hour += 12
    elif "a" in ap or "morning" in ap:
        if hour == 12:
            hour = 0
    elif hour < default_pm_after and hour != 0:
        hour += 12
    return hour % 24, minute % 60


def _spoken_time(hhmm: str) -> str:
    hour, minute = (int(x) for x in hhmm.split(":"))
    suffix = "in the morning" if hour < 12 else "in the afternoon" if hour < 18 else "in the evening"
    h12 = hour % 12 or 12
    return f"{h12}" + (f" {minute:02d}" if minute else "") + f" {suffix}"


def _spoken_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        mins = round(seconds / 60)
        return f"{mins} minute{'s' if mins != 1 else ''}"
    hours = round(seconds / 3600 * 2) / 2  # to the nearest half hour: "about 11 and a half hours"
    whole = int(hours)
    text = ("about " if hours != seconds / 3600 else "") + (f"{whole} and a half" if hours != whole else f"{whole}")
    return f"{text} hour{'s' if hours != 1 else ''}"


class Scheduler:
    def __init__(self, *, respond, announce, log, settings) -> None:
        self._respond = respond      # respond(cfg, text) -> reply text
        self._announce = announce    # announce(text, label)
        self._log = log
        self._settings = settings    # () -> cfg
        self._lock = threading.Lock()
        self._planner = None         # set by set_planner: llm(cfg, messages) -> str, for complicated phrases
        self.jobs: list[dict] = self._load()

    def set_planner(self, llm) -> None:
        self._planner = llm

    # ---- storage

    def _load(self) -> list[dict]:
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
            return [j for j in data if isinstance(j, dict) and j.get("action")] if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self) -> None:
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.jobs, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)

    # ---- when things are due

    @staticmethod
    def next_run(job: dict, now: datetime) -> datetime | None:
        kind = job.get("kind")
        if kind == "once":
            return datetime.fromisoformat(job["run_at"])
        if kind == "interval":
            last = datetime.fromisoformat(job["last_run"]) if job.get("last_run") else datetime.fromisoformat(job["created"])
            return last + timedelta(minutes=float(job.get("minutes") or 60))
        hour, minute = (int(x) for x in job["at"].split(":"))
        days = job.get("days") if kind == "weekly" else list(range(7))
        last = datetime.fromisoformat(job["last_run"]) if job.get("last_run") else None
        for offset in range(0, 8):
            day = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day.weekday() not in days:
                continue
            if last and last.date() == day.date():
                continue  # already ran today
            if offset == 0 and day < now - timedelta(hours=3):
                continue  # missed by more than three hours (EDI was off): skip to the next one
            return day
        return None

    def today_summary(self) -> str:
        now = datetime.now()
        todays = []
        for job in self.jobs:
            nxt = self.next_run(job, now)
            if nxt and nxt.date() == now.date() and nxt >= now:
                todays.append(f"{job['label']} at {_spoken_time(nxt.strftime('%H:%M'))}")
        return ("Scheduled for today: " + "; ".join(todays) + ".") if todays else "Nothing else is scheduled for today."

    def next_summary(self) -> str:
        now = datetime.now()
        upcoming = sorted((self.next_run(j, now), j) for j in self.jobs if self.next_run(j, now))
        if not upcoming:
            return "nothing"
        when, job = upcoming[0]
        return f"{job['label']} at {when.strftime('%a %H:%M')}"

    # ---- the loop

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()

    def _loop(self) -> None:
        while True:
            time.sleep(5)
            now = datetime.now()
            due = []
            with self._lock:
                for job in list(self.jobs):
                    nxt = self.next_run(job, now)
                    if nxt is None:
                        self.jobs.remove(job)
                        continue
                    if nxt <= now:
                        job["last_run"] = now.isoformat(timespec="seconds")
                        if job["kind"] == "once":
                            self.jobs.remove(job)
                        due.append(dict(job))
                if due:
                    self._save()
            for job in due:
                threading.Thread(target=self._fire, args=(job,), daemon=True).start()

    def _fire(self, job: dict) -> None:
        cfg = self._settings()
        you = cfg["JARVIS_CALLS_YOU"]
        action = job["action"]
        kind, text = action.get("type", "speak"), str(action.get("text") or "")
        self._log(f"Scheduler: {job['label']} is due ({kind})")
        try:
            if kind == "speak":
                prefix = "Your timer is up" if job.get("timer") else "A reminder"
                self._announce(f"{prefix}, {you}: {text}." if text else f"{prefix}, {you}.", job["label"])
            elif kind == "briefing":
                self._announce(self._respond(cfg, "give me my briefing"), job["label"])
            elif kind == "ask":
                self._announce(self._respond(cfg, text), job["label"])
            elif kind == "task":
                self._respond(cfg, "take over: " + text)   # the Operator announces its own result
            elif kind == "agents":
                self._respond(cfg, text)                   # the agents announce their own results
        except Exception as err:  # noqa: BLE001 - a bad job must never stop the loop
            self._log(f"Scheduler: {job['label']} failed: {err!r}")
            self._announce(f"I couldn't run {job['label']}, {you}. The reason is in the EDI window.", job["label"])

    # ---- adding, listing, cancelling

    def add(self, job: dict) -> dict:
        job.setdefault("id", uuid.uuid4().hex[:6])
        job.setdefault("created", datetime.now().isoformat(timespec="seconds"))
        job.setdefault("last_run", None)
        with self._lock:
            self.jobs.append(job)
            self._save()
        return job

    def cancel(self, what: str) -> list[dict]:
        what = what.lower().strip()
        with self._lock:
            if re.match(r"all |everything", what):
                gone, self.jobs = self.jobs, []
            elif what.startswith("timer"):
                gone = [j for j in self.jobs if j.get("timer")]
                self.jobs = [j for j in self.jobs if not j.get("timer")]
            elif what.startswith("reminder"):
                gone = [j for j in self.jobs if j["kind"] == "once" and not j.get("timer")]
                self.jobs = [j for j in self.jobs if not (j["kind"] == "once" and not j.get("timer"))]
            else:
                key = re.sub(r"^(?:the|my)\s+", "", what)
                gone = [j for j in self.jobs if key in j["label"].lower() or key in str(j["action"].get("text", "")).lower()]
                self.jobs = [j for j in self.jobs if j not in gone]
            if gone:
                self._save()
        return gone

    def describe(self, you: str) -> str:
        now = datetime.now()
        if not self.jobs:
            return f"Nothing is scheduled, {you}."
        parts = []
        for job in sorted(self.jobs, key=lambda j: self.next_run(j, now) or now):
            nxt = self.next_run(job, now)
            if job["kind"] == "once":
                when = ("in " + _spoken_duration(int((nxt - now).total_seconds()))) if nxt and nxt > now else "any moment now"
            elif job["kind"] == "interval":
                when = f"every {_spoken_duration(int(float(job['minutes']) * 60))}"
            elif job["kind"] == "weekly":
                names = [n for n, d in DAYS.items() if d in job["days"]]
                label = "weekdays" if sorted(job["days"]) == [0, 1, 2, 3, 4] else "weekends" if sorted(job["days"]) == [5, 6] else " and ".join(n.capitalize() for n in names)
                when = f"{label} at {_spoken_time(job['at'])}"
            else:
                when = f"every day at {_spoken_time(job['at'])}"
            parts.append(f"{job['label']}, {when}")
        return f"{len(parts)} thing{'s' if len(parts) != 1 else ''} scheduled, {you}: " + "; ".join(parts) + "."

    # ---- what the user said

    @staticmethod
    def _action_for(what: str) -> dict:
        w = what.strip().rstrip(".!?")
        if BRIEFING_RE.search(w):
            return {"type": "briefing", "text": ""}
        if re.match(r"^(?:take ?over|do this|handle this)\s*[:,]?\s*", w, re.I):
            return {"type": "task", "text": re.sub(r"^(?:take ?over|do this|handle this)\s*[:,]?\s*", "", w, flags=re.I)}
        if re.search(r"\bagents?\b|\b(researcher|analyst|scout|operator|planner)\b", w, re.I):
            return {"type": "agents", "text": w}
        if re.match(r"^(?:ask|tell me|check|what|what's|how|get me|find|give me|read me|is it|will it|do i)\b", w, re.I):
            return {"type": "ask", "text": w}
        return {"type": "speak", "text": re.sub(r"^(?:to\s+)", "", w)}

    def handle(self, cfg: dict, text: str, you: str, *, has_brain: bool) -> str | None:
        """Returns what EDI should say if this was about scheduling, else None."""
        if LIST_RE.search(text):
            return self.describe(you)
        m = CANCEL_RE.match(text)
        if m and re.search(r"\b(timers?|reminders?|schedules?|jobs?|briefing|everything scheduled)\b", text, re.I):
            gone = self.cancel(m.group("what"))
            if not gone:
                return f"There was nothing like that to cancel, {you}."
            return f"Cancelled {gone[0]['label']}, {you}." if len(gone) == 1 else f"Cancelled {len(gone)} things, {you}."
        job = self._parse(text)
        if job is None and has_brain and self._planner and SCHEDULE_HINT_RE.search(text) and not re.match(r"^(?:what|how|why|when|do you|can you)\b", text.strip(), re.I):
            job = self._plan(cfg, text)
        if not job:
            return None
        if job.get("needs_time"):
            return f"When should I remind you, {you}? Say in ten minutes, or at three."
        self.add(job)
        return self._confirmation(job, you)

    def _confirmation(self, job: dict, you: str) -> str:
        now = datetime.now()
        nxt = self.next_run(job, now)
        if job.get("timer"):
            return f"Timer set for {_spoken_duration(int((nxt - now).total_seconds()) + 1)}, {you}."
        what = job["label"]
        if job["kind"] == "once":
            delta = int((nxt - now).total_seconds())
            when = f"in {_spoken_duration(delta)}" if delta < 3 * 3600 else f"at {_spoken_time(nxt.strftime('%H:%M'))}" + (" tomorrow" if nxt.date() != now.date() else "")
            return f"I'll remind you {when}, {you}: {what}." if job["action"]["type"] == "speak" else f"Scheduled {when}, {you}: {what}."
        if job["kind"] == "interval":
            return f"Every {_spoken_duration(int(float(job['minutes']) * 60))}, {you}: {what}."
        days = job.get("days", list(range(7)))
        label = "every day" if sorted(days) == list(range(7)) else "every weekday" if sorted(days) == [0, 1, 2, 3, 4] else "every weekend" if sorted(days) == [5, 6] else "every " + " and ".join(n.capitalize() for n, d in DAYS.items() if d in days)
        return f"{label.capitalize()} at {_spoken_time(job['at'])}, {you}: {what}."

    def _parse(self, text: str) -> dict | None:
        now = datetime.now()
        if re.search(r"\bremind me\s+(?:of\s+|about\s+)?(?:what|who|how|where|when|why|which)\b", text, re.I):
            return None  # "remind me what my car is" is a question for her memory, not a schedule
        m = TIMER_RE.search(text)
        if m:
            secs = _seconds(m.group("n") or m.group("n2"), m.group("unit") or m.group("unit2"))
            return {"kind": "once", "run_at": (now + timedelta(seconds=secs)).isoformat(timespec="seconds"), "timer": True,
                    "label": f"{_spoken_duration(secs)} timer", "action": {"type": "speak", "text": ""}}
        m = REMIND_IN_RE.search(text)
        if m and (m.group("n") or m.group("n2")):
            secs = _seconds(m.group("n") or m.group("n2"), m.group("unit") or m.group("unit2"))
            what = m.group("what").strip()
            return {"kind": "once", "run_at": (now + timedelta(seconds=secs)).isoformat(timespec="seconds"),
                    "label": what[:60], "action": self._action_for(what)}
        m = REMIND_AT_RE.search(text)
        if m and (m.group("h") or m.group("h2")):
            hour, minute = _clock(m.group("h") or m.group("h2"), m.group("m") or m.group("m2"), m.group("ap") or m.group("ap2"))
            run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_at <= now or m.group("tomorrow") or re.search(r"\btomorrow\b", text, re.I):
                run_at += timedelta(days=1)
            what = re.sub(r"\s+tomorrow$", "", m.group("what").strip(), flags=re.I)
            return {"kind": "once", "run_at": run_at.isoformat(timespec="seconds"), "label": what[:60], "action": self._action_for(what)}
        label_for = lambda what, action: "your briefing" if action["type"] == "briefing" else (action["text"] or what)[:60]  # noqa: E731
        m = EVERY_RE.search(text)
        if m:
            when = m.group("when").lower().replace(" ", "")
            what = m.group("what").strip()
            if m.group("h"):
                hour, minute = _clock(m.group("h"), m.group("m"), m.group("ap"), default_pm_after=0 if when in ("morning", "weekday", "weekdays", "day") else 7)
            else:
                hour, minute = (7, 0) if when in ("morning", "day", "weekday", "weekdays") else (18, 0) if when == "evening" else (21, 0) if when == "night" else (9, 0)
            at = f"{hour:02d}:{minute:02d}"
            action = self._action_for(what)
            if when in ("day", "morning", "evening", "night"):
                return {"kind": "daily", "at": at, "label": label_for(what, action), "action": action}
            days = [0, 1, 2, 3, 4] if when.startswith("weekday") else [5, 6] if when.startswith("weekend") else [DAYS[when]]
            return {"kind": "weekly", "at": at, "days": days, "label": label_for(what, action), "action": action}
        m = EVERY_N_RE.search(text)
        if m:
            secs = _seconds(m.group("n"), m.group("unit"))
            what = re.sub(r"^remind me\s+(?:to\s+)?", "", m.group("what").strip(), flags=re.I)
            action = self._action_for(what)
            return {"kind": "interval", "minutes": max(1, secs / 60), "label": label_for(what, action), "action": action}
        if re.search(r"\bremind me\b", text, re.I):
            return {"needs_time": True}
        return None

    def _plan(self, cfg: dict, text: str) -> dict | None:
        try:
            raw = self._planner(cfg, [{"role": "system", "content": PLAN_SYSTEM + f" The local date and time now is {datetime.now().strftime('%A %Y-%m-%d %H:%M')}."},
                                      {"role": "user", "content": text}])
        except Exception as err:  # noqa: BLE001
            self._log(f"Schedule planner failed: {err!r}")
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0:
            return None
        try:
            job = (json.loads(raw[start:end + 1]) or {}).get("job")
        except json.JSONDecodeError:
            return None
        if not isinstance(job, dict) or job.get("kind") not in ("once", "daily", "weekly", "interval"):
            return None
        try:
            if job["kind"] == "once":
                job["run_at"] = datetime.strptime(job["run_at"], "%Y-%m-%d %H:%M").isoformat(timespec="seconds")
            elif job["kind"] == "interval":
                job["minutes"] = float(job["minutes"])
            else:
                datetime.strptime(job["at"], "%H:%M")
                if job["kind"] == "weekly":
                    job["days"] = [int(d) for d in job.get("days") or []] or [0, 1, 2, 3, 4]
        except (KeyError, ValueError, TypeError):
            return None
        action = job.get("action") or {}
        if action.get("type") not in ("speak", "ask", "briefing", "task", "agents"):
            return None
        job["action"] = {"type": action["type"], "text": str(action.get("text") or "")}
        job["label"] = str(job.get("label") or job["action"]["text"] or "job")[:60]
        return job
