/* Voicisst web UI — one small module, no frameworks, no external requests.
   Talks only to the Voicisst server on this computer (cookie auth from the
   tokened launch link). Views are hash-routed: #dashboard #setup #settings
   #help. All shared state lives in the `store` object below. */

/* The settings schema mirrors src/voicisst/config.py exactly — section by
   section, key by key. tests/test_ui_static.py parses this block as JSON and
   compares it against the config dataclasses, so keep it pure JSON. */
const SCHEMA = {
  "engine": {
    "label": "Engine",
    "intro": "Where your speech gets turned into text.",
    "fields": [
      {"key": "mode", "label": "Mode", "type": "enum", "options": ["local", "remote"], "default": "local", "help": "'local' runs everything on this computer; 'remote' uses a voicisst server."},
      {"key": "server_url", "label": "Server address", "type": "str", "default": "", "help": "Web address of your voicisst server (remote mode), like big-box:8765.", "when": {"engine.mode": "remote"}},
      {"key": "token", "label": "Server token", "type": "str", "default": "", "help": "Must match the token the server was started with.", "when": {"engine.mode": "remote"}},
      {"key": "request_timeout", "label": "Request timeout (seconds)", "type": "float", "default": 120.0, "help": "How long to wait for the server before giving up.", "when": {"engine.mode": "remote"}, "advanced": true}
    ]
  },
  "whisper": {
    "label": "Whisper (speech to text)",
    "intro": "The model that hears you.",
    "fields": [
      {"key": "model", "label": "Model", "type": "str", "default": "auto", "help": "'auto' picks large-v3-turbo on a GPU and small on a CPU; any faster-whisper model name works.", "when": {"engine.mode": "local"}},
      {"key": "device", "label": "Device", "type": "enum", "options": ["auto", "cuda", "cpu"], "default": "auto", "help": "Where Whisper runs: 'auto', 'cuda' (GPU), or 'cpu'.", "when": {"engine.mode": "local"}, "advanced": true},
      {"key": "compute", "label": "Compute type", "type": "str", "default": "", "help": "faster-whisper compute type override. Usually leave empty.", "when": {"engine.mode": "local"}, "advanced": true},
      {"key": "language", "label": "Language", "type": "str", "default": "auto", "help": "'auto' detects any of 100+ languages, or set a code like 'en' or 'es'."},
      {"key": "beam_size", "label": "Beam size", "type": "int", "default": 5, "help": "How many guesses Whisper weighs; higher is slower and slightly more accurate.", "when": {"engine.mode": "local"}, "advanced": true},
      {"key": "vad_filter", "label": "Filter silence (VAD)", "type": "bool", "default": false, "help": "Skip long silent stretches before transcribing.", "when": {"engine.mode": "local"}, "advanced": true}
    ]
  },
  "polish": {
    "label": "Polish (LLM cleanup)",
    "intro": "Tidies your words: fillers out, self-corrections applied, punctuation added.",
    "when": {"engine.mode": "local"},
    "fields": [
      {"key": "enabled", "label": "Polish my words", "type": "bool", "default": true, "help": "Turn the LLM cleanup on or off. Off keeps the raw transcript."},
      {"key": "backend", "label": "Backend", "type": "enum", "options": ["ollama", "lmstudio", "openai", "none"], "default": "ollama", "help": "'ollama', 'lmstudio' (LM Studio's local server), 'openai' (any OpenAI-compatible server), or 'none'."},
      {"key": "model", "label": "Model", "type": "str", "widget": "models", "default": "qwen3.5:4b", "help": "Which LLM cleans up your text. The list shows what is installed on your backend; you can also type a name."},
      {"key": "url", "label": "Address", "type": "str", "default": "http://localhost:11434", "help": "Where the polish backend lives. Ollama uses port 11434, LM Studio 1234."},
      {"key": "api_key", "label": "API key", "type": "str", "default": "", "help": "Only for OpenAI-compatible servers that need one.", "when": {"polish.backend": "openai"}},
      {"key": "keep_alive", "label": "Keep model loaded for", "type": "str", "default": "30m", "help": "How long the polish model stays in memory between uses.", "advanced": true},
      {"key": "num_ctx", "label": "Context window", "type": "int", "default": 8192, "help": "How much text the polish model can consider at once.", "advanced": true},
      {"key": "num_predict", "label": "Maximum reply length", "type": "int", "default": 2048, "help": "The longest answer the polish model may write.", "advanced": true},
      {"key": "think", "label": "Let the model think", "type": "bool", "default": false, "help": "Thinking improves long texts but adds seconds of waiting per dictation.", "advanced": true},
      {"key": "think_min_chars", "label": "Think only above (characters)", "type": "int", "default": 100, "help": "Even with thinking on, skip it for dictations shorter than this.", "advanced": true},
      {"key": "num_gpu", "label": "GPU layers", "type": "int", "default": -1, "help": "-1 lets the backend decide how much of the model runs on the GPU.", "advanced": true},
      {"key": "timeout", "label": "Timeout (seconds)", "type": "float", "default": 60.0, "help": "If polish takes longer than this, you get the raw transcript instead.", "advanced": true},
      {"key": "vram_unload_below_mb", "label": "Unload below free VRAM (MB)", "type": "int", "default": 0, "help": "Free the polish model when video memory runs low. 0 = never.", "advanced": true}
    ]
  },
  "hotkey": {
    "label": "Hotkey",
    "intro": "The key that starts and stops dictation.",
    "fields": [
      {"key": "keys", "label": "Keys", "type": "list", "default": [], "help": "Key names that trigger dictation. The Setup page can capture one for you."},
      {"key": "mode", "label": "Style", "type": "enum", "options": ["hold", "toggle"], "default": "hold", "help": "'hold' = keep the key down while speaking; 'toggle' = tap to start, tap to stop."},
      {"key": "backend", "label": "Backend", "type": "enum", "options": ["auto", "evdev", "pynput"], "default": "auto", "help": "How key presses are watched. Leave on 'auto'.", "option_when": {"evdev": {"platform": "linux"}}, "advanced": true}
    ]
  },
  "audio": {
    "label": "Audio",
    "intro": "Recording and loudness.",
    "fields": [
      {"key": "sample_rate", "label": "Sample rate (Hz)", "type": "int", "default": 16000, "help": "16000 is what Whisper expects. Rarely needs changing.", "advanced": true},
      {"key": "input_device", "label": "Microphone", "type": "str", "default": "", "help": "Microphone name or number. Empty = system default."},
      {"key": "min_record_ms", "label": "Shortest recording (ms)", "type": "int", "default": 300, "help": "Recordings shorter than this are ignored as accidental taps.", "advanced": true},
      {"key": "max_record_ms", "label": "Longest recording (ms)", "type": "int", "default": 120000, "help": "Recording stops on its own after this long.", "advanced": true},
      {"key": "muted_rms", "label": "Muted threshold", "type": "float", "default": 0.00001, "help": "Below this loudness the microphone is treated as muted.", "advanced": true},
      {"key": "rms_gate", "label": "Quiet threshold", "type": "float", "default": 0.005, "help": "Below this loudness a recording is dropped as accidental.", "advanced": true},
      {"key": "auto_stop_silence_s", "label": "Auto-stop after silence (seconds)", "type": "float", "default": 0.0, "help": "Stop on its own after this much quiet — great hands-free. 0 = off."},
      {"key": "normalize", "label": "Boost quiet speech", "type": "bool", "default": true, "help": "Whisper-quiet and tired voices get boosted before transcribing."}
    ]
  },
  "output": {
    "label": "Output",
    "intro": "How the text lands in your apps.",
    "fields": [
      {"key": "mode", "label": "Delivery", "type": "enum", "options": ["paste", "type"], "default": "paste", "help": "'paste' is fast and reliable; 'type' presses each key one by one."},
      {"key": "stream", "label": "Live typing", "type": "bool", "default": false, "help": "Type words while you speak, then replace them with the polished text.", "advanced": true},
      {"key": "stream_tick_ms", "label": "Live refresh (ms)", "type": "int", "default": 600, "help": "How often the live transcript updates while streaming.", "advanced": true},
      {"key": "key_delay_ms", "label": "Delay between keys (ms)", "type": "int", "default": 0, "help": "Slow down per-key typing if an app drops characters.", "advanced": true},
      {"key": "key_hold_ms", "label": "Key hold time (ms)", "type": "int", "default": 0, "help": "How long each key stays pressed when typing.", "advanced": true},
      {"key": "paste_chord", "label": "Paste shortcut", "type": "enum", "options": ["auto", "ctrl-v", "ctrl-shift-v", "cmd-v"], "default": "auto", "help": "Which paste shortcut to press. 'auto' picks the usual one for your system.", "option_when": {"cmd-v": {"platform": "darwin"}}, "advanced": true},
      {"key": "newline_mode", "label": "New lines", "type": "enum", "options": ["shift-enter", "enter"], "default": "shift-enter", "help": "Chat apps treat plain Enter as send; shift-enter avoids that."},
      {"key": "terminal_classes", "label": "Terminal windows", "type": "list", "default": ["kitty", "alacritty", "foot", "wezterm", "konsole", "org.gnome.Terminal", "xterm", "ptyxis", "terminal", "iterm2", "windowsterminal", "cmd.exe", "powershell"], "help": "Window names treated as terminals — text is copied there, not pasted.", "advanced": true}
    ]
  },
  "dictionary": {
    "label": "Dictionary",
    "intro": "Names and jargon Voicisst should spell correctly.",
    "fields": [
      {"key": "path", "label": "Extra word file", "type": "str", "default": "", "help": "A file with one word per line. Empty = the default dictionary.txt.", "advanced": true},
      {"key": "words", "label": "Words", "type": "list", "widget": "textarea", "default": [], "help": "One name or term per line — they guide both transcription and polish."},
      {"key": "use_selection", "label": "Use highlighted text", "type": "bool", "default": true, "help": "Linux: text you have selected guides spelling for that dictation.", "when": {"platform": "linux"}}
    ]
  },
  "replacements": {
    "label": "Replacements",
    "kind": "replacements",
    "help": "Swap words after polish — for example make 'vs code' always come out as 'VS Code'. Matches whole words, any capitalization."
  },
  "server": {
    "label": "Server (voicisst serve)",
    "intro": "Only matters when this computer runs 'voicisst serve' for others.",
    "details_label": "Server hosting (voicisst serve)",
    "fields": [
      {"key": "host", "label": "Listen address", "type": "str", "default": "127.0.0.1", "help": "0.0.0.0 shares the server on your network — set a token if you do."},
      {"key": "port", "label": "Port", "type": "int", "default": 8765, "help": "The port 'voicisst serve' listens on."},
      {"key": "token", "label": "Token", "type": "str", "default": "", "help": "When set, clients must send this token."}
    ]
  },
  "ui": {
    "label": "Sounds, notifications, this page",
    "intro": "Feedback and the web UI itself.",
    "fields": [
      {"key": "beep", "label": "Beeps", "type": "bool", "default": true, "help": "Soft tones when recording starts and stops."},
      {"key": "notify", "label": "Desktop notifications", "type": "bool", "default": true, "help": "Short messages from Voicisst, like 'copied to clipboard'."},
      {"key": "tray", "label": "Tray icon", "type": "bool", "default": false, "help": "Show a small status icon while dictating."},
      {"key": "web_port", "label": "This page's port", "type": "int", "default": 8766, "help": "Where this settings page is served (localhost only).", "advanced": true},
      {"key": "open_browser", "label": "Open this page automatically", "type": "bool", "default": true, "help": "Open the browser when the UI starts.", "advanced": true}
    ]
  },
  "history": {
    "label": "History",
    "intro": "A private, local log — nothing leaves this computer.",
    "fields": [
      {"key": "enabled", "label": "Keep a history", "type": "bool", "default": false, "help": "Save everything you dictate to a local file."},
      {"key": "path", "label": "History file", "type": "str", "default": "", "help": "Where the log is saved. Empty = the default history.jsonl."}
    ]
  }
};

