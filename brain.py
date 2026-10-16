"""EDI's brain on Claude Code.

Runs `claude -p` headless as a long-lived process in streaming JSON mode, so each reply costs one
model round trip (about a second and a half on Haiku) instead of a fresh process start, and the
conversation carries over from one message to the next. The session id is saved, so after a restart
she resumes the same conversation. Agents use one-shot calls with their own system prompt.

Needs the Claude Code CLI installed and signed in (run `claude` once in a terminal). Standard
library only, like the rest of EDI.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / "brain_session.json"
DEFAULT_MODEL = "claude-haiku-5-5"
NO_TOOLS = ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""]


class BrainError(Exception):
    pass


def available() -> bool:
    return shutil.which("claude") is not None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):  # EDI may be started from inside Claude Code
        env.pop(key, None)
    return env


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _explain(stderr: str, fallback: str) -> str:
    text = stderr.strip()
    low = text.lower()
    if "login" in low or "not logged in" in low or "authentication" in low or "api key" in low:
        return "Claude Code isn't signed in on this computer. Open a terminal, run claude, and log in."
    if "rate limit" in low or "overloaded" in low:
        return "Claude is rate limited right now."
    return (text[-300:] or fallback)


class Session:
    """One persistent headless Claude conversation."""

    def __init__(self, model: str, system_prompt: str, log) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.session_id: str | None = None
        self.lock = threading.Lock()

    # ---- process

    def _saved_session(self) -> str | None:
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            return data.get("session_id") if data.get("model") == self.model else None
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_session(self) -> None:
        if self.session_id:
            SESSION_FILE.write_text(json.dumps({"session_id": self.session_id, "model": self.model}), encoding="utf-8")

    def forget(self) -> None:
        self.stop()
        self.session_id = None
        try:
            SESSION_FILE.unlink()
        except FileNotFoundError:
            pass

    def start(self, resume: str | None = None) -> None:
        cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", self.model, *NO_TOOLS, "--append-system-prompt", self.system_prompt]
        if resume:
            cmd += ["--resume", resume]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,  # noqa: S603
                                     text=True, encoding="utf-8", errors="replace", env=_env(), bufsize=1,
                                     creationflags=_flags())
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.lines.put(line)
        self.lines.put(None)  # EOF

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def warm(self) -> None:
        """Start the process ahead of time, so the first reply isn't slow."""
        with self.lock:
            if not self.alive():
                resume = self._saved_session()
                self.start(resume)
                self.log(f"Brain: Claude {self.model} starting" + (" (resuming)" if resume else ""))

    # ---- talking

    def ask(self, text: str, timeout: float = 90) -> str:
        with self.lock:
            tried_resume = False
            for attempt in range(2):
                if not self.alive():
                    resume = self._saved_session() if attempt == 0 else None
                    tried_resume = bool(resume)
                    self.start(resume)
                try:
                    return self._exchange(text, timeout)
                except BrainError as err:
                    stderr = ""
                    if self.proc and self.proc.poll() is not None:
                        try:
                            stderr = self.proc.stderr.read()
                        except (OSError, ValueError):
                            stderr = ""
                    self.stop()
                    if attempt == 0 and (tried_resume or "EOF" in str(err)):
                        self.log(f"Brain: restarting Claude session ({str(err)[:80]})")
                        if tried_resume:
                            self.session_id = None
                            try:
                                SESSION_FILE.unlink()
                            except FileNotFoundError:
                                pass
                        continue
                    raise BrainError(_explain(stderr, str(err))) from err
            raise BrainError("Claude didn't answer.")

    def _exchange(self, text: str, timeout: float) -> str:
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as err:
            raise BrainError(f"EOF: {err}") from err
        deadline = time.time() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise BrainError("Claude took too long to answer.")
            if line is None:
                raise BrainError("EOF: Claude exited.")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("session_id"):
                self.session_id = event["session_id"]
                self._save_session()
            elif kind == "result":
                if event.get("session_id"):
                    self.session_id = event["session_id"]
                    self._save_session()
                if event.get("is_error") or event.get("subtype", "success") != "success":
                    raise BrainError(str(event.get("result") or event.get("subtype") or "Claude returned an error"))
                return " ".join(str(event.get("result") or "").split())


def complete(model: str, system: str, prompt: str, timeout: float = 120) -> str:
    """One-shot call with its own system prompt, for agents. Slower to start, but independent of EDI's conversation."""
    cmd = ["claude", "-p", "--output-format", "json", "--model", model, *NO_TOOLS, "--no-session-persistence",
           "--system-prompt", system, prompt]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",  # noqa: S603
                              env=_env(), timeout=timeout, creationflags=_flags())
    except subprocess.TimeoutExpired as err:
        raise BrainError("Claude took too long to answer.") from err
    except OSError as err:
        raise BrainError(str(err)) from err
    try:
        data = json.loads(proc.stdout[proc.stdout.index("{"):]) if "{" in proc.stdout else {}
    except json.JSONDecodeError:
        data = {}
    if proc.returncode != 0 or data.get("is_error") or not data:
        raise BrainError(_explain(proc.stderr, str(data.get("result") or f"claude exited with {proc.returncode}")))
    return " ".join(str(data.get("result") or "").split())
