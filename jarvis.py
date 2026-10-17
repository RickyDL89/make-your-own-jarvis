"""E.D.I. - your own AI assistant.

A free OpenRouter model is the brain, Fish Audio is the voice, your browser is the HUD.
Start it with run.bat (Windows) or ./run.sh (Mac/Linux).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agents
import brain
import scheduler
import screen
import skills
import tasks

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
INDEX_HTML = ROOT / "web" / "index.html"
PROTOCOLS_FILE = ROOT / "protocols.json"

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_SIGNUP_URL = "https://ariacodez.ai/l/fish-audio"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"

DEFAULTS = {
    "FISH_VOICE_ID": "41f0953d7a6b4c078445c7e65d620eeb",  # "JARVIS" in the Fish Audio voice library
    "FISH_MODEL": "s2.1-pro-free",  # S2.1 Pro at $0, free on the Fish Audio API until Nov 30, 2026
    "OPENROUTER_MODEL": "apodex/apodex-1.1-mini:free",  # the OpenRouter brain, used when Claude Code isn't installed
    "BRAIN": "",  # "claude" or "openrouter". Empty means: Claude Code if it's installed, else OpenRouter
    "CLAUDE_MODEL": brain.DEFAULT_MODEL,  # the Claude Code brain for conversation. Haiku is quick, which matters when it's spoken
    "CLAUDE_AGENT_MODEL": "claude-opus-5-5",  # the model agents and tasks think with, where quality matters more than speed
    "JARVIS_CALLS_YOU": "sir",
    "EDI_LOCATION": "",  # your city, for the weather. Empty means: work it out from the connection
    "BRAVE_API_KEY": "",  # optional, for real web search. Free at https://brave.com/search/api
    "PORT": "8765",
}

NO_BROWSER = os.environ.get("JARVIS_NO_BROWSER") == "1"  # for testing: never open browser tabs
HUD_VERSION = "3"  # bump when an old open tab must stop speaking for agents (see /api/events)


def log(message: str) -> None:
    print(f"  {time.strftime('%H:%M:%S')}  {message}", flush=True)


# ---------------------------------------------------------------- settings

def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in read_env_file().items() if v})
    # Keys come from the .env file only, so a key another app left in the system environment
    # never gets used by surprise. The other settings can be overridden for testing.
    for key in DEFAULTS:
        if os.environ.get(key):
            cfg[key] = os.environ[key].strip()
    return cfg


def save_to_env(updates: dict[str, str]) -> None:
    if ENV_FILE.exists():
        template = ENV_FILE.read_text(encoding="utf-8-sig")
    elif ENV_EXAMPLE.exists():
        template = ENV_EXAMPLE.read_text(encoding="utf-8")
    else:
        template = ""
    lines = template.splitlines()
    written = set()
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            written.add(key)
    lines += [f"{key}={value}" for key, value in updates.items() if key not in written]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().strip('"').strip("'")
    except EOFError:
        return ""


def brain_kind(cfg: dict[str, str]) -> str:
    """Which brain to use: Claude Code when it's installed, otherwise OpenRouter. BRAIN in .env forces one."""
    wanted = cfg.get("BRAIN", "").lower()
    if wanted in ("claude", "openrouter"):
        return wanted
    return "claude" if brain.available() else "openrouter"


def first_run(cfg: dict[str, str]) -> dict[str, str]:
    """Ask for any missing key in the console and save it to .env."""
    needed = ["FISH_API_KEY"] + (["OPENROUTER_API_KEY"] if brain_kind(cfg) == "openrouter" else [])
    missing = [k for k in needed if not cfg.get(k)]
    if not missing:
        return cfg
    print("\n  Welcome. EDI needs two keys, then she's yours.")
    print("  Paste each one and press Enter (right-click or Ctrl+V pastes).")
    updates: dict[str, str] = {}
    if "FISH_API_KEY" in missing:
        print("\n  1) THE VOICE - your Fish Audio API key")
        print(f"     Get it here: {FISH_SIGNUP_URL}  (profile > API Keys > Create)")
        key = ask("     Fish Audio key: ")
        if key:
            updates["FISH_API_KEY"] = key
    if "OPENROUTER_API_KEY" in missing:
        print("\n  2) THE BRAIN - your free OpenRouter API key")
        print(f"     Get it here: {OPENROUTER_KEYS_URL}  (sign up and create a key, it starts with sk-or-)")
        key = ask("     OpenRouter key: ")
        if key and not key.startswith("sk-or-"):
            print("     That doesn't look like an OpenRouter key (they start with sk-or-). Saving it anyway.")
        if key:
            updates["OPENROUTER_API_KEY"] = key
    if updates:
        save_to_env(updates)
        print("\n  Saved to the .env file in this folder. You won't be asked again.")
    cfg = load_settings()
    still = [k for k in needed if not cfg.get(k)]
    if still:
        print(f"\n  Still missing: {', '.join(still)}. EDI will start, and tells you what's missing.")
    return cfg


