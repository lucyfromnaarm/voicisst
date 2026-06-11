# The web UI

Voicisst's UI is a small web app that Voicisst serves itself, on your own
machine, in your normal browser. No Electron, no Node, no build step — just
a settings page with three jobs:

- a **setup wizard** that gets you from "just installed" to dictating,
- a **settings editor** so you never have to hand-edit TOML (unless you
  want to — there's a raw editor too),
- a **live dashboard** that shows what dictation is doing right now.

It needs the `ui` extra. The install scripts include it; if you installed
by hand, add it:

```bash
pipx install "voicisst[local,ui]"
```

## Starting it

Two ways, depending on what you want:

```bash
voicisst ui            # the UI on its own: setup and settings
voicisst run --ui      # dictation + the UI: the dashboard goes live
```

`voicisst ui` is for setup and configuration. Dictation isn't running, so
the dashboard just says so — everything else works.

`voicisst run --ui` runs normal dictation and the UI together. The
dashboard now shows your dictation state in real time, updating as you
press the hotkey, speak, and release.

Either way, Voicisst prints the UI's address in the terminal and opens it in
your default browser (turn that off with `ui.open_browser = false` or
`--no-browser`). The UI keeps working if you close the tab; just reopen
the printed URL.

Useful flags and settings:

| What | How |
|---|---|
| Different port | `voicisst ui --port 9000`, or `web_port` under `[ui]` in the config |
| Don't open the browser | `voicisst ui --no-browser`, or `open_browser = false` under `[ui]` |

## Only you can open it

The UI can read and write your Voicisst config, so access is locked down two
ways:

1. **It only listens on your own machine.** The server binds to
   `127.0.0.1`, always. Nothing on your network can reach it, and there is
   no setting to change that.
2. **Every page needs a secret token.** Each time the UI starts, Voicisst
   generates a fresh random token and puts it in the URL it prints and
   opens — something like `http://127.0.0.1:8766/?t=Kx3...`. Without that
   token the server answers 403 to everything, including the front page.
   Once you've loaded the page once, a cookie remembers the token for you.

Why the token, if it's localhost-only? Because "localhost" includes every
program and every user account on your computer, not just you. The token
means none of them can read or rewrite your config behind your back.

The token changes on every start. So old bookmarks stop working — that's
by design. Always use the URL from the current run.

One more promise: the UI makes no external requests. No CDN scripts, no
fonts from Google, no analytics, nothing. Everything it needs ships inside
the Voicisst package.

## First-time setup

The first time you open the UI (before a config file exists), it takes you
to the setup wizard. A progress list on the side shows where you are.
**Every step has a skip** — nothing traps you — and you can rerun the
whole wizard later from the Help page.

**1. Welcome.** What Voicisst is and the privacy deal in a couple of
sentences: your voice is processed on machines you control, and nothing
here phones home. That's it — one click to continue.

**2. Microphone.** Pick your microphone from a list of input devices (the
system default is pre-selected). The **Test** button records a short
sample and shows the level it heard, with a plain-language verdict: fine,
too quiet, or looks muted — and what to do about it.

**3. Hotkey.** Shows the current dictation key(s). Click **Press a key…**
and then press the key you want (you get a few seconds); the wizard
captures its name so you never have to look up key codes. You also choose
how the key works: **hold** to talk (press and hold, release to stop) or
**toggle** (tap to start, tap to stop — no holding needed).

**4. Engine.** Where transcription happens. **Local** is the default:
choose a Whisper model (auto is fine — it picks based on your GPU) and
optionally click **Warm up the model** to download and load it now, with
progress, rather than on your first dictation. **Remote** is for the
client/server split: enter your server's address and token, then check the
connection with one button (the check uses saved settings, so for a
brand-new address, finish setup first and check after).

**5. Polish.** The LLM cleanup step that turns rambling into clean text.
Pick a backend (Ollama is the default), and the wizard checks it can
actually reach it and that the model responds, using a sample sentence
full of "um"s. If you don't want polish — it's optional — there's a clear
"skip polish" path; you'll get raw transcripts.

**6. Permissions.** Whatever your operating system needs Voicisst to be
allowed to do. The wizard only shows the instructions for the OS you're
on: on Linux, the `input` group and the `ydotoold` service; on macOS,
the Accessibility, Input Monitoring, and Microphone permissions, step by
step; on Windows, nothing — there's nothing to grant.

**7. Finish.** A summary of what you picked, and a **Save my choices**
button that writes it all to the config file. Then the next step is right
there on the page: run `voicisst run` and try your hotkey.

## Settings

The Settings view has a form for every config section — engine, whisper,
polish, hotkey, audio, output, dictionary, and the rest — with each
field labelled and explained in place. Each section shows only the handful
of settings you're likely to touch; the rest sit behind a closed "More
options" disclosure, so the page stays short on a low-energy day. The
polish model field is a dropdown listing the models already installed on
your backend (Ollama or LM Studio) — you can still type any name. Two
sections get special editors:

- **Replacements**: rows of find → replace pairs (e.g. "vs code" →
  "VS Code"), applied after polish.
- **Dictionary**: a plain text box, one word or name per line. These feed
  both Whisper and the polisher so your names come out spelled right.

Save buttons stay disabled until you change something, and the result of a
save (success or a validation error) is announced where screen readers
pick it up too. Dictation reads the config when it starts, so a running
`voicisst run` picks up your changes the next time you start it.

If you'd rather see the whole file, open the **raw TOML** editor at the
bottom of the page. It edits the same `config.toml` that
[CONFIGURATION.md](CONFIGURATION.md) documents. Both paths validate before
writing: a typo'd key or broken TOML gets you an error message (often with
a "did you mean…?"), and the file on disk stays untouched. Saving
preserves the comments and layout of your existing file.