/* ------------------------------------------------------------------ tiny helpers */

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

/* Same-origin fetch only; the auth cookie set on page load rides along. */
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (err) {
    return {
      ok: false,
      status: 0,
      body: { error: "could not reach Voicisst", hint: "is it still running?" },
    };
  }
  let body = {};
  try {
    body = await response.json();
  } catch (err) {
    body = {};
  }
  return { ok: response.ok, status: response.status, body };
}

/* Status lines: glyph + words, never color alone. */
function setStatus(node, text, ok) {
  node.classList.remove("ok", "warn");
  if (ok === true) {
    node.classList.add("ok");
    node.textContent = "✓ " + text;
  } else if (ok === false) {
    node.classList.add("warn");
    node.textContent = "✗ " + text;
  } else {
    node.textContent = text;
  }
}

/* ------------------------------------------------------------------ the store */

const store = {
  meta: null, // /api/meta payload
  view: "",
  state: { state: "idle", detail: "", ts: 0 },
  wsAlive: false,
  wsAttempts: 0,
  config: null, // /api/config payload {toml, values, path}
  dirty: {}, // dotted key -> edited value, for PUT /api/config/values
  healthLoaded: false,
  wizard: { step: 1, values: {}, captured: "", prefilled: false },
};

/* ------------------------------------------------------------------ live state */

