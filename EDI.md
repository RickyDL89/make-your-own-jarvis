# EDI — what she can do

EDI is this JARVIS build renamed and given skills. Everything below works by voice (hold **Space**,
or say **"EDI"** with the wake word on) or by typing in the box. She still answers any normal
question with her brain; the skills kick in automatically when what you say matches one.

## The look

EDI is a planetary nebula drawn by a WebGL shader: a clumpy ring of gas with dark lanes and a bright
inner rim, fine filaments pointing at a central star, a translucent interior and a faint outer shell,
all drifting slowly. You can see her think: veins of light thread the cloud and carry pulses outward
from the star, waves of light roll out through the gas, and lightning flickers through the interior,
all rare while she is idle and a storm while she works out an answer or an agent reports in. Her
colour walks around the whole spectrum while she is idle or thinking
(the ring and the interior always stay complementary); **listening is always cyan** and **speaking is
always gold**, so you can tell what she is doing at a glance. Her voice makes the star breathe. Click
the nebula to talk, the same as holding Space. Without WebGL the gas falls back to a soft glow.

## Agents

- **"Spawn a research agent to find the latest news about Eskom."**
- **"Delegate the weekend forecast to a weather agent."**
- **"Get a planner agent to break my Saturday into a schedule."**
- **"Test your agents"** — spawns a Researcher and an Analyst that each have to ask EDI a question
  first, so you can watch them talk.

An agent appears beside EDI as a smaller nebula in its own fixed colour (orange, green, violet,
pink, blue, yellow, in spawn order) with a dashed link to her. Packets fly along the link whenever
one of them sends the other a message, and the **AGENT COMMS** panel at the left shows what was
said. The agent works on its own thread with its own persona (`research`, `analyst`, `writer`,
`coder`, `planner`, `weather`, `travel`, `scout`, or a generalist), can use EDI's live-data skills,
may ask EDI one clarifying question, and reports back. EDI then tells you the result out loud, and
the agent fades away a few seconds later. Several agents can run at once.

Agent roles and colours live at the top of [agents.py](agents.py).

## How she speaks

Her replies are streamed: the text is split into sentences, each sentence is fetched as a streaming MP3 that starts
playing after the first chunk arrives, and the next sentence is already loading while she says this one. She starts
talking well under a second after the brain answers, however long the reply. If Fish Audio fails, she finishes the
sentence in the browser's voice and a red note says why.

**Interrupting her:** with the wake word on, say **"EDI"** while she is talking and she stops at once. *"EDI, stop"*
just silences her; *"EDI, what about tomorrow?"* cuts her off and answers the new question; a bare *"EDI"* makes her
stop and listen. Holding Space or clicking the nebula interrupts her too.

## Talk to her without touching anything

Click **WAKE WORD · OFF** at the top right of the page to turn it on. Then:

- **"EDI, what's the weather tomorrow?"** — say the name and the question in one breath.
- **"EDI"** on its own — she chimes, then listens for one sentence.

She hears "Eddie", "Edie" and "E D I" too, because speech recognition spells her name every which way.
The listening is done by Chrome or Edge, so it only works while her tab is open, and she never listens
while she is speaking. The setting is remembered next time.

## Open things on your computer

- **"Open Excel"**, "launch Chrome", "start Notepad", "open Visual Studio Code" — anything in your Start Menu.
- **"Open my downloads folder"**, "open documents", "open desktop", "open pictures".
- **"Open calculator"**, "open settings", "open task manager", "open camera", "open control panel".
- **"Open youtube.com"** — any web address.
- Nicknames and files live in [apps.json](apps.json): `"my budget": "C:\\Users\\You\\Documents\\budget.xlsx"`
  lets you say "open my budget".

Protocols in [protocols.json](protocols.json) can run local programs too, with a `"run"` list next to `"open"`:
`"run": ["outlook.exe"]`.

## Her eyes: what's on your screen