## The dashboard

The dashboard's job is to answer one question at a glance: *what is
dictation doing right now?* A large indicator shows the state with a
color, a distinct visual, and the state's name in text — never color
alone, so it works regardless of how you see color.

| State | Color | What you see | What it means |
|---|---|---|---|
| Idle | gray | hollow circle (○) | Ready and waiting. Press your hotkey to dictate. |
| Listening | red | filled circle (●) with a gentle pulse | Recording. Speak now. |
| Transcribing | amber | dotted ring with a ⋯ glyph | Whisper is turning your speech into text. |
| Polishing | violet | a ✦ spark | The LLM is cleaning up the transcript. |
| Delivering | green | check mark (✓) | The text is being typed/pasted into your app. |
| Error | high-contrast | a bold ! plus a message | Something went wrong; the message says what and, where possible, how to fix it. |

Two more you may see: **Stopped** (■) when dictation has shut down, and
**Not running** (a dashed, hollow circle) when no dictation process is
connected at all.

The state name and details also land in a screen-reader live region, and
the pulse animation is dropped entirely if your system asks for reduced
motion.

Below the indicator, an engine health card shows which Whisper model and
device are in use and which polish backend is configured. When dictation
is running it fills in by itself; standalone, it waits for you to press
**Check engine** (the check may load the model, which takes a moment).

If you started with plain `voicisst ui`, the dashboard tells you dictation
isn't running and shows the command to start it (`voicisst run --ui`). The
same notice appears if the connection to Voicisst drops; it reconnects by
itself.

There's no "test dictation" button — the real thing is the test. Press
your hotkey, say something, and watch the states change.

If you also use the tray icon (`voicisst run --ui --tray`), it mirrors the
same states with a distinct shape per state — hollow gray ring (idle),
filled red circle (listening), half-filled amber circle (transcribing),
violet diamond (polishing), green check (delivering), white X on a black
disc (error) — so the tray stays readable in monochrome themes and without
color vision. The tray menu also gets an "Open settings UI" item.

## Troubleshooting

**"Address already in use" / the UI won't start.** Another program (or an
older Voicisst still running) has port 8766. Either stop it, or move the UI:
set `web_port` under `[ui]` in the config, or run `voicisst ui --port
9000`.

**The browser didn't open.** Some setups (SSH sessions, unusual default
browsers) can't auto-open. The URL is printed in the terminal — copy it
into your browser by hand. It looks like `http://127.0.0.1:8766/?t=...`,
and the `?t=` part matters.

**403 Forbidden.** You opened a URL from a previous run — the token in it
has expired, because each start generates a new one. Go back to the
terminal and use the URL that the *current* run printed. (Bookmarking the
UI doesn't work across restarts for the same reason.)

**The dashboard says dictation isn't running.** That's normal under plain
`voicisst ui`. Start `voicisst run --ui` to get live state.

**`voicisst ui` complains about a missing package.** The UI's dependencies
live in the `ui` extra: `pip install "voicisst[ui]"` (or reinstall with
`pipx install "voicisst[local,ui]"`).

For everything not UI-specific — hotkeys that don't fire, silent
microphones, polish problems — see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