const STATES = {
  idle: { label: "Idle", desc: "Waiting for your hotkey.", icon: "○", cls: "is-idle" },
  listening: { label: "Listening", desc: "Recording your voice.", icon: "●", cls: "is-listening" },
  transcribing: { label: "Transcribing", desc: "Turning speech into text.", icon: "⋯", cls: "is-transcribing" },
  polishing: { label: "Polishing", desc: "Tidying up the wording.", icon: "✦", cls: "is-polishing" },
  delivering: { label: "Delivering", desc: "Typing into your app.", icon: "✓", cls: "is-delivering" },
  error: { label: "Problem", desc: "Something went wrong — details below.", icon: "!", cls: "is-error" },
  stopped: { label: "Stopped", desc: "Dictation has stopped.", icon: "■", cls: "is-stopped" },
  off: { label: "Not running", desc: "To dictate, open a terminal and run: voicisst run --ui", icon: "—", cls: "is-off" },
};

function renderState() {
  const running = Boolean(store.meta && store.meta.dictation_running);
  let key = store.state.state;
  if (!running) key = "off";
  const s = STATES[key] || STATES.idle;
  $("#state-circle").className = "state-circle " + s.cls;
  $("#state-icon").textContent = s.icon;
  $("#state-name").textContent = s.label;
  $("#state-detail").textContent =
    key !== "off" && store.state.detail ? store.state.detail : s.desc;

  const note = $("#conn-note");
  if (!running) {
    note.textContent =
      "Dictation is not running, so there is no live state to show. " +
      "Settings still work. To dictate, run voicisst run --ui in a terminal.";
  } else if (!store.wsAlive) {
    note.textContent = "Lost the live connection — reconnecting…";
  } else {
    note.textContent =
      "Connected. Try it: press your hotkey and speak — the circle follows along.";
  }
}

function wsURL() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const t = new URLSearchParams(location.search).get("t");
  const query = t ? "?t=" + encodeURIComponent(t) : "";
  return proto + "//" + location.host + "/ws/state" + query;
}

function connectWS() {
  let sock;
  try {
    sock = new WebSocket(wsURL());
  } catch (err) {
    scheduleReconnect();
    return;
  }
  sock.onopen = () => {
    store.wsAlive = true;
    store.wsAttempts = 0;
    renderState();
  };
  sock.onmessage = (msg) => {
    try {
      store.state = JSON.parse(msg.data);
    } catch (err) {
      return;
    }
    renderState();
  };
  sock.onclose = () => {
    store.wsAlive = false;
    renderState();
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  const delay = Math.min(15000, 1000 * 2 ** store.wsAttempts);
  store.wsAttempts += 1;
  setTimeout(connectWS, delay);
}

/* ------------------------------------------------------------------ engine health */

function renderHealth(body) {
  const dl = $("#health-list");
  dl.replaceChildren();
  let polish = body.polish_backend || "";
  if (!polish || polish === "none") polish = "off";
  else if (body.polish_model) polish += " · " + body.polish_model;
  const rows = [
    ["Mode", body.mode],
    ["Whisper model", body.whisper_model],
    ["Device", body.device],
    ["Polish", polish],
  ];
  for (const [k, v] of rows) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(el("dt", { text: k }), el("dd", { text: String(v) }));
  }
}

async function refreshHealth() {
  const note = $("#health-note");
  note.textContent = "Checking… the first check can take a little while.";
  const r = await api("/api/engine/health");
  store.healthLoaded = true;
  if (r.ok) {
    renderHealth(r.body);
    note.textContent = "";
  } else {
    $("#health-list").replaceChildren();
    note.textContent =
      "The engine is not ready: " +
      (r.body.error || "unknown problem") +
      (r.body.hint ? " — " + r.body.hint : "");
  }
}

