# Running a Voicisst server

`voicisst serve` exposes Voicisst's transcription and polish over HTTP + WebSocket,
so one machine with a GPU can do the heavy lifting for every other machine
you own. The client (`voicisst run --server URL`) keeps audio capture, hotkeys,
and typing local — only audio and text cross the wire.

## Setup

On the GPU box:

```bash
pipx install "voicisst[server]"
ollama pull qwen3.5:4b            # if you want polish on the server
voicisst serve                        # binds 127.0.0.1:8765 by default
```

On the laptop:

```bash
pipx install "voicisst[media]"       # media only needed for local M4A file decoding
voicisst run --server http://big-box:8765 --token s3cret
```

or persist it in the client's `config.toml`:

```toml
[engine]
mode = "remote"
server_url = "http://big-box:8765"
token = "s3cret"
```

The server warms its models at startup, so the first dictation isn't slow.

## Security model

- **Default is loopback.** `voicisst serve` binds `127.0.0.1` and is unreachable
  from other machines until you change `server.host`.
- **Token auth.** Set a token on both ends and every REST request must send
  `Authorization: Bearer <token>`; the WebSocket passes it as a `?token=`
  query parameter (a Bearer header works there too). Wrong or missing token
  → HTTP 401 (the WebSocket gets an `error` frame and close code 4401).
  Generate one with
  `python -c "import secrets; print(secrets.token_hex(32))"`.
- **LAN exposure.** `voicisst serve --host 0.0.0.0` opens the API to your
  network. Anyone who can reach the port can run transcription jobs on your
  GPU and read what you dictate, so always pair it with a token — the server
  warns loudly at startup if you bind a non-loopback address without one.
- **No TLS built in.** Traffic is plain HTTP. On a trusted home LAN (or over
  WireGuard/Tailscale) that may be fine. Across anything you trust less, put
  a reverse proxy with TLS in front and keep Voicisst itself on loopback:

  ```
  # Caddyfile — Caddy handles certificates and WebSocket upgrades
  voicisst.example.com {
      reverse_proxy 127.0.0.1:8765
  }
  ```

  (nginx works too; remember the `Upgrade`/`Connection` headers for
  `/v1/stream`.) Then point the client at `https://voicisst.example.com`.

## systemd unit (headless GPU box)

```ini
# ~/.config/systemd/user/voicisst-serve.service
[Unit]
Description=Voicisst dictation server
After=network-online.target

[Service]
ExecStart=%h/.local/bin/voicisst serve --host 0.0.0.0 --port 8765 --token CHANGE-ME
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now voicisst-serve
loginctl enable-linger $USER     # keep it running with no one logged in
journalctl --user -u voicisst-serve -f
```

## Protocol reference

Wire format for audio: **WAV bytes, 16-bit PCM mono**, base64-encoded inside
JSON. Protocol version 1; default port 8765.

### Authentication

When the server has a token: REST requests need
`Authorization: Bearer <token>`, the WebSocket needs `?token=<token>` (or
the same Bearer header). REST without it gets a 401; the WebSocket gets an
`error` frame and close code 4401.

### REST endpoints

All request/response bodies are JSON. `language` is `null` for auto-detect or
an ISO code (`"en"`, `"es"`, ...). `vocab` is a string of preferred
spellings/context words (may be empty).

```
GET  /v1/health
     -> {"status": "ok", "version": ..., ...engine health fields
         (mode, whisper_model, device, polish_backend, polish_model)}

POST /v1/transcribe
     {"audio_b64": "...", "language": null, "vocab": ""}
     -> {"text": "raw transcript"}

POST /v1/polish
     {"text": "...", "language": null, "vocab": ""}
     -> {"text": "polished text"}

POST /v1/process            # transcribe + polish in one round trip
     {"audio_b64": "...", "language": null, "vocab": "", "polish": true}
     -> {"raw": "raw transcript", "text": "polished text"}
```

### File transcription in remote mode

`voicisst transcribe-file` and the web UI's Files page still decode and chunk
the recording on the client before sending work to the server. That keeps long
recordings away from the short-dictation request limits. M4A/AAC decoding on
the client needs the `media` extra (`pip install "voicisst[media]"`) or
`ffmpeg` on `PATH`; the server only receives normal WAV chunks.

### WebSocket: `/v1/stream?token=...`

Streaming transcription with live partials, used by the client's streaming
mode:

1. Client sends a text frame:
   `{"type": "start", "sample_rate": 16000, "language": null, "vocab": ""}`
2. Client sends binary frames: raw **int16 PCM** chunks (no WAV header).
3. Server sends `{"type": "partial", "text": "..."}` whenever the transcript
   changes — each `text` is the full raw transcript so far. The server
   re-transcribes on its own cadence.
4. Client sends `{"type": "finalize", "vocab": ""}`. The server replies with
   zero or more `{"type": "polish", "text": "..."}` full-text snapshots as
   the polisher streams, then `{"type": "final", "text": "...", "raw": "..."}`
   and closes.
5. Client may send `{"type": "cancel"}` at any point; the server closes.

Errors arrive as `{"type": "error", "message": "...", "hint": "..."}` —
`hint` is a human-readable fix suggestion.

## curl examples

```bash
TOKEN=s3cret
BASE=http://127.0.0.1:8765

# health
curl -s -H "Authorization: Bearer $TOKEN" $BASE/v1/health

# transcribe a 16-bit PCM mono WAV
b64=$(base64 -w0 < sample.wav)            # macOS: base64 < sample.wav | tr -d '\n'
curl -s -X POST $BASE/v1/transcribe \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"audio_b64\": \"$b64\", \"language\": null, \"vocab\": \"\"}"
# -> {"text": "um so we should ship it on thursday"}

# polish text
curl -s -X POST $BASE/v1/polish \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "um so we should ship it on thursday", "language": null, "vocab": ""}'
# -> {"text": "We should ship it on Thursday."}

# one round trip: transcribe + polish
curl -s -X POST $BASE/v1/process \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"audio_b64\": \"$b64\", \"language\": null, \"vocab\": \"\", \"polish\": true}"
# -> {"raw": "...", "text": "..."}
```

## Checking a remote setup

From the client machine:

```bash
voicisst selftest --server http://big-box:8765
```

This checks reachability, auth, and the server's engine health alongside the
usual local checks (mic, hotkeys, typing).
