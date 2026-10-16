# EDI — what she can do

EDI is this JARVIS build renamed and given skills. Everything below works by voice (hold **Space**,
or say **"EDI"** with the wake word on) or by typing in the box. She still answers any normal
question with her brain; the skills kick in automatically when what you say matches one.

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

## Outlook calendar and inbox

- **"What's on my calendar today?"**, "Any meetings tomorrow?", "What's my schedule this week?"
- **"Do I have unread emails?"**, "What's in my inbox?"

This reads classic Outlook through Windows automation, so classic Outlook has to be signed in to your account.
If you only use the new Outlook app, she says so instead of guessing. Open classic Outlook once, add your account,
and it starts working.

## Settings added to `.env`

| Setting | What it does | Default |
|---|---|---|
| `EDI_LOCATION` | Your city, for the weather | worked out from your connection |
| `BRAVE_API_KEY` | Optional key for real web search | off |

## Saying her name out loud

The screen shows **EDI**; the voice engine is sent "Edie", so she says *ee-dee* instead of spelling it.
The voice-restore protocol is now **"activate edi mode"**.
