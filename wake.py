"""A local wake word: EDI hears her name through the microphone herself, offline, with no browser tab needed.

Uses Vosk (an offline speech recogniser) in keyword mode, with a small English model that downloads itself
on first use (about 40 MB into models/). Needs two optional packages:  pip install vosk sounddevice
Without them EDI falls back to the wake word in the browser page, which only works while the tab is open.

When her name is heard: if a page is open it chimes and listens there (Chrome hears sentences better);
if no page is open she listens here too, answers through the speakers, and nobody needs a browser at all.
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import threading
import time
import urllib.request
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
CHIME = MODELS / "chime.wav"
RATE = 16000
DEFAULT_WORDS = "edie,eddie,hey edie,hey eddie,e d i"
# How the dictation recogniser tends to hear her name. An utterance that starts with one of these wakes her too.
MISHEARD = ["edie", "eddie", "eddy", "easy", "eighty", "idi", "e d i", "a d i", "ed e", "eedee", "edi",
            "hey edie", "hey eddie", "hey easy", "hey eighty", "okay edie", "ok edie", "hi edie"]

try:
    import sounddevice as sd  # type: ignore
    import vosk  # type: ignore
    vosk.SetLogLevel(-1)
    AVAILABLE = True
except Exception:  # noqa: BLE001 - optional packages
    AVAILABLE = False


def available() -> bool:
    return AVAILABLE and os.name == "nt"


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ensure_model(log) -> Path | None:
    path = MODELS / MODEL_NAME
    if path.is_dir():
        return path
    MODELS.mkdir(exist_ok=True)
    zip_path = MODELS / f"{MODEL_NAME}.zip"
    log(f"Wake: downloading the offline speech model ({MODEL_NAME}, about 40 MB)...")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response, zip_path.open("wb") as out:  # noqa: S310 - fixed https URL
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(MODELS)
        zip_path.unlink(missing_ok=True)
    except Exception as err:  # noqa: BLE001
        log(f"Wake: couldn't fetch the speech model ({err}); the local wake word is off")
        return None
    log("Wake: speech model ready")
    return path if path.is_dir() else None


def ensure_chime() -> Path:
    """A short two-note chime as a WAV, generated once."""
    if CHIME.exists():
        return CHIME
    MODELS.mkdir(exist_ok=True)
    frames = bytearray()
    for freq, length in ((880, 0.1), (1320, 0.14)):
        n = int(RATE * length)
        for i in range(n):
            env = min(1.0, i / (RATE * 0.01), (n - i) / (RATE * 0.04))
            frames += struct.pack("<h", int(0.35 * 32767 * env * math.sin(2 * math.pi * freq * i / RATE)))
    with wave.open(str(CHIME), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(bytes(frames))
    return CHIME


def play_wav(path: Path) -> None:
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command",  # noqa: S603
                      f"(New-Object System.Media.SoundPlayer '{path}').PlaySync()"], creationflags=_flags())


def play_mp3(path: Path, timeout: float = 120) -> None:
    """Play an MP3 through the default speakers and wait for it to finish (PowerShell's MediaPlayer)."""
    script = (
        "Add-Type -AssemblyName PresentationCore; $p = New-Object System.Windows.Media.MediaPlayer; "
        f"$p.Open([uri]'{path.resolve().as_uri()}'); $p.Play(); $t = 0; "
        "while (-not $p.NaturalDuration.HasTimeSpan -and $t -lt 50) { Start-Sleep -Milliseconds 100; $t++ }; "
        "if ($p.NaturalDuration.HasTimeSpan) { Start-Sleep -Milliseconds ([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds + 250) }; $p.Close()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],  # noqa: S603
                   timeout=timeout, creationflags=_flags(), capture_output=True)


def strip_wake(text: str, words: list[str]) -> str:
    """'edie what time is it' -> 'what time is it'."""
    t = " " + " ".join(text.split()) + " "
    for w in sorted(words, key=len, reverse=True):
        t = t.replace(f" {w} ", " ")
    return " ".join(t.replace("[unk]", " ").split())


class Listener(threading.Thread):
    """Listens for the wake word forever. Two recognisers share the microphone: a keyword one that only
    knows her name, and a dictation one, so when she hears 'Edie, what time is it' in one breath the rest
    of the sentence comes along. on_wake(text) returns True when she should also capture the command
    here (no page open and nothing was said after the name); then on_command(text) is called."""

    def __init__(self, cfg: dict, on_wake, on_command, log) -> None:
        super().__init__(daemon=True, name="wake-word")
        self.words = [w.strip().lower() for w in (cfg.get("EDI_WAKE_WORDS") or DEFAULT_WORDS).split(",") if w.strip()]
        self.device = cfg.get("EDI_MIC") or None
        self.on_wake, self.on_command, self.log = on_wake, on_command, log
        self.ok = False
        self._last = 0.0

    def _device_index(self):
        if not self.device:
            return None
        for i, d in enumerate(sd.query_devices()):
            if self.device.lower() in d["name"].lower() and d["max_input_channels"] > 0:
                return i
        self.log(f"Wake: no microphone matching '{self.device}', using the default")
        return None

    def run(self) -> None:
        model_path = ensure_model(self.log)
        if not model_path:
            return
        try:
            model = vosk.Model(str(model_path))
            keyword = vosk.KaldiRecognizer(model, RATE, json.dumps(self.words + ["[unk]"]))
            dictation = vosk.KaldiRecognizer(model, RATE)
            stream = sd.RawInputStream(samplerate=RATE, blocksize=2000, dtype="int16", channels=1, device=self._device_index())
            stream.start()
        except Exception as err:  # noqa: BLE001
            self.log(f"Wake: couldn't start the microphone ({err}); the local wake word is off")
            return
        self.ok = True
        self.log(f"Wake: listening for {', '.join(self.words[:3])}... (offline)")
        while True:
            try:
                self.step(stream, keyword, dictation)
            except Exception as err:  # noqa: BLE001 - keep listening whatever happens
                if "stopped" in str(err).lower() or "closed" in str(err).lower():
                    return  # the stream went away (shutdown)
                self.log(f"Wake: {err!r}")
                time.sleep(1)

    def _starts_with_name(self, text: str) -> str | None:
        """'eighty what time is it' -> 'eighty' when an utterance opens with one of the ways her name is heard."""
        t = " " + " ".join(text.split()) + " "
        for v in sorted(set(self.words + MISHEARD), key=len, reverse=True):
            if t.startswith(f" {v} "):
                return v
        return None

    def step(self, stream, keyword, dictation) -> None:
        """One read from the microphone. Anything with a .read(n) -> (bytes, flag) works as the stream."""
        data, _ = stream.read(2000)
        data = bytes(data)
        heard = ""
        # Two detectors on the same audio: the keyword grammar (reports when the utterance ends) and the
        # dictation recogniser's running text (reports as soon as the utterance opens with her name).
        dictation_done = dictation.AcceptWaveform(data)
        running = json.loads(dictation.Result() if dictation_done else dictation.PartialResult())
        said = running.get("text") if dictation_done else running.get("partial", "")
        said = said or ""
        opener = self._starts_with_name(said) if said else None
        if keyword.AcceptWaveform(data):
            kw = json.loads(keyword.Result()).get("text", "")
            if kw and any(f" {w} " in f" {kw} " for w in self.words):
                heard = kw
        if not heard and opener and (dictation_done or len(said.split()) >= 2):
            heard = opener
        if not heard:
            return
        if time.time() - self._last < 2.5:
            return
        self._last = time.time()
        # Whatever was said after the name so far, plus whatever follows until the next pause, is the command.
        if not dictation_done:
            said = json.loads(dictation.FinalResult()).get("text", "") or said
        rest = strip_wake(said, self.words + MISHEARD)
        more = "" if dictation_done else self._capture(stream, dictation, max_seconds=4.0, quiet=True)
        command = " ".join((rest + " " + more).split())
        self.log(f"Wake: heard '{heard}'" + (f" then '{command}'" if command else ""))
        if len(command.split()) < 2:
            command = ""
        if self.on_wake(command):
            text = self._capture(stream, dictation)
            if text:
                self.on_command(text)
        keyword.Reset()
        dictation.Reset()
        self._last = time.time()

    def _capture(self, stream, rec, max_seconds: float = 8.0, quiet: bool = False) -> str:
        """Listen for one sentence: until a pause, or the time is up."""
        rec.Reset()
        started = time.time()
        text = ""
        while time.time() - started < max_seconds:
            data, _ = stream.read(2000)
            if rec.AcceptWaveform(bytes(data)):
                text = json.loads(rec.Result()).get("text", "").strip()
                break
        if not text:
            text = json.loads(rec.FinalResult()).get("text", "").strip()
        if not quiet:
            self.log(f"Wake: command '{text}'" if text else "Wake: heard nothing after the chime")
        return text
