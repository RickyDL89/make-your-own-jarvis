"""EDI's take-over bridge: "EDI, take over: rename the photos in my downloads by date."

A task runs as a tool-enabled headless Claude Code session (your own Claude Code settings and MCP
servers included), confined to EDI_TASK_DIR, allowed only the tools in EDI_TASK_TOOLS, and cut off
after EDI_TASK_TIMEOUT seconds. Every tool step streams back so the HUD can show it, destructive-
sounding tasks need a spoken confirmation first, and everything is written to tasks.log.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "tasks.log"

TAKEOVER_RE = re.compile(
    r"^(?:edi,?\s+)?(?:please\s+)?(?:can you\s+)?(?:take ?over|take the wheel|take control|do this|handle this|"
    r"go ahead and|run (?:a|this) task|on my computer)\s*[:,]?\s*(?:and\s+)?(.+?)[.!?]*$", re.I)
CONFIRM_RE = re.compile(r"^(?:edi,?\s+)?(?:yes,?\s+)?(?:confirm|confirmed|go ahead|do it|proceed|i confirm)(?:\s+(?:the\s+)?take ?over)?[.!?]*$", re.I)
CANCEL_RE = re.compile(r"^(?:edi,?\s+)?(?:no,?\s+)?(?:cancel|stop|never ?mind|abort|forget it)(?:\s+(?:the\s+|that\s+)?(?:take ?over|task))?[.!?]*$", re.I)
DANGER_RE = re.compile(r"\b(delete|remove|rm|erase|wipe|format|uninstall|kill|shut ?down|reboot|restart the (?:pc|computer|machine)|"
                       r"reset|purge|drop|truncate|overwrite|empty the (?:trash|recycle|bin)|clear (?:all|everything)|move all)\b", re.I)

DEFAULT_TOOLS = ",".join([
    "Read", "Glob", "Grep", "Edit", "Write", "WebSearch", "WebFetch", "Task", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(dir:*)", "Bash(ls:*)", "Bash(Get-ChildItem:*)", "Bash(python:*)", "Bash(py:*)", "Bash(pip:*)",
    "Bash(node:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(mkdir:*)", "Bash(type:*)", "Bash(cat:*)", "Bash(where:*)",
    # read-only looking around: disk usage, finding files, system facts
    "Bash(du:*)", "Bash(df:*)", "Bash(find:*)", "Bash(wc:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(sort:*)",
    "Bash(stat:*)", "Bash(tree:*)", "Bash(tasklist:*)", "Bash(systeminfo:*)", "Bash(wmic:*)", "Bash(fsutil volume:*)",
    "Bash(powershell -NoProfile -Command Get-:*)", "Bash(powershell -Command Get-:*)", "Bash(powershell Get-:*)",
])


# What a task may additionally use once the user has said "confirm" to something destructive.
CONFIRMED_TOOLS = ",".join([
    "PowerShell", "Bash(rm:*)", "Bash(del:*)", "Bash(rmdir:*)", "Bash(Remove-Item:*)", "Bash(mv:*)", "Bash(move:*)",
    "Bash(ren:*)", "Bash(Rename-Item:*)", "Bash(Move-Item:*)", "Bash(git:*)", "Bash(winget:*)", "Bash(taskkill:*)",
])


def settings(cfg: dict[str, str], confirmed: bool = False) -> dict:
    folder = cfg.get("EDI_TASK_DIR") or str(Path.home())
    tools = [t.strip() for t in (cfg.get("EDI_TASK_TOOLS") or DEFAULT_TOOLS).split(",") if t.strip()]
    if confirmed:
        tools += [t.strip() for t in (cfg.get("EDI_TASK_TOOLS_CONFIRMED") or CONFIRMED_TOOLS).split(",") if t.strip()]
    try:
        timeout = int(cfg.get("EDI_TASK_TIMEOUT") or 600)
    except ValueError:
        timeout = 600
    return {"dir": folder, "tools": tools, "timeout": timeout, "model": cfg.get("CLAUDE_AGENT_MODEL") or "claude-opus-5-5"}


def describe_step(name: str, inp: dict) -> str:
    """One short line for the HUD per tool call: what she's doing, not the whole payload."""
    inp = inp or {}
    short = lambda s, n=70: (s if len(s) <= n else s[:n - 1] + "…")  # noqa: E731
    path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit") and path:
        verb = {"Read": "Reading", "Edit": "Editing", "Write": "Writing", "MultiEdit": "Editing", "NotebookEdit": "Editing"}[name]
        return f"{verb} {Path(str(path)).name}"
    if name == "Bash":
        return "Running: " + short(str(inp.get("command", "")).strip().splitlines()[0] if inp.get("command") else "a command")
    if name in ("Glob", "Grep"):
        return f"Searching for {short(str(inp.get('pattern', '')), 40)}"
    if name in ("WebSearch", "WebFetch"):
        return "Looking up " + short(str(inp.get("query") or inp.get("url") or ""), 50)
    if name == "Task":
        return "Delegating: " + short(str(inp.get("description") or inp.get("prompt") or ""), 50)
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"Using {parts[1] if len(parts) > 1 else name}: {parts[-1]}"
    return f"Using {name}"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    return env


