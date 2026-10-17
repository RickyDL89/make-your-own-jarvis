"""EDI's eyes: a screenshot of the active window or the whole screen, taken with Windows' own drawing
APIs through a hidden PowerShell call, so there is nothing to install.

Screenshots go to the screens/ folder next to her (git-ignored); only the last few are kept. They leave
the computer only as part of the request to Claude, the same as any image Claude Code reads.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCREENS = ROOT / "screens"

SCREEN_RE = re.compile(
    r"\b(what(?:'s| is) on (?:my|the) screen|what am i looking at|(?:look at|see|check|read) (?:my|the) screen|"
    r"read (?:this|that|it) (?:to me|out|aloud)?\b|read (?:this|that|the) (?:error|message|dialog|page|email|text|code)|"
    r"what does (?:this|that|the|it) (?:error|message|dialog|window|page|email|popup|notification|code|screen)? ?say|"
    r"what(?:'s| is) this (?:error|message|dialog|window|popup|thing)|describe (?:my|the|this) (?:screen|window)|"
    r"can you see (?:this|that|my screen|the screen)|explain (?:this error|this message|what(?:'s| is) on my screen)|"
    r"summari[sz]e (?:this|that|what i'm looking at|my screen|the page|this page|this email)|"
    r"translate (?:this|that|the screen|what's on my screen)|what(?:'s| is) wrong (?:here|with this|on my screen))\b", re.I)
WANTS_WINDOW_RE = re.compile(r"\b(this|that|the) (?:error|message|dialog|window|popup|notification|page|email|code|text)\b|\bthis\b|\bthat\b|\bwindow\b|\bdialog\b", re.I)
TASK_MENTIONS_SCREEN_RE = re.compile(r"\b(screen|window|dialog|popup|this error|that error|what you see|what i'm looking at|on my display|the page i have open)\b", re.I)

CAPTURE_PS = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class EdiWin {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[EdiWin]::SetProcessDPIAware() | Out-Null
$mode = '__MODE__'
$out = '__OUT__'
$title = ''
if ($mode -eq 'window') {
  $h = [EdiWin]::GetForegroundWindow()
  $sb = New-Object System.Text.StringBuilder 512
  [EdiWin]::GetWindowText($h, $sb, 512) | Out-Null
  $title = $sb.ToString()
  $r = New-Object EdiWin+RECT
  [EdiWin]::GetWindowRect($h, [ref]$r) | Out-Null
  $x = $r.Left; $y = $r.Top; $w = $r.Right - $r.Left; $hh = $r.Bottom - $r.Top
  if ($w -lt 80 -or $hh -lt 80 -or $title -like '*E.D.I.*') { $mode = 'screen' }
}
if ($mode -ne 'window') {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $x = $b.X; $y = $b.Y; $w = $b.Width; $hh = $b.Height
}
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, $y, 0, 0, $bmp.Size)
$g.Dispose()
$max = 1800
if ($w -gt $max) {
  $nh = [int]($hh * $max / $w)
  $small = New-Object System.Drawing.Bitmap $bmp, $max, $nh
  $bmp.Dispose(); $bmp = $small
}
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output ("{0}|{1}x{2}|{3}" -f $mode, $w, $hh, $title)
"""


def capture(mode: str = "screen") -> tuple[Path | None, str]:
    """Take a screenshot. mode is 'window' (the active window) or 'screen' (everything).

    Returns (path, description) or (None, reason). Falls back from a window to the whole screen when
    the active window is EDI's own page or too small to mean anything.
    """
    if os.name != "nt":
        return None, "Screen capture is only wired up for Windows."
    SCREENS.mkdir(exist_ok=True)
    out = SCREENS / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    script = CAPTURE_PS.replace("__MODE__", mode).replace("__OUT__", str(out).replace("'", "''"))
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],  # noqa: S603
                              capture_output=True, text=True, timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, "The screen capture took too long."
    except OSError as err:
        return None, f"Couldn't run the screen capture: {err}"
    if proc.returncode != 0 or not out.exists():
        return None, (proc.stderr or proc.stdout).strip()[-200:] or "The screen capture failed."
    used_mode, size, title = (proc.stdout.strip().split("|", 2) + ["", ""])[:3]
    _prune()
    what = f"the active window '{title}'" if used_mode == "window" and title else "the whole screen"
    return out, f"{what}, {size} pixels"


def _prune(keep: int = 4) -> None:
    shots = sorted(SCREENS.glob("screen-*.png"))
    for old in shots[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
