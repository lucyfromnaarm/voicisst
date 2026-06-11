# Platform setup

What Voicisst needs from each OS, and how to give it that with the least
friction. After any setup change, `voicisst selftest` tells you whether it
worked.

The web UI (`voicisst ui`, see [UI.md](UI.md)) works identically on every
platform — it runs in your normal browser, so nothing below applies to it.
The platform differences are about hotkeys, typing, and audio. The UI's
setup wizard knows about them too: it shows the permission steps for the OS
you're actually on and skips the rest.

## Linux

Voicisst supports both Wayland and X11. Two things need system-level setup:

- **Hotkeys** are read from `/dev/input/event*` via evdev, which requires
  membership in the `input` group. (If that's not available, Voicisst falls back
  to pynput, which works on X11 but not on most Wayland compositors.)
- **Typing/pasting** on Wayland goes through `ydotool`, whose daemon
  `ydotoold` creates a virtual keyboard via `/dev/uinput`.

### The easy way

```bash
./scripts/setup-linux.sh
```

The script is idempotent and works on dnf/apt/pacman/zypper distros. It:

1. installs `ydotool`, `wl-clipboard`, PortAudio, and `libnotify`;
2. loads the `uinput` kernel module now and at boot;
3. writes a udev rule giving the `input` group access to `/dev/uinput`
   (`KERNEL=="uinput", GROUP="input", MODE="0660"`);
4. adds you to the `input` group;
5. installs and starts a *user* systemd unit for `ydotoold` — a user-level
   `ydotoold` keeps its socket in your `XDG_RUNTIME_DIR` instead of a
   root-owned `/tmp` socket you can't use (it disables a system-wide
   `ydotoold` if one is enabled).

**Log out and back in** after the script adds you to the `input` group;
group membership only applies to new sessions.

Then:

```bash
voicisst selftest
voicisst run                                      # run in the foreground first
```

To start Voicisst at login, install the user unit from `packaging/systemd/`
(the script prints these exact commands when it finishes):

```bash
install -Dm0644 packaging/systemd/voicisst.service ~/.config/systemd/user/voicisst.service
systemctl --user daemon-reload
systemctl --user enable --now voicisst.service
journalctl --user -u voicisst -f                  # watch the logs
```

The unit expects `voicisst` at `~/.local/bin/voicisst` (the pipx/uv default) — edit
`ExecStart` if `command -v voicisst` says yours lives elsewhere.

### Wayland notes

- ydotool synthesizes input at the kernel level, so it works on every
  compositor — GNOME, KDE, Sway, Hyprland.
- The ydotoold socket lives at `$XDG_RUNTIME_DIR/.ydotool_socket`; the
  `YDOTOOL_SOCKET` environment variable overrides the location. If a
  system-wide `ydotoold` service is running, disable it — its socket is
  root-owned and Voicisst can't reach it.
- **GNOME focused-window caveat:** Voicisst checks the focused window's class
  only to detect terminals (where it copies text instead of pasting). GNOME
  Wayland has no public API for this; Voicisst tries a GNOME Shell extension
  D-Bus call (a "Window Calls"-style extension) and gives up quietly if none
  is installed. Without it, terminal detection doesn't work on GNOME — if
  you dictate into terminals a lot, either install such an extension or set
  `output.paste_chord = "ctrl-shift-v"`. Sway and Hyprland are detected via
  `swaymsg` / `hyprctl` and work out of the box.
- Clipboard and primary selection use `wl-clipboard` (`wl-copy`,
  `wl-paste`).

### X11 notes

- Typing/pasting uses `xdotool` (`xdotool type --delay`, `key BackSpace`)
  when available, with pynput as a fallback. Install `xdotool` from your
  package manager.
- Clipboard uses `xclip`.
- Hotkeys: evdev if you're in the `input` group, else pynput's global
  listener works fine on X11.

### Defaults

Hotkeys are `KEY_COMPOSE` / `KEY_MENU` — the Menu key, to the right of the
spacebar between AltGr and Ctrl on most full-size keyboards. No Menu key?
Pick anything: `keys = ["KEY_F9"]` or `["KEY_RIGHTALT"]` in the `[hotkey]`
section.

## macOS

```bash
pipx install "voicisst[local,ui]"
voicisst selftest
voicisst
```

Default hotkey: **right Option** (`alt_r`), hold to talk. Pasting uses Cmd+V.

macOS gates everything Voicisst does behind permission prompts. The setup
wizard (`voicisst ui`) walks you through all three; here they are in full.
They all live in **System Settings → Privacy & Security**:

1. **Microphone** — prompted automatically the first time Voicisst records. If
   you missed the prompt: System Settings → Privacy & Security → Microphone,
   enable your terminal app (or the Voicisst binary, if you use the release
   build).
2. **Accessibility** — required for Voicisst to type into other apps. System
   Settings → Privacy & Security → Accessibility → "+" → add the app that
   launches Voicisst (Terminal.app, iTerm2, or the Voicisst binary). Without this,
   dictation transcribes but nothing appears in the focused window.
3. **Input Monitoring** — required for the global hotkey listener. System
   Settings → Privacy & Security → Input Monitoring → add the same app.
   Without this, the hotkey never fires.

Note that the permission attaches to the *launching* app: if you grant
Terminal.app and later run Voicisst from iTerm2, you'll grant it again. After
changing a permission, restart Voicisst.

To start Voicisst at login, use the LaunchAgent template at
`packaging/macos/one.octavia.voicisst.plist` (it runs
`~/.local/bin/voicisst run` and logs to `~/Library/Logs/voicisst.log`):

```bash
cp packaging/macos/one.octavia.voicisst.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/one.octavia.voicisst.plist
# older macOS: launchctl load -w ~/Library/LaunchAgents/one.octavia.voicisst.plist
```

## Windows

```powershell
irm https://raw.githubusercontent.com/lucyfromnaarm/voicisst/main/scripts/install.ps1 | iex
```

or `pipx install "voicisst[local,ui]"`, or unzip the release binary.

Default hotkey: **right Ctrl** (`ctrl_r`), hold to talk. Hotkeys and typing
both use pynput; pasting is Ctrl+V.

**Antivirus note:** Voicisst listens for a global hotkey, which means installing
a system-wide keyboard hook — the same mechanism keyloggers use, so some
antivirus products flag it. Voicisst only acts on your configured hotkey (plus
Backspace, to let you cancel a polish in progress) and never sends keystrokes
anywhere; the listener is ~200 lines you can read at
`src/voicisst/hotkeys/pynput_listener.py`. If your AV blocks it, add an
exclusion for the Voicisst executable.

**Start at login:** press Win+R, run `shell:startup`, and create a shortcut
there pointing at `voicisst.exe` (pipx installs it under
`%USERPROFILE%\.local\bin`; `where voicisst` shows the path). Set the shortcut to
run minimized if you don't want a console window.

## Hardware notes (all platforms)

- GPU: the defaults (Whisper `large-v3-turbo` + `qwen3.5:4b` for polish) fit
  in about 8 GB of VRAM. faster-whisper on CUDA needs the NVIDIA driver plus
  cuBLAS/cuDNN libraries.
- CPU-only: `whisper.model = "auto"` resolves to `small` on CPU, which is
  serviceable. Consider `polish.enabled = false` if polish latency on CPU
  bothers you — or run the models on another machine entirely
  ([SERVER.md](SERVER.md)).
- Audio capture uses PortAudio via the sounddevice library; the Linux setup
  script installs it (`libportaudio2` on Debian/Ubuntu). macOS and Windows
  builds bundle it.