# ---------------------------------------------------------------- brain (a free OpenRouter model)

_history: list[dict] = skills.load_history()  # picks up where the last session left off
_lock = threading.Lock()


def persona(cfg: dict[str, str]) -> str:
    """Who EDI is. The part of the system prompt that doesn't change from one message to the next."""
    title = cfg["JARVIS_CALLS_YOU"]
    return (
        "You are EDI, pronounced ee-dee, the user's personal AI assistant, running on their computer. "
        'When you say your own name, write it as "Edie" so the voice engine pronounces it correctly. '
        f'Address the user as "{title}". Be calm, precise, quietly witty and loyal. '
        "Everything you write is spoken aloud by a voice engine, so reply in one or two short "
        "sentences of plain spoken English. Never use markdown, lists, emojis, URLs, code, or "
        "anything in square brackets or asterisks. "
        "A message may start with a line in square brackets: that is context from your own systems "
        "(the time, your notes, live data you fetched), not something the user said. Use it, never read it out. "
        + skills.ABILITIES
    )


_last_screen: tuple[float, str] | None = None  # (when, what she said about it), for follow-up questions


def turn_context(cfg: dict[str, str], context: str | None = None) -> str:
    """What changes every message: the time, the user's notes, live data a skill just fetched."""
    parts = [f"The local date and time is {time.strftime('%A, %B %d, %Y, %I:%M %p')}.", skills.memory_prompt().strip()]
    if _last_screen and time.time() - _last_screen[0] < 300:
        parts.append(f"{int(time.time() - _last_screen[0])} seconds ago you looked at the user's screen and said: {_last_screen[1]}")
    if context:
        parts.append(context)
    return " ".join(p for p in parts if p)


def look_at_screen(cfg: dict[str, str], text: str) -> str:
    """Take a screenshot and answer the question about it, in EDI's voice."""
    global _last_screen
    title = cfg["JARVIS_CALLS_YOU"]
    mode = "window" if screen.WANTS_WINDOW_RE.search(text) else "screen"
    path, info = screen.capture(mode)
    if not path:
        return f"I couldn't capture the screen, {title}. {info}"
    log(f"Screen: captured {info} -> {path.name}")
    system = (persona(cfg) + " You have been given the path of a screenshot of the user's screen, taken just now. "
              "Use the Read tool to look at it before answering. Answer the question about what is on the screen in one "
              "to four spoken sentences; when asked to read something out, read the exact text. Never mention the file "
              "path, the tool, or that it is a screenshot; speak as if you are looking at the screen.")
    prompt = f"The user asked: {text}\nThe screenshot shows {info}. It is at: {path}\nRead it now and answer."
    reply = brain.complete(cfg["CLAUDE_AGENT_MODEL"], system, prompt, tools=["Read"])
    reply = reply or f"I couldn't make anything out, {title}."
    _last_screen = (time.time(), reply)
    with _lock:
        _history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
        del _history[:-16]
        skills.save_history(list(_history))
    return reply


def system_prompt(cfg: dict[str, str]) -> str:
    return persona(cfg) + turn_context(cfg)


_claude: brain.Session | None = None


def claude_session(cfg: dict[str, str]) -> brain.Session:
    global _claude
    if _claude is None:
        _claude = brain.Session(cfg["CLAUDE_MODEL"], persona(cfg), log)
    return _claude