- **"What's on my screen?"** / **"What am I looking at?"** — the whole screen (all monitors).
- **"What does this error say?"** / **"Read this to me."** / **"Summarise this page."** / **"What's wrong here?"** —
  the active window. If the active window is EDI's own page she looks at the whole screen instead.
- Follow-ups work for a few minutes: *"and what's the second line?"*
- **"Take over: fix the error in that window"** — when a task mentions the screen or a window, the Operator gets a
  fresh screenshot to look at before it starts.

The capture uses Windows' own drawing APIs through a hidden PowerShell call (nothing to install), is downscaled to
keep it quick, and is saved in `screens/` next to her (git-ignored, only the last four kept). A screenshot leaves the
computer only as part of the request to Claude, the same as any image Claude Code reads. Whole-screen shots of two
monitors are small on detail; for exact text, ask about *this window*.

## Timers, reminders and standing orders

- **"Set a timer for ten minutes."** — "Your timer is up, sir."
- **"Remind me in twenty minutes to take the bread out."** / **"Remind me at three to call the bank."** /
  **"Remind me at 7:30 tomorrow to pack the gym bag."**
- **"Every weekday at seven, give me my briefing."** / **"Every morning, ask what the weather is."** /
  **"Every Friday at six, take over: back up my repos folder to D:\backups."** / **"Every two hours, remind me to stretch."**
- **"What's scheduled?"** — she reads the list. **"Cancel the timer."** / **"Cancel all reminders."** / **"Cancel the briefing."**

Simple phrases are understood instantly; anything more involved ("the first Monday of next month at nine...") goes to
the brain. Jobs live in `schedules.json` and survive restarts. When one comes due she speaks up on the page; if nobody
is there she keeps it and opens with *"While you were away, sir..."* next time. A job she misses by more than three
hours (she was off) is skipped until its next turn.

## The briefing

**"Give me my briefing"** (or **"brief me"**): the day and time, today's and tomorrow's weather, the top headlines,
the rand, your calendar if Outlook is signed in, anything scheduled today, anything that happened while you were away,
and anything you asked her to remember that might matter. Schedule it with *"every weekday at seven, give me my briefing."*

## Starting with Windows

**"EDI, start with Windows"** registers a logon task that starts her minimised, without opening a browser tab, so she
is always running for timers, reminders and scheduled jobs. Pin her page in your browser. **"Stop starting with
Windows"** removes the task. (The task runs [autostart.cmd](autostart.cmd).)

## Taking over: EDI does things on your computer

- **"EDI, take over: rename the photos in my downloads folder by date."**
- **"Take over: find every .log file older than a month under my repos and list them in a text file on the desktop."**
- **"Do this: add a README to the project in D:\repos\myapp describing what it does."**

The task goes to **Claude Code** with tools (Opus 5.5), running as the **Operator**, an orange nebula next to
EDI. Every step it takes (reading a file, running a command, searching) flies to EDI and shows in the comms
panel, and EDI tells you the result out loud when it is finished. Because it is Claude Code, your own Claude
Code setup applies: MCP servers you have added (Desktop Commander, ClickUp, and so on) are available to it.

Guardrails:

- It works inside `EDI_TASK_DIR` (your home folder unless you change it) and Claude Code will not write outside it.
- It may only use the tools in `EDI_TASK_TOOLS`: by default reading, searching, editing and writing files,
  `git status/diff/log/add/commit`, `python`, `node`, `npm`, and web search. Anything else is refused and EDI
  tells you what was blocked. Add rules like `Bash(docker:*)` to widen it.
- Tasks that sound destructive (delete, remove, uninstall, format, wipe, kill...) are not started until you say
  **"confirm"**; **"cancel"** drops them. A confirmed task also gets the tools it needs for that kind of work
  (`EDI_TASK_TOOLS_CONFIRMED`: PowerShell, rm/del/Remove-Item, move/rename, all of git, winget, taskkill), which
  an unconfirmed task never has.
- `EDI_TASK_TIMEOUT` (900 seconds) cuts a runaway task off. The Operator is told its budget and asked to
  work in fast, partial steps so it always has something to report; if it still runs out, EDI says so and
  suggests a narrower task (one folder instead of a whole drive, for example).
