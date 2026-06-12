"""stderr logging + best-effort desktop notifications.

`notify()` always logs `[summary] body` to stderr so headless runs and
journald keep a trace. When `enabled`, it additionally fires a desktop
notification appropriate for the platform — fire-and-forget via
`subprocess.Popen` with output devnulled. It never raises: a missing
notify-send/osascript/powershell must not take dictation down with it.
"""

from __future__ import annotations

import subprocess
import sys

# Desktop bubbles auto-expire (GNOME stacked them indefinitely otherwise).
_EXPIRE_MS = 3000
# Fixed notification ID: each bubble REPLACES the previous one, so rapid
# events (rejected takes, repeated errors) never pile up into a wall of
# notifications — there is at most one Voicisst bubble at any time.
_REPLACE_ID = "812731"
_VALID_URGENCIES = ("low", "normal", "critical")

# Windows toasts are silently dropped unless CreateToastNotifier is given a
# *registered* AppUserModelID. Voicisst doesn't install one, so borrow Windows
# PowerShell's AUMID, which every Windows installation registers.
_WIN_PS_AUMID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
)


def notify(summary: str, body: str = "", urgency: str = "low", *, enabled: bool = True) -> None:
    """Log to stderr; optionally show a best-effort desktop notification."""
    print(f"[{summary}] {body}", file=sys.stderr)
    if not enabled:
        return
    if urgency not in _VALID_URGENCIES:
        urgency = "normal"
    try:
        if sys.platform.startswith("linux"):
            _notify_linux(summary, body, urgency)
        elif sys.platform == "darwin":
            _notify_darwin(summary, body)
        elif sys.platform in ("win32", "cygwin"):
            _notify_windows(summary, body)
    except Exception:
        pass  # notifications are decorative — never let them raise


def _spawn(cmd: list[str]) -> None:
    """Fire-and-forget: never block dictation on a notification daemon."""
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # No console-window flash on Windows; harmless 0 elsewhere.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _notify_linux(summary: str, body: str, urgency: str) -> None:
    _spawn(
        [
            "notify-send",
            "--app-name=Voicisst",
            "-r",
            _REPLACE_ID,
            "-u",
            urgency,
            "-t",
            str(_EXPIRE_MS),
            "--",
            summary,
            body,
        ]
    )


def _osa_escape(s: str) -> str:
    """Escape for embedding inside an AppleScript double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify_darwin(summary: str, body: str) -> None:
    script = f'display notification "{_osa_escape(body)}" with title "{_osa_escape(summary)}"'
    _spawn(["osascript", "-e", script])


def _ps_escape(s: str) -> str:
    """Escape for embedding inside a PowerShell single-quoted string."""
    return s.replace("'", "''")


def _notify_windows(summary: str, body: str) -> None:
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t=$x.GetElementsByTagName('text');"
        f"[void]$t.Item(0).AppendChild($x.CreateTextNode('{_ps_escape(summary)}'));"
        f"[void]$t.Item(1).AppendChild($x.CreateTextNode('{_ps_escape(body)}'));"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"'{_WIN_PS_AUMID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x));"
    )
    try:
        _spawn(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ]
        )
        return
    except OSError:
        pass  # no powershell on PATH — fall back to msg.exe
    text = f"{summary}: {body}" if body else summary
    _spawn(["msg", "*", "/TIME:5", text])