class OpenRouterError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def openrouter_post(cfg: dict[str, str], payload: dict) -> dict:
    request = urllib.request.Request(OPENROUTER_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {cfg['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
            data = json.loads(response.read())
    except urllib.error.HTTPError as err:
        raise OpenRouterError(err.code, err.read()[:400].decode("utf-8", "replace")) from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise OpenRouterError(0, str(err)) from err
    except json.JSONDecodeError as err:
        raise OpenRouterError(502, "unreadable reply") from err
    # OpenRouter reports some failures inside a 200 response.
    choices = data.get("choices") or [{}]
    problem = data.get("error") or choices[0].get("error")
    if isinstance(problem, dict):
        code = problem.get("code")
        raise OpenRouterError(code if isinstance(code, int) else 502, str(problem.get("message") or problem))
    return data


def llm(cfg: dict[str, str], messages: list[dict], max_tokens: int = 4000) -> str:
    """One call to the brain with any messages. Agents use this with their own persona."""
    if brain_kind(cfg) == "claude":
        system = " ".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        if len(turns) == 1:
            prompt = turns[0]["content"]
        else:  # a conversation, flattened: Claude sees who said what
            prompt = "\n\n".join(("User: " if m["role"] == "user" else "Assistant: ") + m["content"] for m in turns) + "\n\nAssistant:"
        try:
            return brain.complete(cfg["CLAUDE_AGENT_MODEL"], system, prompt)
        except brain.BrainError:
            if not cfg.get("OPENROUTER_API_KEY"):
                raise
            log("Brain: Claude failed, falling back to OpenRouter for this call")
    return openrouter_llm(cfg, messages, max_tokens)


def openrouter_llm(cfg: dict[str, str], messages: list[dict], max_tokens: int = 4000) -> str:
    payload = {
        "model": cfg["OPENROUTER_MODEL"],
        "messages": messages,
        "max_tokens": max_tokens,
        "reasoning": {"effort": "none"},  # skip the thinking step, so spoken replies come back fast
    }
    try:
        data = openrouter_post(cfg, payload)
    except OpenRouterError as err:
        if err.status != 400:
            raise
        del payload["reasoning"]  # some models can't switch their thinking off
        data = openrouter_post(cfg, payload)
    choices = data.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content")
    return " ".join(content.split()) if isinstance(content, str) else ""


def think(cfg: dict[str, str], text: str, context: str | None = None, remember: bool = True) -> str:
    """Ask the brain as EDI. `context` is live data a skill just fetched (weather, search results, calendar)."""
    reply = ""
    if brain_kind(cfg) == "claude" and remember:
        # The persistent Claude session holds the conversation itself; each turn gets the time,
        # the user's notes and any live data in a bracketed line the persona knows to treat as context.
        try:
            reply = claude_session(cfg).ask(f"[{turn_context(cfg, context)}]\n{text}")
        except brain.BrainError as err:
            if not cfg.get("OPENROUTER_API_KEY"):
                raise
            log(f"Brain: Claude failed ({str(err)[:80]}), falling back to OpenRouter")
    if not reply:
        with _lock:
            messages = list(_history) + [{"role": "user", "content": text}]
        system = [{"role": "system", "content": system_prompt(cfg)}]
        if context:
            system.append({"role": "system", "content": context})
        reply = llm(cfg, [*system, *messages])
    reply = reply or f"I'm not sure what to say to that, {cfg['JARVIS_CALLS_YOU']}."
    if remember:
        with _lock:
            _history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            del _history[:-16]  # keep the last 8 exchanges
            skills.save_history(list(_history))
    return reply


AGENTS = agents.Runtime(llm=llm, think=think, live_context=skills.live_context, log=log)


def brain_error(err: OpenRouterError, cfg: dict[str, str]) -> str:
    title = cfg["JARVIS_CALLS_YOU"]
    if err.status == 401:
        return f"My brain key isn't working, {title}. Check OPENROUTER_API_KEY in the .env file."
    if err.status == 402:
        return f"Your OpenRouter account is out of credit, {title}. Check it at openrouter.ai."
    if err.status == 429:
        if "per-day" in err.detail:
            return f"I've used up today's free questions, {title}. They reset tomorrow."
        return f"I'm being rate limited, {title}. Give me a moment and try again."
    if err.status == 403:
        return f"OpenRouter blocked that request, {title}. The reason is in the EDI window."
    if err.status in (400, 404):
        return f"OpenRouter won't run the model {cfg['OPENROUTER_MODEL']}, {title}. The reason is in the EDI window."
    if err.status == 0:
        return f"I can't reach my brain, {title}. Check your internet connection."
    return f"OpenRouter is having a moment, {title}. Try again shortly."


# ---------------------------------------------------------------- voice (Fish Audio)

_voice_override: str | None = None  # set by a protocol with a "voice" field, until restart


_last_voice_problem: str | None = None


def speak(cfg: dict[str, str], text: str) -> tuple[bytes | None, str | None]:
    """The whole clip at once (POST /api/tts)."""
    response, problem = tts_open(cfg, text)
    if problem:
        return None, problem
    with response:
        return response.read(), None


def tts_open(cfg: dict[str, str], text: str):
    """Start a Fish Audio request and hand back the open response, so the bytes can be streamed as they arrive."""
    global _last_voice_problem
    key = cfg.get("FISH_API_KEY")
    if not key:
        _last_voice_problem = "No Fish Audio key yet, so you're hearing the backup voice. Add FISH_API_KEY to the .env file."
        return None, _last_voice_problem
    # The screen shows "EDI", but the voice engine would spell that out. "Edie" is spoken ee-dee.
    text = re.sub(r"\b(?:E\.D\.I\.|EDI)(?=\W|$)", "Edie", text)
    body = json.dumps({
        "text": text,
        "reference_id": _voice_override or cfg["FISH_VOICE_ID"],
        "format": "mp3",
        "mp3_bitrate": 128,
        "normalize": True,
        "latency": "balanced",
    }).encode("utf-8")
    request = urllib.request.Request(FISH_TTS_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "model": cfg["FISH_MODEL"],
    })
    try:
        response = urllib.request.urlopen(request, timeout=60)  # noqa: S310 - fixed https URL
        _last_voice_problem = None
        return response, None
    except urllib.error.HTTPError as err:
        detail = err.read()[:300].decode("utf-8", "replace")
        log(f"Fish Audio error {err.code}: {detail}")
        if err.code in (401, 403):
            problem = "Fish Audio didn't accept the key. Check FISH_API_KEY in the .env file."
        elif err.code == 402:
            problem = "Your Fish Audio API credit is empty. Add credit on fish.audio and EDI gets her voice back."
        elif err.code in (400, 404, 422):
            problem = "Fish Audio couldn't use that voice. Check FISH_VOICE_ID and FISH_MODEL in the .env file."
        else:
            problem = f"Fish Audio returned an error ({err.code}). Using the backup voice for now."
    except (urllib.error.URLError, TimeoutError):
        problem = "Can't reach Fish Audio right now. Using the backup voice."
    _last_voice_problem = problem
    return None, problem


# ---------------------------------------------------------------- protocols

def load_protocols() -> list[dict]:
    try:
        data = json.loads(PROTOCOLS_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as err:
        log(f"protocols.json has a typo ({err}). Ignoring it until it's fixed.")
        return []
    return [p for p in data if isinstance(p, dict) and p.get("say")] if isinstance(data, list) else []


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def match_protocol(text: str) -> dict | None:
    said = f" {_words(text)} "
    for protocol in load_protocols():
        if f" {_words(protocol['say'])} " in said:
            return protocol
    return None


def open_url(url: str) -> None:
    if NO_BROWSER:
        log(f"(would open) {url}")
    else:
        webbrowser.open(url)


# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    server_version = "EDI"

    def log_message(self, format: str, *args) -> None:  # keep the window readable
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 100_000:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/status":
            cfg = load_settings()
            kind = brain_kind(cfg)
            self._json(200, {
                "voice": bool(cfg.get("FISH_API_KEY")),
                "brain": kind == "claude" or bool(cfg.get("OPENROUTER_API_KEY")),
                "brainName": f"Claude {cfg['CLAUDE_MODEL'].split('-')[1]}" if kind == "claude" else "OpenRouter",
                "model": cfg["CLAUDE_MODEL"] if kind == "claude" else cfg["OPENROUTER_MODEL"],
                "calls": cfg["JARVIS_CALLS_YOU"],
                "protocols": [p["say"] for p in load_protocols()],
            })
        elif path == "/api/events":
            # The HUD polls this to draw agents and the messages flying between them and EDI.
            query = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&") if "=" in pair) if "?" in self.path else {}
            try:
                since = int(query.get("since", ""))
            except ValueError:
                since = None  # first poll: the page only wants the current seq and the active agents
            events, seq = AGENTS.events_since(since) if since is not None else ([], AGENTS.events_since(0)[1])
            if since is None and query.get("v") == HUD_VERSION:
                events = AGENTS.unheard_events()  # a page arriving late still gets what it missed
            if query.get("v") != HUD_VERSION:
                # A page running an older HUD (a tab left open from before a restart) must not speak:
                # it would talk over the current one. It still gets to draw the agents.
                events = [{**e, "speak": ""} if e.get("type") in ("agent_done", "announce") else e for e in events]
            self._json(200, {"seq": seq, "events": events, "agents": AGENTS.active()})
        elif path == "/api/tts":
            # Streaming speech: Fish Audio's MP3 bytes go to the page as they arrive, so the audio element
            # starts playing after the first chunk instead of after the whole clip.
            query = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&") if "=" in pair) if "?" in self.path else {}
            text = urllib.parse.unquote_plus(query.get("text", ""))[:2000].strip()
            if not text:
                self._json(400, {"error": "Nothing to say."})
                return
            response, problem = tts_open(load_settings(), text)
            if problem:
                self._json(502, {"error": problem})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with response:
                    while True:
                        chunk = response.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # she was interrupted; the page closed the stream
        elif path == "/api/tts/check":
            self._json(200, {"error": _last_voice_problem or "The voice stream stopped. Using the backup voice."})
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        global _voice_override
        path = self.path.split("?", 1)[0]
        cfg = load_settings()
        data = self._read_json()
        text = str(data.get("text") or "").strip()[:2000]
        title = cfg["JARVIS_CALLS_YOU"]

        if path == "/api/chat":
            if not text:
                self._json(400, {"error": "Say something first."})
                return
            self._json(200, respond(cfg, text))
            return

        if path == "/api/tts":
            if not text:
                self._json(400, {"error": "Nothing to say."})
                return
            audio, problem = speak(cfg, text)
            if audio:
                self._send(200, audio, "audio/mpeg")
            else:
                self._json(502, {"error": problem})
            return

        if path == "/api/claim":
            # One voice per agent report: the first HUD to claim an event's seq gets to speak it.
            try:
                seq = int(data.get("seq"))
            except (TypeError, ValueError):
                self._json(400, {"error": "seq?"})
                return
            self._json(200, {"speak": AGENTS.claim(seq)})
            return

        if path == "/api/reset":
            with _lock:
                _history.clear()
                skills.save_history([])
            if _claude:
                _claude.forget()
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "not found"})


def respond(cfg: dict[str, str], text: str) -> dict:
    """Everything EDI does with a sentence, in order: protocols, scheduling, take-over, agents, skills, then the brain.

    Used by the HUD's /api/chat and by the scheduler when a job comes due. Returns the JSON the HUD expects.
    """
    global _voice_override
    title = cfg["JARVIS_CALLS_YOU"]
    log(f"You:    {text}")
    protocol = match_protocol(text)
    if protocol:
        for url in protocol.get("open") or []:
            open_url(str(url))
        for target in protocol.get("run") or []:  # local apps and files, see skills.launch
            try:
                skills.launch(str(target))
            except OSError as err:
                log(f"Couldn't run {target}: {err}")
        if protocol.get("voice"):
            voice_id = str(protocol["voice"]).strip()
            _voice_override = None if voice_id == "default" else voice_id
        reply = str(protocol.get("reply") or "Right away, {you}.").replace("{you}", title)
        log(f"EDI:    {reply}  (protocol)")
        return {"reply": reply, "protocol": protocol["say"]}
    has_brain = brain_kind(cfg) == "claude" or bool(cfg.get("OPENROUTER_API_KEY"))
    # Scheduling first, so "every weekday at seven, give me my briefing" becomes a job rather than a briefing now.
    scheduled = SCHEDULER.handle(cfg, text, title, has_brain=has_brain)
    if scheduled:
        log(f"EDI:    {scheduled}  (schedule)")
        return {"reply": scheduled, "skill": "schedule"}
    # The briefing: weather, headlines, the rand, today's schedule, in one go.
    if scheduler.BRIEFING_RE.search(text):
        if not has_brain:
            return {"reply": f"The briefing needs a brain, {title}. Install Claude Code or add an OpenRouter key.", "error": "brain"}
        try:
            reply = briefing(cfg)
        except Exception as err:  # noqa: BLE001 - keep EDI alive and report it
            log(f"Briefing failed: {err!r}")
            return {"reply": f"I couldn't put the briefing together, {title}. The reason is in the EDI window.", "error": "internal"}
        log(f"EDI:    {reply}  (briefing)")
        return {"reply": reply, "skill": "briefing"}
    # Eyes: "what's on my screen", "what does this error say", "read this to me".
    if screen.SCREEN_RE.search(text) and brain_kind(cfg) == "claude":
        try:
            reply = look_at_screen(cfg, text)
        except brain.BrainError as err:
            log(f"Screen: {err}")
            reply = f"I couldn't get a look at the screen, {title}. {err}"
        except Exception as err:  # noqa: BLE001 - keep EDI alive and report it
            log(f"Screen failed: {err!r}")
            reply = f"Something went wrong while I was looking at the screen, {title}. The details are in the EDI window."
        log(f"EDI:    {reply}  (screen)")
        return {"reply": reply, "skill": "screen"}
    # Take-over: "EDI, take over: ..." hands the task to Claude Code with tools, shown as the Operator.
    task_reply = AGENTS.handle_task_phrase(cfg, text, title, available=brain_kind(cfg) == "claude")
    if task_reply:
        log(f"EDI:    {task_reply}  (task)")
        return {"reply": task_reply, "skill": "task"}
    # Agents: "spawn a research agent to ..." puts helpers on the HUD that report back later.
    # Several at once are fine; an operator assignment is a take-over task (Claude Code with tools).
    wanted = AGENTS.requests(cfg, text) if has_brain else AGENTS.parse(text)
    if wanted:
        if not has_brain:
            return {"reply": f"I can't spawn agents without a brain, {title}. Add OPENROUTER_API_KEY to the .env file.", "error": "brain"}
        if wanted.get("demo"):
            spawned = AGENTS.demo(cfg)
            reply = AGENTS.acknowledgement(spawned, title)
        else:
            asks = wanted["agents"]
            if len(asks) == 1 and len(asks[0]["task"].split()) < 2:
                reply = AGENTS.missing_task_reply(asks[0]["role"], title)  # "spawn a researcher" with no task
                log(f"EDI:    {reply}  (agents)")
                return {"reply": reply, "skill": "agents"}
            spawned, held = [], []
            for ask in asks:
                if ask["role"] == "operator":
                    if brain_kind(cfg) != "claude":
                        held.append("the operator needs Claude Code installed")
                    elif tasks.DANGER_RE.search(ask["task"]):
                        AGENTS._pending_task = (ask["task"], time.time())
                        held.append("the operator's task sounds destructive, so say confirm to let it go ahead")
                    else:
                        spawned.append(AGENTS.spawn_task(cfg, ask["task"]))
                else:
                    spawned.append(AGENTS.spawn(cfg, ask["role"], ask["task"]))
            reply = AGENTS.acknowledgement(spawned, title) if spawned else f"Nothing started yet, {title}."
            if held:
                reply += " One thing, " + "; ".join(held) + "."
        log(f"EDI:    {reply}  (agents)")
        return {"reply": reply, "skill": "agents", "agents": [a["id"] for a in spawned]}
    # Skills: things EDI does herself (open an app, remember a fact) answer right away.
    # Things that need live data (weather, search, Outlook) fetch it, then let the brain answer.
    skill = skills.handle(cfg, text)
    if skill and skill.reply:
        log(f"EDI:    {skill.reply}  ({skill.name})")
        return {"reply": skill.reply, "skill": skill.name}
    if not has_brain:
        return {"reply": f"My brain isn't connected yet, {title}. Install Claude Code, or add OPENROUTER_API_KEY to the .env file, and restart me.", "error": "brain"}
    try:
        reply = think(cfg, text, skill.context if skill else None)
    except OpenRouterError as err:
        log(f"OpenRouter error {err}")
        return {"reply": brain_error(err, cfg), "error": "brain"}
    except brain.BrainError as err:
        log(f"Claude error: {err}")
        return {"reply": f"My brain isn't answering, {title}. {err}", "error": "brain"}
    except Exception as err:  # noqa: BLE001 - keep EDI alive and report it
        log(f"Unexpected error: {err!r}")
        return {"reply": f"Something went wrong in my head, {title}. The details are in the EDI window.", "error": "internal"}
    log(f"EDI:    {reply}" + (f"  ({skill.name})" if skill else ""))
    return {"reply": reply, **({"skill": skill.name} if skill else {})}


def briefing(cfg: dict[str, str]) -> str:
    """The morning briefing: live facts gathered by skills, told by EDI in her own words."""
    facts = skills.briefing_context(cfg)
    facts += " " + SCHEDULER.today_summary()
    missed = AGENTS.unheard_text()
    if missed:
        facts += " Things that happened while the user was away: " + missed
    return think(cfg, "Give me my briefing.",
                 context=("The user asked for their briefing. Facts gathered just now: " + facts +
                          " Deliver it as a spoken briefing of four to seven sentences: greet them with the day, then "
                          "weather, the main headlines, the rand, anything scheduled today, anything that happened while "
                          "they were away, and one short closing line. Plain spoken English, no lists."),
                 remember=True)


SCHEDULER = scheduler.Scheduler(respond=lambda cfg, text: respond(cfg, text).get("reply", ""),
                                announce=lambda text, label: AGENTS.announce(text, label), log=log,
                                settings=load_settings)
SCHEDULER.set_planner(llm)  # complicated phrases ("the first Monday of next month at nine...") go to the brain


class Server(ThreadingHTTPServer):
    # Windows lets a second program share a port that's already taken when address reuse is on.
    # With it off the bind fails instead, so main() moves on to the next free port.
    allow_reuse_address = os.name != "nt"


BANNER = r"""
   _____  ____   ___
  | ____||  _ \ |_ _|
  |  _|  | | | | | |
  | |___ | |_| | | |
  |_____||____/ |___|
"""


def main() -> None:
    try:
        # Line-buffered, so the address shows up right away even when the output goes to a log file.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass
    print(BANNER)
    cfg = first_run(load_settings())

    port = int(cfg.get("PORT") or 8765)
    server = None
    for candidate in range(port, port + 20):
        try:
            server = Server(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"\n  Couldn't find a free port near {port}. Close other apps or set PORT in .env.\n")
        sys.exit(1)

    url = f"http://127.0.0.1:{server.server_address[1]}"
    print("\n  ==================================================")
    print("    E.D.I. is online")
    print(f"    Open {url}  (Chrome or Edge, for voice input)")
    print(f"    Voice: Fish Audio {cfg['FISH_VOICE_ID']} ({cfg['FISH_MODEL']})")
    kind = brain_kind(cfg)
    if kind == "claude":
        print(f"    Brain: Claude Code {cfg['CLAUDE_MODEL']}" + (" (OpenRouter as fallback)" if cfg.get("OPENROUTER_API_KEY") else ""))
    else:
        print(f"    Brain: OpenRouter {cfg['OPENROUTER_MODEL']}" if cfg.get("OPENROUTER_API_KEY") else "    Brain: not connected yet")
    print("    Skills: open apps and files, memory, weather, web search, Outlook, agents")
    print("    Close this window to shut EDI down.")
    print("  ==================================================\n")
    if kind == "claude":
        threading.Thread(target=lambda: claude_session(cfg).warm(), daemon=True).start()  # so the first reply is quick
    SCHEDULER.start()
    if SCHEDULER.jobs:
        print(f"    Scheduled: {len(SCHEDULER.jobs)} job(s), next: {SCHEDULER.next_summary()}")
    if not NO_BROWSER:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  EDI offline. Goodbye.\n")
    finally:
        if _claude:
            _claude.stop()  # take the brain down with her


if __name__ == "__main__":
    main()