- Everything is written to `tasks.log` next to her.

## Memory (survives restarts)

- **"Remember that my car is a blue Golf"** → "Noted, sir."
- **"What do you remember?"** — she reads her notes back.
- **"Forget about my car"** — drops matching notes. **"Forget everything"** — wipes them.

Notes and the last few exchanges are kept in `memory.json` next to her, which is git-ignored.
She also picks the conversation up where it left off after a restart.

## Live information (all free, no keys needed)

- **Weather:** "What's the weather like?", "Will it rain tomorrow?", "Weather in Cape Town this week".
  Your location comes from your internet connection; set `EDI_LOCATION=Johannesburg` in `.env` to pin it.
- **Exchange rates:** "What's the rand dollar exchange rate?", "How much is a euro in rand?", "Pound to rand".
- **News and sport:** "What's the latest news in South Africa?", "Any news about Eskom?", "Who won the rugby this weekend?"
- **Facts:** "Search for ..." or "look up ..." — Wikipedia when there is no search key.
- **Real web search (optional):** get a free key at <https://brave.com/search/api> (2,000 searches a month),
  put it in `.env` as `BRAVE_API_KEY=...`, restart her, and "search for the best pizza in Johannesburg" gets proper results.

## Calendar and mail

- **"What's on my calendar today?"**, "Anything tomorrow?", "What's my schedule this week?", "What's on Friday?",
  "When's my next meeting?", "Am I free this afternoon?"
- **"Any new mail?"**, "What's in my inbox?", "Anything from Thandi?", "What's my latest email about?"

**Google (recommended, read-only, no sign-in flow).** Two values in `.env`:

- `GOOGLE_CALENDAR_ICS` — Google Calendar → Settings → your calendar → *Secret address in iCal format*. Several
  calendars: comma-separated. She reads the feed herself (recurring events, exceptions and moved instances included),
  refreshed every ninety seconds; *"check my calendar again"* forces a refresh. Google's own export lags a few minutes
  behind the live calendar, so something you added a moment ago may take a little while to show.
- `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` — Google Account → Security → 2-Step Verification → *App passwords*,
  create one for EDI. She reads Gmail's unread-inbox feed: count, sender, subject and the first line, nothing else.

Restart her after adding them; the briefing picks both up automatically.

**Classic Outlook** is the fallback when Google isn't set up: it is read through Windows automation and needs classic
Outlook signed in to your account (the new Outlook app has no such interface, and she says so instead of guessing).

## The brain

EDI thinks with **Claude Code** when it is installed: she keeps one headless `claude` session alive
in the background (Haiku by default, about a second and a half per reply), so the conversation carries
over from one message to the next and even across restarts. It runs on your Claude subscription, with
no daily cap. The cost that Claude Code prints is the equivalent API price, not a charge, when you are
signed in with a claude.ai plan. Agents use short one-off Claude calls with their own persona.

Sign in once by running `claude` in a terminal. If Claude Code is missing, or `BRAIN=openrouter`, she
uses the OpenRouter key instead (free models there allow about 50 questions a day). If Claude fails
and an OpenRouter key is set, she falls back to it for that reply.

## Settings added to `.env`

| Setting | What it does | Default |
|---|---|---|
| `BRAIN` | `claude` or `openrouter` | Claude Code if installed, else OpenRouter |
| `CLAUDE_MODEL` | The Claude model she talks with | `claude-haiku-5-5` (this machine: Opus 5.5) |
| `CLAUDE_AGENT_MODEL` | The Claude model agents and tasks think with | `claude-opus-5-5` |
| `EDI_LOCATION` | Your city, for the weather | worked out from your connection |
| `BRAVE_API_KEY` | Optional key for real web search | off |

## Saying her name out loud

The screen shows **EDI**; the voice engine is sent "Edie", so she says *ee-dee* instead of spelling it.
The voice-restore protocol is now **"activate edi mode"**.