def log_line(text: str) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")


def run(cfg: dict[str, str], task: str, on_step, confirmed: bool = False) -> dict:
    """Run one task to completion. on_step(text) is called for every tool call as it happens.
    `confirmed` means the user said yes to a destructive-sounding task, which unlocks the extra tools.

    Returns {"result": str, "steps": int, "denied": [str], "error": str | None, "seconds": float, "cost": float}.
    """
    s = settings(cfg, confirmed)
    folder = Path(s["dir"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    system = (
        "You are the Operator, the hands of EDI, a voice assistant on this computer. EDI has delegated a task "
        f"from the user to you. Work inside {folder} unless the task clearly names another place. Do the task "
        "carefully and completely, prefer reversible actions, and never ask questions: if something is ambiguous, "
        "make a sensible choice and say which. When you are done, reply with a short plain-text report of what you "
        "did and anything the user should know, in at most five sentences, no markdown."
    )
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", s["model"],
           "--allowedTools", *s["tools"], "--append-system-prompt", system, task]
    log_line(f"START  {task}" + ("  (confirmed by the user)" if confirmed else ""))
    log_line(f"       dir={folder} model={s['model']} tools={len(s['tools'])}")
    started = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=str(folder), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,  # noqa: S603
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                env=_env(), bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as err:
        log_line(f"FAILED to start: {err}")
        return {"result": "", "steps": 0, "denied": [], "error": f"Couldn't start Claude Code: {err}", "seconds": 0, "cost": 0}

    out = {"result": "", "steps": 0, "denied": [], "error": None, "seconds": 0.0, "cost": 0.0}
    stderr_lines: list[str] = []
    threading.Thread(target=lambda: stderr_lines.extend(proc.stderr.readlines()), daemon=True).start()
    timer = threading.Timer(s["timeout"], proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        out["steps"] += 1
                        step = describe_step(str(block.get("name")), block.get("input") or {})
                        log_line(f"STEP   {step}")
                        on_step(step)
            elif kind == "result":
                out["result"] = " ".join(str(event.get("result") or "").split())
                out["cost"] = float(event.get("total_cost_usd") or 0)
                for d in event.get("permission_denials") or []:
                    out["denied"].append(describe_step(str(d.get("tool_name")), d.get("tool_input") or {}))
                if event.get("is_error") or event.get("subtype", "success") != "success":
                    out["error"] = out["result"] or str(event.get("subtype") or "Claude Code returned an error")
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    out["seconds"] = time.time() - started
    if proc.returncode not in (0, None) and not out["result"]:
        tail = " ".join(stderr_lines)[-300:].strip()
        out["error"] = out["error"] or (tail or f"Claude Code exited with code {proc.returncode}")
        if out["seconds"] >= s["timeout"] - 1:
            out["error"] = f"The task ran out of time after {s['timeout']} seconds."
    log_line(f"END    steps={out['steps']} denied={len(out['denied'])} seconds={out['seconds']:.0f} cost=${out['cost']:.3f}"
             + (f" error={out['error']}" if out["error"] else ""))
    log_line(f"RESULT {out['result'][:1000]}")
    return out
