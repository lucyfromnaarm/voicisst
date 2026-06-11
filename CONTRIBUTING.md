# Contributing to Flow

Thanks for helping. Bug reports, new injector/listener backends, docs fixes,
and accessibility feedback are all wanted.

## Dev setup

```bash
git clone https://github.com/lucyfromnaarm/flow-dictation
cd flow-dictation
uv venv --python 3.12 .venv
source .venv/bin/activate                  # plain python -m venv works too
uv pip install -e ".[all,dev]"             # or: pip install -e ".[all,dev]"

pytest                                      # must pass headless
ruff check src tests                        # must be clean
```

The test suite runs with no audio device, no GPU, no network, and no DISPLAY
— exactly like CI, which runs ruff + pytest on Ubuntu, macOS, and Windows
against Python 3.10 and 3.12.

## SPEC.md is the contract

[`SPEC.md`](SPEC.md) defines every module's API and semantics. Code must
match it; if you think the spec is wrong, change the spec in the same PR and
say why. Don't make modules drift from it silently.

## House rules

These are enforced (CI and review), not aspirational:

- **Lazy imports** for anything hardware/platform/heavy: `sounddevice`,
  `evdev`, `pynput`, `faster_whisper`, `fastapi`, `pystray` get imported
  inside the function or method that uses them, never at module top. This is
  what lets a client install skip the server deps, lets tests run headless,
  and keeps startup fast. `numpy`, `requests`, and stdlib are fine at top
  level.
- Python ≥ 3.10, `from __future__ import annotations`, type hints
  everywhere.
- Ruff config lives in `pyproject.toml`: line length 100, rules E, F, W, I,
  UP, B.
- Runtime errors must help the user fix things. Not "connection refused" but
  "ollama not running — try `systemctl status ollama`". `EngineError` has a
  `.hint` field for exactly this.
- Every string delivered to the focused window goes through
  `textproc.sanitize()` first (terminal-escape safety).
- No `print()` to stdout in library code — use the `notify` helpers or
  stderr.

## Tests

- Headless, always. Mock `subprocess` and `requests` with `monkeypatch`;
  inject fake hardware modules with
  `monkeypatch.setitem(sys.modules, "faster_whisper", fake)`.
- No `time.sleep` over 0.2 s; no skip-on-CI markers — everything runs
  everywhere.
- Server tests use `fastapi.testclient.TestClient` with a fake engine
  injected through `create_app(engine, token=...)`.
- Shared fixtures (tmp config, fake engine) belong in `tests/conftest.py`.

## Adding an injector backend

Injectors put text into the focused window. To support a new mechanism (a
new compositor protocol, a new OS):

1. Create `src/flow_dictation/inject/yourbackend.py` with a class
   subclassing `Injector` from `inject/base.py`. Implement `name`, the
   `available()` classmethod (cheap, no side effects — checked at startup),
   and `type_text`, `backspace`, `paste_chord`, `tap_escape`.
2. Honor the contracts: `type_text` translates `\n` per
   `cfg.output.newline_mode`; `backspace(n)` sends exactly `n` deletions,
   never more — streaming mode trusts this to avoid eating user text.
3. Add it to the platform picker in `inject/__init__.py` (`get_injector`),
   in the right priority order.
4. Test it headless by mocking the subprocess calls or module it drives.

## Adding a hotkey listener backend

Same shape: subclass `HotkeyListener` from `hotkeys/base.py`
(`start`/`stop`/`available`, calling `on_press`/`on_release`/`on_backspace`),
register it in `hotkeys/__init__.py` (`get_listener`), suppress key
autorepeat, and document your backend's key-name format in
`docs/CONFIGURATION.md`.

## PRs

- One change per PR, with tests for new behavior.
- `pytest` and `ruff check src tests` clean before you push; CI repeats them
  on all three OSes.
- Update the docs (`docs/`, `README.md`) and `CHANGELOG.md` when behavior or
  config changes.
- New config fields need a default that keeps existing setups working.
- For anything user-visible, say in the PR what you ran by hand to verify it
  (which OS, which compositor) — much of Flow can only be exercised on real
  hardware.

## Code of conduct

Be kind. Details in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