function enterDashboard() {
  renderState();
  // Only auto-check when dictation runs (the engine exists then). Standalone,
  // a health check may load a model — leave that to the button.
  if (!store.healthLoaded && store.meta && store.meta.dictation_running) {
    refreshHealth();
  }
}

/* ------------------------------------------------------------------ config cache */

async function ensureConfig() {
  if (store.config) return store.config;
  const r = await api("/api/config");
  if (r.ok) store.config = r.body;
  return store.config;
}

function schemaDefault(section, key) {
  const field = SCHEMA[section].fields.find((f) => f.key === key);
  return field ? field.default : "";
}

/* ------------------------------------------------------------------ setup wizard */

const WIZ_STEPS = 7;

const FRIENDLY = {
  "audio.input_device": "Microphone",
  "hotkey.keys": "Hotkey",
  "hotkey.mode": "Hotkey style",
  "engine.mode": "Transcription runs",
  "engine.server_url": "Server address",
  "engine.token": "Server token",
  "whisper.model": "Whisper model",
  "polish.enabled": "Polish",
  "polish.backend": "Polish backend",
  "polish.model": "Polish model",
  "polish.url": "Polish address",
};

function renderWizard() {
  for (const li of document.querySelectorAll("#wizard-progress li")) {
    const n = Number(li.dataset.step);
    li.classList.toggle("done", n < store.wizard.step);
    if (n === store.wizard.step) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  }
  for (const stepEl of document.querySelectorAll(".wiz-step")) {
    stepEl.hidden = Number(stepEl.dataset.step) !== store.wizard.step;
  }
  $("#wiz-back").disabled = store.wizard.step === 1;
  $("#wiz-skip").hidden = store.wizard.step === WIZ_STEPS;
  $("#wiz-next").hidden = store.wizard.step === WIZ_STEPS;

  if (store.wizard.step === 2) loadMics();
  if (store.wizard.step === 3) showCurrentHotkeys();
  if (store.wizard.step === 5) loadWizardPolishModels();
  if (store.wizard.step === 6) showPlatform();
  if (store.wizard.step === 7) renderSummary();
}

function stepTo(n) {
  store.wizard.step = Math.min(WIZ_STEPS, Math.max(1, n));
  renderWizard();
  $("#setup-title").focus({ preventScroll: false });
}

function collectStep(n) {
  const v = store.wizard.values;
  if (n === 2) {
    const mic = $("#wiz-mic").value;
    if (mic) v["audio.input_device"] = mic;
  }
  if (n === 3) {
    v["hotkey.mode"] = $("#wiz-mode-toggle").checked ? "toggle" : "hold";
    if (store.wizard.captured) v["hotkey.keys"] = [store.wizard.captured];
  }
  if (n === 4) {
    const remote = $("#wiz-engine-remote").checked;
    v["engine.mode"] = remote ? "remote" : "local";
    if (remote) {
      const url = $("#wiz-server-url").value.trim();
      if (url) v["engine.server_url"] = url;
      const tok = $("#wiz-server-token").value.trim();
      if (tok) v["engine.token"] = tok;
    } else {
      v["whisper.model"] = $("#wiz-model").value;
    }
  }
  if (n === 5) {
    if ($("#wiz-polish-none").checked) {
      v["polish.enabled"] = false;
    } else {
      v["polish.enabled"] = true;
      v["polish.backend"] = wizardPolishBackend();
      const model = $("#wiz-polish-model").value.trim();
      if (model) v["polish.model"] = model;
      const url = $("#wiz-polish-url").value.trim();
      if (url) v["polish.url"] = url;
    }
  }
}

async function prefillWizard() {
  if (store.wizard.prefilled) return;
  store.wizard.prefilled = true;
  const data = await ensureConfig();
  const values = data ? data.values : null;
  const polish = values ? values.polish : null;
  $("#wiz-polish-model").value = polish ? polish.model : schemaDefault("polish", "model");
  $("#wiz-polish-url").value = polish ? polish.url : schemaDefault("polish", "url");
  if (!values) return;
  $("#wiz-server-url").value = values.engine.server_url || "";
  $("#wiz-server-token").value = values.engine.token || "";
  if (values.engine.mode === "remote") {
    $("#wiz-engine-remote").checked = true;
    $("#wiz-local-opts").hidden = true;
    $("#wiz-remote-opts").hidden = false;
  }
  if (values.hotkey.mode === "toggle") $("#wiz-mode-toggle").checked = true;
  const modelChoice = $("#wiz-model");
  if ([...modelChoice.options].some((o) => o.value === values.whisper.model)) {
    modelChoice.value = values.whisper.model;
  }
  if (!values.polish.enabled || values.polish.backend === "none") {
    $("#wiz-polish-none").checked = true;
    $("#wiz-polish-opts").hidden = true;
  } else if (values.polish.backend === "openai") {
    $("#wiz-polish-openai").checked = true;
  } else if (values.polish.backend === "lmstudio") {
    $("#wiz-polish-lmstudio").checked = true;
  }
}

function wizardPolishBackend() {
  if ($("#wiz-polish-openai").checked) return "openai";
  if ($("#wiz-polish-lmstudio").checked) return "lmstudio";
  return "ollama";
}

function loadWizardPolishModels() {
  if ($("#wiz-polish-none").checked) return;
  const url = $("#wiz-polish-url").value.trim() || schemaDefault("polish", "url");
  fillModelList($("#wiz-polish-models"), wizardPolishBackend(), url);
}

function enterSetup() {
  prefillWizard();
  renderWizard();
}

