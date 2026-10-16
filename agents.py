"""EDI's agents: helpers she spawns for a task, visible on the HUD while they work.

Say "spawn a research agent to find the latest news about Eskom", "delegate the weekend
forecast to a weather agent", or "test your agents". Each agent runs on its own thread with its
own persona, can use EDI's live-data skills, may ask EDI one clarifying question, and reports
back. Everything they say to each other is published as events, which the HUD draws as nebulae
with message packets flying between them.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from collections import deque

ROLES = {
    "research": ("Researcher", "You find facts and current information and report them accurately, with the key numbers and names."),
    "analyst": ("Analyst", "You weigh options and numbers and give a clear recommendation with the reason."),
    "analysis": ("Analyst", "You weigh options and numbers and give a clear recommendation with the reason."),
    "writer": ("Writer", "You draft short, polished text exactly as asked."),
    "writing": ("Writer", "You draft short, polished text exactly as asked."),
    "coder": ("Coder", "You reason about code and technical tasks and give precise, concrete steps."),
    "coding": ("Coder", "You reason about code and technical tasks and give precise, concrete steps."),
    "code": ("Coder", "You reason about code and technical tasks and give precise, concrete steps."),
    "developer": ("Coder", "You reason about code and technical tasks and give precise, concrete steps."),
    "planner": ("Planner", "You break goals into a few ordered steps with rough time estimates."),
    "planning": ("Planner", "You break goals into a few ordered steps with rough time estimates."),
    "weather": ("Meteorologist", "You report weather and forecasts and what they mean for the user's plans."),
    "finance": ("Analyst", "You explain money questions plainly, with the numbers."),
    "travel": ("Navigator", "You plan routes and trips and flag what could go wrong."),
    "scout": ("Scout", "You look things up fast and report only what matters."),
}
HUES = [28, 135, 275, 330, 215, 60]  # each agent gets a fixed colour, in spawn order
ROLE_WORDS = "|".join(sorted(ROLES, key=len, reverse=True))

SPAWN_RE = re.compile(
    r"^(?:edi,?\s+)?(?:please\s+)?(?:can you\s+)?(?:spawn|create|launch|start|summon|call(?: on| in| up)?|get|ask|have|"
    r"send|dispatch|assign|deploy|bring in|use)\s+(?:an?\s+|another\s+|the\s+|your\s+)?(?:(\w+)\s+)?agent\s*"
    r"(?:to|that|and|who|which|:|,)?\s*(.*?)[.!?]*$", re.I)
DELEGATE_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?delegate\s+(.+?)\s+to\s+(?:an?\s+)?(?:(\w+)\s+)?agent[.!?]*$", re.I)
# Speech recognition turns "spawn a research agent" into "spoon a research agent" or "spooner researcher",
# so also accept any sentence that mentions an agent, or a role by name, followed by a task.
ANY_AGENT_RE = re.compile(r"\b(?:an?|another|the|your|one)\s+(?:(\w+)\s+)?agents?\b\s*(?:to|that|who|which|and|:|,)?\s*(.*?)[.!?]*$", re.I)
ROLE_NOUNS = {"researcher": "research", "analyst": "analyst", "writer": "writer", "coder": "coder", "developer": "coder",
              "planner": "planner", "meteorologist": "weather", "navigator": "travel", "scout": "scout"}
ROLE_NOUN_RE = re.compile(r"^(?:\w+\W+){0,3}?(?:an?\s+|another\s+|the\s+)?(" + "|".join(ROLE_NOUNS) + r")\b\s*(?:to|that|who|which|and|:|,)?\s*(.*?)[.!?]*$", re.I)
TEST_RE = re.compile(r"^(?:edi,?\s+)?(?:please\s+)?(?:(?:run|do|start)\s+(?:an?\s+|the\s+)?agent\s+(?:test|demo)|"
                     r"(?:test|demo|try(?: out)?|show me)\s+(?:your\s+|the\s+)?agents?(?:\s+system)?)[.!?]*$", re.I)

DEMO_TASKS = [
    ("research", "find out what the weather will be like for the user's plans this weekend"),
    ("analyst", "work out roughly how long the user's drive to work will take tomorrow morning"),
]


class Runtime:
    def __init__(self, *, llm, think, live_context, log) -> None:
        self._llm = llm                    # llm(cfg, messages) -> str
        self._think = think                # think(cfg, text, context=None, remember=True) -> str
        self._live_context = live_context  # live_context(cfg, text) -> str | None
        self._log = log
        self._events: deque = deque(maxlen=400)
        self._seq = 0
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.agents: dict[int, dict] = {}

    # ---- events the HUD polls

    def emit(self, kind: str, **data) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": kind, "time": time.time(), **data})

    def events_since(self, since: int) -> tuple[list[dict], int]:
        with self._lock:
            return [e for e in self._events if e["seq"] > since], self._seq

    def active(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self.agents.values() if a["status"] != "gone"]

    def claim(self, seq: int) -> bool:
        """The first HUD to claim an announcement speaks it; every other open tab or browser only shows it."""
        with self._lock:
            if not hasattr(self, "_claimed"):
                self._claimed: set[int] = set()
            if seq in self._claimed:
                return False
            self._claimed.add(seq)
            if len(self._claimed) > 1000:
                self._claimed = set(sorted(self._claimed)[-500:])
            return True

    # ---- what the user said

    def parse(self, text: str) -> dict | None:
        if TEST_RE.match(text):
            return {"demo": True}
        m = DELEGATE_RE.match(text)
        if m:
            return {"role": m.group(2), "task": m.group(1).strip()}
        m = SPAWN_RE.match(text)
        if m:
            return {"role": m.group(1), "task": m.group(2).strip()}
        m = ANY_AGENT_RE.search(text)
        not_a_request = r"\b(what|how|why|which|do you|are you|is there|was|were|about|tell me|explain|define)\b"
        if m and not re.search(not_a_request, text[:m.start()], re.I):
            return {"role": m.group(1), "task": m.group(2).strip()}
        m = ROLE_NOUN_RE.match(text)
        if m and not re.search(not_a_request, text[:m.start(1)], re.I):
            return {"role": ROLE_NOUNS[m.group(1).lower()], "task": m.group(2).strip()}
        return None

    def missing_task_reply(self, role_word: str | None, you: str) -> str:
        name = ROLES.get((role_word or "").lower(), ("agent", ""))[0].lower()
        return f"Happy to, {you}. What should the {name} work on? Say the task after the word to, for example: get a researcher to find tomorrow's weather."

    # ---- spawning

    def _new_agent(self, role_word: str | None, task: str) -> dict:
        role = (role_word or "").lower()
        name, brief = ROLES.get(role, (None, None))
        if not name:
            name = f"{role.capitalize()} agent" if role and role not in ("new", "second", "another", "other", "sub") else "Agent"
            brief = "You are a capable generalist and do exactly the task you are given."
        agent_id = next(self._ids)
        taken = {a["name"] for a in self.active()}
        if name in taken:
            name = f"{name} {sum(1 for a in self.agents.values() if a['name'].startswith(name)) + 1}"
        agent = {"id": agent_id, "name": name, "role": brief, "hue": HUES[(agent_id - 1) % len(HUES)],
                 "task": task, "status": "spawning", "started": time.time()}
        with self._lock:
            self.agents[agent_id] = agent
        return agent

    def spawn(self, cfg: dict, role_word: str | None, task: str, *, force_question: bool = False) -> dict:
        agent = self._new_agent(role_word, task)
        self.emit("agent_spawn", agent=dict(agent))
        threading.Thread(target=self._run, args=(cfg, agent, force_question), daemon=True, name=f"agent-{agent['id']}").start()
        return agent

    def demo(self, cfg: dict) -> list[dict]:
        return [self.spawn(cfg, role, task, force_question=True) for role, task in DEMO_TASKS]

    def acknowledgement(self, agents: list[dict], you: str) -> str:
        names = " and ".join(a["name"].lower() if a["name"] != "Agent" else "an agent" for a in agents)
        article = "" if len(agents) > 1 or agents[0]["name"] == "Agent" else ("an " if agents[0]["name"][0] in "AEIOU" else "a ")
        return f"Spawning {article}{names}, {you}. I'll report back when they're done." if len(agents) > 1 else \
               f"Spawning {article}{names}, {you}. I'll report back shortly."

    # ---- the agent's life

    def _status(self, agent: dict, status: str) -> None:
        with self._lock:
            agent["status"] = status
        self.emit("agent_status", id=agent["id"], status=status)

    def _say(self, sender: str, receiver: str, text: str, agent_id: int) -> None:
        self._log(f"{sender} -> {receiver}: {text}")
        self.emit("message", id=agent_id, **{"from": sender, "to": receiver}, text=text)

    def _persona(self, agent: dict, cfg: dict) -> str:
        return (f"You are {agent['name']}, an agent spawned by EDI, the user's personal AI assistant. {agent['role']} "
                "You are talking to EDI, not to the user, so address EDI. Plain text only, no markdown, no lists, "
                f"at most four sentences. The user is called {cfg['JARVIS_CALLS_YOU']}. "
                f"The local date and time is {time.strftime('%A, %B %d, %Y, %I:%M %p')}.")

    def _run(self, cfg: dict, agent: dict, force_question: bool) -> None:
        name, task = agent["name"], agent["task"] or "report what you can do"
        you = cfg["JARVIS_CALLS_YOU"]
        try:
            time.sleep(0.8)  # let the HUD draw the spawn before the first packet flies
            self._say("EDI", name, f"{name}, I need you to {task}. Report back to me briefly.", agent["id"])
            self._status(agent, "working")
            context = None
            try:
                context = self._live_context(cfg, task)
            except Exception as err:  # noqa: BLE001 - live data is a bonus, not a requirement
                self._log(f"{name}: live data failed: {err!r}")
            system = [{"role": "system", "content": self._persona(agent, cfg)}]
            if context:
                system.append({"role": "system", "content": "Live data you fetched for this task: " + context})
            if force_question:
                instruction = ("Before doing anything, reply with exactly 'QUESTION: ' followed by one short clarifying "
                               "question for EDI that you genuinely need answered to do this task well.")
            else:
                instruction = ("If you genuinely need one piece of information from EDI before you can do this, reply with "
                               "exactly 'QUESTION: ' followed by one short question. Otherwise reply with your report.")
            first = self._llm(cfg, system + [{"role": "user", "content": f"EDI's brief: {task}. {instruction}"}])
            report = first
            m = re.match(r"\s*QUESTION:\s*(.+)", first, re.I | re.S)
            if m:
                question = " ".join(m.group(1).split())[:400]
                self._say(name, "EDI", question, agent["id"])
                self._status(agent, "asking EDI")
                answer = self._think(cfg, f"[{name} asks] {question}",
                                     context=(f"Your agent {name} is working on the task '{task}' and asks you this question. "
                                              "Answer it in one or two sentences from what you know about the user and the "
                                              "conversation. If you don't know, make a sensible assumption and say it is one."),
                                     remember=False)
                self._say("EDI", name, answer, agent["id"])
                self._status(agent, "working")
                report = self._llm(cfg, system + [
                    {"role": "user", "content": f"EDI's brief: {task}. You asked: {question}"},
                    {"role": "assistant", "content": f"QUESTION: {question}"},
                    {"role": "user", "content": f"EDI answers: {answer}. Now do the task and give your report."},
                ])
            report = " ".join(report.split())[:900] or "I have nothing useful to report."
            self._say(name, "EDI", report, agent["id"])
            self._status(agent, "done")
            summary = self._think(cfg, f"[Report from {name}] {report}",
                                  context=(f"Your agent {name} has just reported back on the task '{task}'. Tell the user "
                                           "the result in one to three spoken sentences, as EDI, crediting the agent in passing."),
                                  remember=True)
            self.emit("agent_done", id=agent["id"], speak=summary)
        except Exception as err:  # noqa: BLE001 - an agent failing must never take EDI down
            self._log(f"{name} failed: {err!r}")
            reason = str(err)[:120]
            self._say(name, "EDI", f"I ran into a problem and couldn't finish: {reason}", agent["id"])
            self._status(agent, "failed")
            self.emit("agent_done", id=agent["id"], speak=f"The {name.lower()} couldn't finish, {you}. The reason is in the EDI window.")
        finally:
            def retire() -> None:
                time.sleep(12)
                with self._lock:
                    agent["status"] = "gone"
            threading.Thread(target=retire, daemon=True).start()
