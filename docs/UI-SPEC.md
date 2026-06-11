# Voicisst UI — architecture contract (v0.2.0)

The UI is a **local web app**: onboarding wizard, settings editor, and a
live dictation dashboard. It is served by Voicisst itself on
`127.0.0.1:<ui.web_port>` (default 8766) and opened in the user's normal
browser. No Node, no build step: hand-written, accessible HTML/CSS/JS
shipped as package data in `src/voicisst/ui/static/`.

Two entry modes:
- `voicisst ui` — UI server standalone (settings/onboarding; engine
  features lazy-load on demand).
- `voicisst run --ui` — dictation + UI together; the dashboard shows live
  state from the shared `events.StateBus`.

## Security model

- Bind 127.0.0.1 ONLY (hard-coded; the port comes from `cfg.ui.web_port`).
- A per-run secret: `token = secrets.token_urlsafe(16)` generated at
  startup; the launch URL is `http://127.0.0.1:8766/?t=<token>`.
- Every request (including `/` and static files) requires the token via
  (a) `?t=` query, (b) `voicisst_ui` cookie, or (c) `X-Voicisst-Token`
  header. First authorized page load sets the cookie (HttpOnly,
  SameSite=Strict) so subsequent API/WS calls just work. Compare with
  `hmac.compare_digest`. Unauthorized → 403 JSON for /api, minimal 403
  page for /.
- PUT config writes go through the same 0600-perms path used elsewhere.

## events.py (provided — do not modify)

`StateBus` with `subscribe(fn)->id` (fires immediately with the latest
event), `unsubscribe(id)`, `publish(state, detail="")->StateEvent`,
`.last`. States: `idle listening transcribing polishing delivering error
stopped` (constants in the module). `StateEvent.as_dict()` →
`{"state","detail","ts"}`.

## Server API (src/voicisst/ui/server.py)

```python
def create_ui_app(cfg: Config, *, bus: StateBus | None = None,
                  engine: Engine | None = None, token: str = "",
                  config_file: Path | None = None) -> "FastAPI"
def serve_ui(cfg: Config, *, bus=None, engine=None,
             open_browser: bool | None = None, port: int | None = None) -> None
    # generates the token, prints the URL loudly to stderr, optionally
    # webbrowser.open()s it, uvicorn.run (log_level="warning")
```

All endpoints JSON unless noted. `engine` may be None → engine-dependent
endpoints lazily build a LocalEngine/RemoteEngine via `get_engine(cfg)` on
first use (errors become `{"error", "hint"}` with status 503, never a
crash). Blocking work runs via `run_in_threadpool`.

- `GET /` → static index.html. `GET /static/*` → assets.
- `GET /api/meta` → `{"version", "platform": sys.platform,
  "onboarded": <config file exists>, "config_path", "dictation_running":
  <bus is not None>}`
- `GET /api/config` → `{"toml": <file text or default template>,
  "values": {<section>: {<key>: value}}, "path"}` (values from the
  EFFECTIVE loaded config; replacements as a plain dict).
- `PUT /api/config` body `{"toml": str}` → validate (tomlkit parse +
  `load_config` on a temp file must not error) then atomic write
  (0600). → `{"ok": true}` / 400 `{"error"}`.
- `PUT /api/config/values` body `{"values": {"audio.min_record_ms": 300,
  "replacements": {...}, ...}}` → loads the existing file with tomlkit
  (or starts from `default_config_toml()` when absent), sets the dotted
  keys preserving comments/layout, validates, writes 0600. Unknown keys →
  400 with the did-you-mean text from config's validation where feasible.
- `GET /api/state` → bus.last as_dict (idle if no bus).
- `WS /ws/state` → on connect, push the latest event; then push every
  event as JSON. Token required (cookie or `?t=`). Server → client only.
- `GET /api/audio/devices` → `{"devices": [{"index", "name",
  "default": bool}]}` via sounddevice.query_devices (input-capable only);
  failures → `{"devices": [], "error", "hint"}`.
- `POST /api/audio/test` body `{"seconds": 1.0 (cap 5.0)}` → records via
  `audio.Recorder` → `{"ok": bool, "rms", "peak", "samples", "hint"}`
  (hint explains muted/quiet/no-device cases using the config thresholds).
- `POST /api/hotkey/capture` body `{"timeout_s": 5 (cap 15)}` → captures
  the NEXT key press using a temporary listener and returns
  `{"key": "<name>", "backend": "evdev"|"pynput"}` or 408
  `{"error","hint"}`. evdev path: open the discovered keyboards
  read-only, first EV_KEY down wins, return its evdev name
  (`ecodes.KEY[code]` → e.g. "KEY_F9"). pynput path: one-shot listener,
  return the pynput name ("f9", "alt_r", single char). MUST not steal
  the key (no suppression) and MUST always release devices.
- `POST /api/engine/warm` → kicks a background thread doing
  `engine.warm()`; immediate `{"status": "loading"}`. `GET
  /api/engine/warm` → `{"status": "idle"|"loading"|"ready"|"error",
  "detail"}`. `GET /api/engine/health` → engine.health() or 503.
- `POST /api/polish/test` body `{"text"}` (default sample with fillers)
  → `{"result", "changed": result != input}`.
- `POST /api/dictation/test`? NOT in v0.2.0 — the dashboard tells users
  to just use their hotkey and watch the state.