async function loadMics() {
  const select = $("#wiz-mic");
  const chosen = select.value;
  const r = await api("/api/audio/devices");
  const devices = (r.body && r.body.devices) || [];
  select.replaceChildren(el("option", { value: "", text: "System default" }));
  for (const d of devices) {
    select.append(
      el("option", { value: d.name, text: d.name + (d.default ? " (default)" : "") })
    );
  }
  if ([...select.options].some((o) => o.value === chosen)) select.value = chosen;
  if (r.body && r.body.error) {
    setStatus(
      $("#wiz-mic-result"),
      "Could not list microphones: " + r.body.error +
        (r.body.hint ? " — " + r.body.hint : ""),
      false
    );
  }
}

async function showCurrentHotkeys() {
  const data = await ensureConfig();
  const span = $("#wiz-hotkey-current");
  if (data && data.values.hotkey.keys.length) {
    span.textContent = data.values.hotkey.keys.join(", ");
  } else {
    span.textContent = "none yet";
  }
}

function showPlatform() {
  const platform = store.meta ? store.meta.platform : "";
  $("#wiz-perm-linux").hidden = platform !== "linux";
  $("#wiz-perm-darwin").hidden = platform !== "darwin";
  $("#wiz-perm-win").hidden = platform === "linux" || platform === "darwin";
}

function renderSummary() {
  const list = $("#wiz-summary");
  list.replaceChildren();
  const entries = Object.entries(store.wizard.values);
  if (!entries.length) {
    list.append(
      el("li", { text: "Nothing picked — Voicisst's defaults will be used. That works fine." })
    );
    return;
  }
  for (const [key, value] of entries) {
    let shown;
    if (key === "engine.token") shown = "(hidden)";
    else if (Array.isArray(value)) shown = value.join(", ");
    else if (typeof value === "boolean") shown = value ? "on" : "off";
    else shown = String(value);
    list.append(el("li", { text: (FRIENDLY[key] || key) + ": " + shown }));
  }
}

function wireWizard() {
  $("#wiz-next").addEventListener("click", () => {
    collectStep(store.wizard.step);
    stepTo(store.wizard.step + 1);
  });
  $("#wiz-skip").addEventListener("click", () => stepTo(store.wizard.step + 1));
  $("#wiz-back").addEventListener("click", () => stepTo(store.wizard.step - 1));

  $("#wiz-mic-refresh").addEventListener("click", loadMics);

  $("#wiz-mic-test").addEventListener("click", async () => {
    const note = $("#wiz-mic-result");
    setStatus(note, "Recording for one second — say something…");
    const r = await api("/api/audio/test", {
      method: "POST",
      body: JSON.stringify({ seconds: 1.0 }),
    });
    const b = r.body || {};
    if (b.ok) {
      setStatus(
        note,
        "Heard you — the microphone works." + (b.hint ? " " + b.hint : ""),
        true
      );
    } else {
      setStatus(note, b.hint || b.error || "The test did not work.", false);
    }
  });

  $("#wiz-hotkey-capture").addEventListener("click", async () => {
    const button = $("#wiz-hotkey-capture");
    const note = $("#wiz-hotkey-result");
    button.disabled = true;
    setStatus(note, "Now press the key you want to use (you have 6 seconds)…");
    const r = await api("/api/hotkey/capture", {
      method: "POST",
      body: JSON.stringify({ timeout_s: 6 }),
    });
    button.disabled = false;
    if (r.ok && r.body.key) {
      store.wizard.captured = r.body.key;
      setStatus(note, "Got it: " + r.body.key, true);
    } else {
      setStatus(
        note,
        (r.body.error || "No key arrived.") + (r.body.hint ? " " + r.body.hint : ""),
        false
      );
    }
  });

  for (const id of ["wiz-engine-local", "wiz-engine-remote"]) {
    document.getElementById(id).addEventListener("change", () => {
      const remote = $("#wiz-engine-remote").checked;
      $("#wiz-local-opts").hidden = remote;
      $("#wiz-remote-opts").hidden = !remote;
    });
  }

  $("#wiz-warm").addEventListener("click", async () => {
    const note = $("#wiz-warm-result");
    setStatus(note, "Starting… downloads can take a few minutes the first time.");
    const r = await api("/api/engine/warm", { method: "POST" });
    if (!r.ok) {
      setStatus(
        note,
        (r.body.error || "Could not start.") + (r.body.hint ? " " + r.body.hint : ""),
        false
      );
      return;
    }
    pollWarm(note);
  });

  $("#wiz-health-check").addEventListener("click", async () => {
    const note = $("#wiz-health-result");
    setStatus(note, "Checking…");
    const r = await api("/api/engine/health");
    if (r.ok) {
      setStatus(
        note,
        "Server reachable" + (r.body.whisper_model ? " — model " + r.body.whisper_model : "") + ".",
        true
      );
    } else {
      setStatus(
        note,
        (r.body.error || "Not reachable.") + (r.body.hint ? " " + r.body.hint : ""),
        false
      );
    }
  });

  const polishRadios = [
    "wiz-polish-ollama",
    "wiz-polish-lmstudio",
    "wiz-polish-openai",
    "wiz-polish-none",
  ];
  for (const id of polishRadios) {
    document.getElementById(id).addEventListener("change", () => {
      $("#wiz-polish-opts").hidden = $("#wiz-polish-none").checked;
      const urlInput = $("#wiz-polish-url");
      const suggested = BACKEND_URLS[wizardPolishBackend()];
      if (suggested && Object.values(BACKEND_URLS).includes(urlInput.value)) {
        urlInput.value = suggested;
      }
      loadWizardPolishModels();
    });
  }
  $("#wiz-polish-url").addEventListener("change", loadWizardPolishModels);

  $("#wiz-polish-test").addEventListener("click", async () => {
    const note = $("#wiz-polish-result");
    setStatus(note, "Testing — sending a short, messy sample…");
    const r = await api("/api/polish/test", { method: "POST", body: JSON.stringify({}) });
    if (r.ok && r.body.changed) {
      setStatus(note, "Polish works. The sample came back as: “" + r.body.result + "”", true);
    } else if (r.ok) {
      setStatus(note, "Polish answered but did not change the sample: " + r.body.result);
    } else {
      setStatus(
        note,
        (r.body.error || "Polish is not reachable.") + (r.body.hint ? " " + r.body.hint : ""),
        false
      );
    }
  });

  $("#wiz-finish").addEventListener("click", async () => {
    const note = $("#wiz-finish-result");
    const values = store.wizard.values;
    if (!Object.keys(values).length) {
      setStatus(
        note,
        "Nothing to save — the defaults are already in place. You can change anything later in Settings.",
        true
      );
      return;
    }
    setStatus(note, "Saving…");
    const r = await api("/api/config/values", {
      method: "PUT",
      body: JSON.stringify({ values }),
    });
    if (r.ok) {
      store.config = null; // reload on next visit to Settings
      if (store.meta) store.meta.onboarded = true;
      setStatus(note, "Saved. You are ready to dictate.", true);
    } else {
      setStatus(note, "Could not save: " + (r.body.error || "unknown problem"), false);
    }
  });
}

let warmTimer = 0;
function pollWarm(note) {
  clearTimeout(warmTimer);
  warmTimer = setTimeout(async () => {
    const r = await api("/api/engine/warm");
    const status = (r.body && r.body.status) || "error";
    if (status === "loading") {
      setStatus(note, "Still loading… this is normal for a first download.");
      pollWarm(note);
    } else if (status === "ready") {
      setStatus(note, "The model is ready.", true);
    } else {
      setStatus(note, "Loading failed: " + (r.body.detail || "unknown problem"), false);
    }
  }, 1500);
}

/* ------------------------------------------------------------------ settings */

/* Conditional visibility. SCHEMA marks fields/sections with a `when` object
   ({"engine.mode": "remote"}, {"platform": "linux"}, …). One small evaluator
   hides what does not apply; the "Show all settings" toggle reveals everything
   with hidden-by-default items marked. Hidden controls keep their values. */

const SHOW_ALL_KEY = "voicisst-show-all";

/* Form fields that drive someone else's visibility (e.g. engine.mode):
   changing them re-evaluates visibility live, no save needed. */
const WHEN_DRIVERS = (() => {
  const keys = new Set();
  const collect = (when) => {
    for (const key of Object.keys(when || {})) {
      if (key !== "platform") keys.add(key);
    }
  };
  for (const spec of Object.values(SCHEMA)) {
    collect(spec.when);
    for (const field of spec.fields || []) {
      collect(field.when);
      for (const when of Object.values(field.option_when || {})) collect(when);
    }
  }
  return keys;
})();

let visRules = []; // rebuilt with the form: {el, when, badge?, invert?, keepSelect?}

function showAllSettings() {
  try {
    return localStorage.getItem(SHOW_ALL_KEY) === "1";
  } catch (err) {
    return false;
  }
}

function whenValue(key) {
  if (key === "platform") return store.meta ? store.meta.platform : "";
  // Live form value first, then the loaded config, then the schema default.
  const control = document.getElementById("f-" + key.split(".").join("-"));
  if (control) return control.value;
  const [section, field] = key.split(".");
  const values = store.config ? store.config.values : null;
  if (values && values[section] && field in values[section]) {
    return String(values[section][field]);
  }
  return String(schemaDefault(section, field));
}

function whenMatches(when) {
  if (!when) return true;
  return Object.entries(when).every(([key, expected]) => whenValue(key) === expected);
}

function applyVisibility() {
  const showAll = showAllSettings();
  for (const rule of visRules) {
    const matched = whenMatches(rule.when);
    if (rule.keepSelect) {
      // An <option>: hide it unless it applies, "Show all" is on, or it is
      // the value already stored in the config (never strand a saved value).
      const hide = !matched && !showAll && rule.keepSelect.value !== rule.el.value;
      rule.el.hidden = hide;
      rule.el.disabled = hide;
      continue;
    }
    // `invert` marks stand-in notes shown when their section is hidden.
    rule.el.hidden = rule.invert ? matched || showAll : !matched && !showAll;
    if (rule.badge) rule.badge.hidden = matched || !showAll;
  }
}

function notUsedBadge() {
  const badge = el("span", { class: "not-used", text: "(not used in your current setup)" });
  badge.hidden = true;
  return badge;
}

function markDirty(dotted, value, original, deep = false) {
  const same = deep
    ? JSON.stringify(value) === JSON.stringify(original)
    : value === original;
  if (same) delete store.dirty[dotted];
  else store.dirty[dotted] = value;
  updateSaveButton();
}

function updateSaveButton() {
  const n = Object.keys(store.dirty).length;
  const button = $("#settings-save");
  button.disabled = n === 0;
  button.textContent = n ? "Save changes (" + n + ")" : "Save changes";
}