Module must lazy-import fastapi/uvicorn/tomlkit; missing → EngineError
with hint `pip install 'voicisst[ui]'`. Reuse the publish-into-globals
pattern from server/app.py for FastAPI annotation resolution.

## Frontend (src/voicisst/ui/static/: index.html, app.css, app.js)

Single page, four views (hash-routed: #dashboard #setup #settings #help),
nav as a proper `<nav>` with `aria-current`. On load: `GET /api/meta`;
when `onboarded` is false → #setup; else #dashboard.

- **Dashboard**: a large state indicator — circle ≥120px with BOTH color
  and an icon/label per state (never color-only): idle (gray, hollow),
  listening (red, filled + gentle pulse), transcribing (amber, dots),
  polishing (violet, sparkles), delivering (green, check), error (high-
  contrast warning + the detail text). State name + detail rendered in an
  `aria-live="polite"` region. Connection state of the WS shown
  ("dictation not running — start `voicisst run --ui`" when /ws/state has
  no bus or closes). Engine health card (model, device, polish backend).
- **Setup wizard**: steps with a visible progress list:
  1 Welcome (what Voicisst is, privacy line) → 2 Microphone (device list,
  Test button → live result, too-quiet/muted hints) → 3 Hotkey (current
  keys shown; "Press a key…" capture button wired to /api/hotkey/capture;
  hold-vs-toggle radio) → 4 Engine (local: model choice
  auto/large-v3-turbo/small + Warm up button with progress poll; remote:
  server URL + token + health check button) → 5 Polish (backend pick,
  Ollama reachability + model presence via /api/polish/test, "skip
  polish" path) → 6 Platform permissions (render ONLY the current
  platform's instructions: linux → input group + ydotoold; darwin →
  Accessibility + Input Monitoring + Microphone walkthrough; win →
  nothing special) → 7 Finish (PUT the collected values via
  /api/config/values, show "run `voicisst run`" next steps).
  Every step skippable; wizard rerunnable from Help.
- **Settings**: forms generated from a static SCHEMA constant in app.js
  (mirrors config.py sections/keys/types/help text — keep in sync), plus
  a "raw TOML" `<details>` editor (textarea + Save via PUT /api/config).
  Save buttons disabled-until-dirty, success/error announced in an
  aria-live region. Includes replacements (key→value row editor) and
  dictionary words (textarea, one per line → dictionary.words list).
- **Help**: links to docs, "rerun setup", troubleshooting quick list,
  version from /api/meta.

Accessibility requirements (hard): semantic landmarks (header/nav/main),
every input labelled, fieldset/legend for groups, full keyboard
operability, visible focus, hit targets ≥44px, `prefers-reduced-motion`
respected (no pulse animation), WCAG AA contrast in both light and dark
(`prefers-color-scheme`), works at 200% zoom, no information by color
alone. Plain readable language — the audience includes exhausted people;
short sentences, no jargon walls.

JS rules: no frameworks, no CDN, no external requests of any kind
(privacy promise), single app.js ES module, fetch with the cookie auth,
WS reconnect with backoff, all state in one small store object. Keep it
boring and readable.

## Integration (dictation.py / tray.py / cli.py)

- `DictationApp(cfg, engine, ..., bus: StateBus | None = None)`: when a
  bus is provided, publish: listening (recording start), transcribing
  (stop accepted), polishing (polish window start — same place the beeps
  fire), delivering (deliver/replace final), idle (after each utterance
  completes or is rejected; rejection publishes error w/ short detail
  first when there is a user-actionable cause like too-quiet), error
  (recorder failures etc.), stopped (teardown). Publishing must never
  block or raise into the worker (StateBus already guards).
- `tray.py`: subscribe to the bus and swap the icon per state — distinct
  SHAPE + color per state (hollow/filled/half/diamond/check/cross), so
  states are distinguishable without color. Menu gains "Open settings
  UI" when the UI server is running (pass the tokened URL in).
- `cli.py`: new command `voicisst ui [--port] [--no-browser]` →
  serve_ui(cfg) standalone. `voicisst run` gains `--ui` flag (and
  honors a `ui.web_port`/auto-open config): builds one StateBus, passes
  it to DictationApp AND serve_ui (UI in a daemon thread — uvicorn in a
  thread with its own loop; dictation stays on the main thread except
  the darwin --tray inversion which still wins main).

## Tests

- `tests/test_ui_server.py`: TestClient + FakeEngine + a real tmp config
  file — token auth (403 without, cookie set with, compare_digest), meta,
  config GET/PUT round-trip preserving a comment, values-PUT with dotted
  keys + validation error, state WS (publish on bus → frame arrives),
  audio devices/test with fake sounddevice, hotkey capture with fake
  backends (mock the capture helper), engine warm state machine, polish
  test, 0600 perms on written config, no-fastapi ImportError hint.
- `tests/test_ui_static.py`: serve index.html + assets through the app
  (auth honored), assert key landmarks/ids exist (nav, #dashboard,
  aria-live region, setup wizard root), every `<input>` in index.html has
  a label (parse with html.parser), app.js contains no `http://` or
  `https://` literals (no external requests), CSS contains
  prefers-reduced-motion and prefers-color-scheme blocks.
- Existing test suites must keep passing; bus additions covered in
  test_dictation.py (publish order for a successful utterance + a
  rejection) and tray tests for icon-state mapping.