function buildFieldRow(section, field, values) {
  const dotted = section + "." + field.key;
  const id = "f-" + section + "-" + field.key;
  const helpId = id + "-help";
  const sectionValues = values[section] || {};
  const current = field.key in sectionValues ? sectionValues[field.key] : field.default;
  const help = el("p", { class: "help", id: helpId, text: field.help });

  if (field.type === "bool") {
    const control = el("input", { type: "checkbox", id, "aria-describedby": helpId });
    control.checked = Boolean(current);
    control.addEventListener("change", () =>
      markDirty(dotted, control.checked, Boolean(current))
    );
    return el(
      "div",
      { class: "row-check" },
      control,
      el("label", { for: id, text: field.label }),
      help
    );
  }

  const row = el("div", { class: "row" });
  row.append(el("label", { for: id, text: field.label }));
  let control;
  if (field.type === "enum") {
    control = el("select", { id, "aria-describedby": helpId });
    for (const option of field.options) {
      const opt = el("option", { value: option, text: option });
      control.append(opt);
      const optionWhen = field.option_when && field.option_when[option];
      if (optionWhen) visRules.push({ el: opt, when: optionWhen, keepSelect: control });
    }
    control.value = String(current);
    control.addEventListener("change", () =>
      markDirty(dotted, control.value, String(current))
    );
  } else if (field.widget === "textarea") {
    control = el("textarea", { id, rows: "6", "aria-describedby": helpId });
    const original = current || [];
    control.value = original.join("\n");
    control.addEventListener("input", () => {
      const lines = control.value.split("\n").map((s) => s.trim()).filter(Boolean);
      markDirty(dotted, lines, original, true);
    });
  } else if (field.type === "list") {
    control = el("input", { type: "text", id, "aria-describedby": helpId });
    const original = current || [];
    control.value = original.join(", ");
    control.addEventListener("input", () => {
      const items = control.value.split(",").map((s) => s.trim()).filter(Boolean);
      markDirty(dotted, items, original, true);
    });
  } else if (field.type === "int" || field.type === "float") {
    control = el("input", { type: "number", id, "aria-describedby": helpId });
    if (field.type === "float") control.setAttribute("step", "any");
    control.value = String(current);
    control.addEventListener("input", () => {
      const num =
        field.type === "int" ? parseInt(control.value, 10) : parseFloat(control.value);
      if (Number.isNaN(num)) return; // leave the last good value pending
      markDirty(dotted, num, current);
    });
  } else {
    control = el("input", { type: "text", id, "aria-describedby": helpId });
    if (field.widget === "models") {
      // The dropdown is a datalist: installed models to pick from, free
      // text still allowed for models not pulled yet.
      control.setAttribute("list", id + "-list");
      row.append(el("datalist", { id: id + "-list" }));
    }
    control.value = current === null || current === undefined ? "" : String(current);
    control.addEventListener("input", () =>
      markDirty(dotted, control.value, String(current))
    );
  }
  if (WHEN_DRIVERS.has(dotted)) control.addEventListener("change", applyVisibility);
  row.append(control, help);
  return row;
}

/* ----------------------------------------------------------- model dropdown */

/* Fill a <datalist> with the models installed on the polish backend. */
let modelsFetchSeq = 0;
async function fillModelList(list, backend, url) {
  if (!list || backend === "none") return;
  const seq = ++modelsFetchSeq;
  const r = await api(
    "/api/polish/models?backend=" + encodeURIComponent(backend) +
      "&url=" + encodeURIComponent(url)
  );
  if (seq !== modelsFetchSeq) return; // a newer request superseded this one
  list.replaceChildren();
  for (const name of (r.body && r.body.models) || []) {
    list.append(el("option", { value: name }));
  }
}

function refreshPolishModels() {
  const urlInput = document.getElementById("f-polish-url");
  fillModelList(
    document.getElementById("f-polish-model-list"),
    whenValue("polish.backend"),
    urlInput ? urlInput.value : ""
  );
}

/* Each backend's usual address. Switching backends swaps the address field
   between these defaults — a hand-edited address is left alone. */
const BACKEND_URLS = { ollama: "http://localhost:11434", lmstudio: "http://localhost:1234" };

function wirePolishBackend() {
  const backendSel = document.getElementById("f-polish-backend");
  const urlInput = document.getElementById("f-polish-url");
  if (backendSel) {
    backendSel.addEventListener("change", () => {
      const suggested = BACKEND_URLS[backendSel.value];
      const isDefault = Object.values(BACKEND_URLS).includes(urlInput && urlInput.value);
      if (urlInput && suggested && isDefault && urlInput.value !== suggested) {
        urlInput.value = suggested;
        urlInput.dispatchEvent(new Event("input")); // register the edit
      }
      refreshPolishModels();
    });
  }
  if (urlInput) urlInput.addEventListener("change", refreshPolishModels);
}

function buildReplacements(map) {
  const original = { ...map };
  const wrap = el("div", { id: "replacements-editor" });
  const rows = el("div", {
    id: "replacement-rows",
    role: "group",
    "aria-label": "Replacement pairs",
  });

  function currentMap() {
    const out = {};
    for (const row of rows.querySelectorAll(".rep-row")) {
      const spoken = row.querySelector(".rep-from").value.trim();
      const written = row.querySelector(".rep-to").value;
      if (spoken) out[spoken] = written;
    }
    return out;
  }

  function changed() {
    markDirty("replacements", currentMap(), original, true);
  }

  function addRow(spoken = "", written = "") {
    const from = el("input", { type: "text", class: "rep-from", "aria-label": "Spoken words" });
    from.value = spoken;
    const to = el("input", { type: "text", class: "rep-to", "aria-label": "Written replacement" });
    to.value = written;
    const remove = el("button", {
      type: "button",
      class: "ghost",
      "aria-label": "Remove this replacement",
      text: "Remove",
    });
    const row = el(
      "div",
      { class: "rep-row" },
      from,
      el("span", { class: "rep-arrow", "aria-hidden": "true", text: "→" }),
      to,
      remove
    );
    remove.addEventListener("click", () => {
      row.remove();
      changed();
    });
    from.addEventListener("input", changed);
    to.addEventListener("input", changed);
    rows.append(row);
    return row;
  }

  for (const [spoken, written] of Object.entries(map)) addRow(spoken, written);
  const add = el("button", { type: "button", class: "ghost", text: "Add a replacement" });
  add.addEventListener("click", () => {
    const row = addRow();
    row.querySelector(".rep-from").focus();
  });
  wrap.append(rows, add);
  return wrap;
}

function buildSettingsForm(values) {
  store.dirty = {};
  updateSaveButton();
  visRules = [];
  const root = $("#settings-sections");
  root.replaceChildren();
  for (const [section, spec] of Object.entries(SCHEMA)) {
    const fs = el("fieldset");
    const legend = el("legend", { text: spec.label });
    fs.append(legend);
    if (spec.intro) fs.append(el("p", { class: "help", text: spec.intro }));
    if (spec.kind === "replacements") {
      fs.append(el("p", { class: "help", text: spec.help }));
      fs.append(buildReplacements(values.replacements || {}));
    } else {
      const rowFor = (field) => {
        const row = buildFieldRow(section, field, values);
        if (field.when) {
          const badge = notUsedBadge();
          row.querySelector("label").append(" ", badge);
          visRules.push({ el: row, when: field.when, badge });
        }
        return row;
      };
      for (const field of spec.fields) {
        if (!field.advanced) fs.append(rowFor(field));
      }
      // Rarely-touched knobs live behind a closed disclosure so the page
      // stays short. Values inside still load and save normally.
      const advanced = spec.fields.filter((field) => field.advanced);
      if (advanced.length) {
        const advBox = el(
          "details",
          { class: "adv-details" },
          el("summary", { text: "More options" })
        );
        for (const field of advanced) advBox.append(rowFor(field));
        fs.append(advBox);
      }
    }
    let node = fs;
    if (spec.details_label) {
      // Niche sections live behind a collapsed disclosure.
      node = el(
        "details",
        { class: "section-details" },
        el("summary", { text: spec.details_label }),
        fs
      );
    }
    if (spec.when) {
      const badge = notUsedBadge();
      legend.append(" ", badge);
      visRules.push({ el: node, when: spec.when, badge });
    }
    root.append(node);
    if (section === "polish") {
      // Stand-in line shown when the whole polish section is hidden (remote).
      const note = el("p", {
        class: "help remote-note",
        id: "polish-remote-note",
        text: "Transcription and polish run on your server — configure them there.",
      });
      note.hidden = true;
      visRules.push({ el: note, when: spec.when, invert: true });
      root.append(note);
    }
  }
  applyVisibility();
  wirePolishBackend();
  refreshPolishModels();
}

async function enterSettings() {
  const fresh = !store.config;
  const data = await ensureConfig();
  if (!data) {
    setStatus($("#save-status"), "Could not load your settings — is Voicisst still running?", false);
    return;
  }
  $("#settings-path").textContent = data.path;
  if (fresh || !$("#settings-sections").childElementCount) {
    buildSettingsForm(data.values);
    $("#raw-toml").value = data.toml;
    $("#raw-save").disabled = true;
  }
}

function wireSettings() {
  const showAll = $("#show-all");
  showAll.checked = showAllSettings();
  showAll.addEventListener("change", () => {
    try {
      localStorage.setItem(SHOW_ALL_KEY, showAll.checked ? "1" : "0");
    } catch (err) {
      /* private browsing: the toggle still works for this page load */
    }
    applyVisibility();
    setStatus(
      $("#save-status"),
      showAll.checked
        ? "Showing every setting, including ones not used in your current setup."
        : "Hiding settings that do not apply to your current setup."
    );
  });

  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!Object.keys(store.dirty).length) return;
    const note = $("#save-status");
    setStatus(note, "Saving…");
    const r = await api("/api/config/values", {
      method: "PUT",
      body: JSON.stringify({ values: store.dirty }),
    });
    if (r.ok) {
      store.config = null;
      await enterSettings(); // reload + rebuild with the saved values
      setStatus(note, "Saved. Dictation picks this up the next time it starts.", true);
    } else {
      setStatus(note, "Not saved: " + (r.body.error || "unknown problem"), false);
    }
  });

  $("#raw-toml").addEventListener("input", () => {
    $("#raw-save").disabled = false;
  });

  $("#raw-save").addEventListener("click", async () => {
    const note = $("#raw-status");
    setStatus(note, "Saving…");
    const r = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ toml: $("#raw-toml").value }),
    });
    if (r.ok) {
      store.config = null;
      await enterSettings();
      setStatus(note, "Saved.", true);
    } else {
      setStatus(note, "Not saved: " + (r.body.error || "unknown problem"), false);
    }
  });
}

/* ------------------------------------------------------------------ help */

function enterHelp() {
  if (!store.meta) return;
  $("#help-version").textContent = store.meta.version || "unknown";
  $("#help-config-path").textContent = store.meta.config_path || "";
}

function wireHelp() {
  $("#help-rerun").addEventListener("click", () => {
    store.wizard = { step: 1, values: {}, captured: "", prefilled: false };
  });
}

/* ------------------------------------------------------------------ router */

const VIEWS = ["dashboard", "setup", "settings", "help"];

function currentView() {
  const hash = location.hash.replace("#", "");
  return VIEWS.includes(hash) ? hash : "";
}

function showView(view) {
  store.view = view;
  for (const name of VIEWS) {
    document.getElementById(name).hidden = name !== view;
    const link = document.querySelector('nav a[href="#' + name + '"]');
    if (!link) continue;
    if (name === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (view === "dashboard") enterDashboard();
  else if (view === "setup") enterSetup();
  else if (view === "settings") enterSettings();
  else if (view === "help") enterHelp();
}

/* ------------------------------------------------------------------ boot */

async function init() {
  wireWizard();
  wireSettings();
  wireHelp();
  $("#health-refresh").addEventListener("click", refreshHealth);
  window.addEventListener("hashchange", () => showView(currentView() || "dashboard"));

  const r = await api("/api/meta");
  store.meta = r.ok
    ? r.body
    : { version: "", platform: "", onboarded: true, config_path: "", dictation_running: false };
  connectWS();

  const view = currentView();
  if (view) showView(view);
  else location.hash = store.meta.onboarded ? "#dashboard" : "#setup"; // triggers showView
}

init();
