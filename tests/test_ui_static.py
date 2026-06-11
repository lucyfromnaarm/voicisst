"""Static frontend assets: auth-guarded serving, accessible markup, schema sync.

These tests parse the shipped index.html/app.js/app.css directly (and serve
them through the real create_ui_app) so regressions in labels, landmarks, or
the JS settings SCHEMA fail loudly without a browser.
"""

from __future__ import annotations

import dataclasses
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voicisst.config import Config
from voicisst.ui.server import STATIC_DIR, create_ui_app

TOKEN = "static-t0ken-xyz"

INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
APP_CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

# External request literals: forbidden everywhere in code. Only loopback hosts
# (config defaults like the local Ollama address) are allowed to appear.
EXTERNAL_URL = re.compile(r"https?://(?!localhost|127\.0\.0\.1)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Served through the real app, auth honored


def test_assets_served_through_app_with_token_auth(cfg: Config, tmp_path: Path) -> None:
    app = create_ui_app(cfg, token=TOKEN, config_file=tmp_path / "config.toml")

    bare = TestClient(app)
    assert bare.get("/").status_code == 403
    assert bare.get("/static/app.js").status_code == 403
    assert bare.get("/static/app.css").status_code == 403

    client = TestClient(app)
    client.headers["X-Voicisst-Token"] = TOKEN
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.content == INDEX.encode("utf-8")
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.content == APP_JS.encode("utf-8")
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "css" in r.headers["content-type"]
    assert r.content == APP_CSS.encode("utf-8")


# ---------------------------------------------------------------------------
# Markup audit (html.parser, no browser)


class MarkupAudit(HTMLParser):
    """Collects tags, ids, form controls, label targets, aria-live, hrefs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: set[str] = set()
        self.ids: set[str] = set()
        self.controls: list[tuple[str, dict[str, str | None]]] = []
        self.label_targets: set[str] = set()
        self.aria_live: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.tags.add(tag)
        if a.get("id"):
            self.ids.add(a["id"] or "")
        if tag in ("input", "select", "textarea"):
            self.controls.append((tag, a))
        if tag == "label" and a.get("for"):
            self.label_targets.add(a["for"] or "")
        if a.get("aria-live"):
            self.aria_live.append(a["aria-live"] or "")
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"] or "")


@pytest.fixture(scope="module")
def audit() -> MarkupAudit:
    parser = MarkupAudit()
    parser.feed(INDEX)
    return parser


def test_landmarks_views_and_live_regions(audit: MarkupAudit) -> None:
    # Semantic landmarks are a hard requirement.
    assert {"header", "nav", "main"} <= audit.tags
    # Grouped controls use fieldset/legend.
    assert {"fieldset", "legend"} <= audit.tags
    # The four hash-routed views plus the wizard root exist by id.
    assert {"dashboard", "setup", "settings", "help", "wizard", "wizard-progress"} <= audit.ids
    # The nav links to every view.
    for view in ("#dashboard", "#setup", "#settings", "#help"):
        assert view in audit.hrefs
    # Polite live regions announce state changes and save results.
    assert "polite" in audit.aria_live
    assert len(audit.aria_live) >= 2  # dashboard state + settings save status


def test_every_form_control_is_labelled(audit: MarkupAudit) -> None:
    assert audit.controls, "index.html should contain form controls"
    for tag, attrs in audit.controls:
        assert attrs.get("type") != "hidden"
        control_id = attrs.get("id")
        assert control_id, f"unlabelled <{tag}> without an id: {attrs}"
        labelled = (
            control_id in audit.label_targets
            or attrs.get("aria-label")
            or attrs.get("aria-labelledby")
        )
        assert labelled, f"<{tag} id={control_id!r}> has no label[for] or aria-label"


def test_single_es_module_and_state_hero_present(audit: MarkupAudit) -> None:
    assert '<script type="module" src="/static/app.js"></script>' in INDEX
    assert INDEX.count("<script") == 1  # exactly one script: app.js
    # The dashboard hero: state circle + textual state (never color-only).
    assert {"state-circle", "state-icon", "state-name", "state-detail"} <= audit.ids
    assert {"conn-note", "save-status", "raw-toml"} <= audit.ids


# ---------------------------------------------------------------------------
# Privacy: no external requests from code


def test_app_js_makes_no_external_requests() -> None:
    bad = EXTERNAL_URL.search(APP_JS)
    context = APP_JS[bad.start() : bad.start() + 60] if bad else ""
    assert bad is None, f"external URL literal in app.js near: {context!r}"
    # fetch()/WebSocket never take an absolute http(s) literal.
    assert not re.search(r"""fetch\(\s*["'`]https?:""", APP_JS)
    assert not re.search(r"""new WebSocket\(\s*["'`]""", APP_JS)
    assert "github.com" not in APP_JS  # docs link lives in index.html only


def test_app_css_has_no_urls() -> None:
    assert not re.search(r"https?://", APP_CSS)


def test_index_html_external_links_are_github_docs_only() -> None:
    for match in re.finditer(r"https?://[^\s\"'<>]+", INDEX):
        url = match.group(0)
        assert url.startswith("https://github.com/"), f"unexpected external URL {url!r}"


# ---------------------------------------------------------------------------
# CSS honors user preferences


def test_css_media_queries_for_motion_and_color_scheme() -> None:
    assert "@media (prefers-color-scheme: dark)" in APP_CSS
    assert "@media (prefers-reduced-motion: reduce)" in APP_CSS
    # The pulse animation is opt-in for motion-okay users only.
    assert "@media (prefers-reduced-motion: no-preference)" in APP_CSS


# ---------------------------------------------------------------------------
# The JS settings SCHEMA mirrors config.py


def _extract_schema() -> dict:
    match = re.search(r"const SCHEMA = (\{.*?\n\});", APP_JS, re.S)
    assert match, "app.js must define a pure-JSON `const SCHEMA = {...};` block"
    return json.loads(match.group(1))


def test_schema_sections_and_keys_match_config() -> None:
    schema = _extract_schema()
    reference = Config()
    section_names = [f.name for f in dataclasses.fields(reference)]
    assert list(schema) == section_names  # same sections, same order

    for section_field in dataclasses.fields(reference):
        name = section_field.name
        if name == "replacements":
            assert schema[name].get("kind") == "replacements"
            assert schema[name]["help"].strip()
            continue
        section = getattr(reference, name)
        expected = {f.name for f in dataclasses.fields(section)}
        got = {entry["key"] for entry in schema[name]["fields"]}
        assert got == expected, f"SCHEMA out of sync for [{name}]: {got ^ expected}"


def test_schema_field_types_defaults_and_help() -> None:
    schema = _extract_schema()
    reference = Config()
    for name, spec in schema.items():
        if spec.get("kind") == "replacements":
            continue
        section = getattr(reference, name)
        for entry in spec["fields"]:
            where = f"{name}.{entry['key']}"
            assert entry["type"] in {"str", "int", "float", "bool", "enum", "list"}, where
            assert entry["help"].strip(), f"{where} needs one-line help text"
            assert entry["label"].strip(), f"{where} needs a label"
            assert "default" in entry, where
            if entry["type"] == "enum":
                assert entry["options"], where
                assert entry["default"] in entry["options"], where
            if (name, entry["key"]) == ("hotkey", "keys"):
                continue  # the real default depends on the platform
            actual = getattr(section, entry["key"])
            if isinstance(actual, list):
                assert list(entry["default"]) == list(actual), where
            else:
                assert entry["default"] == actual, where
                assert type(entry["default"]) is type(actual), where


# ---------------------------------------------------------------------------
# Conditional visibility: `when` objects in SCHEMA, the show-all toggle


def _schema_fields(schema: dict) -> dict[str, dict]:
    return {
        f"{name}.{entry['key']}": entry
        for name, spec in schema.items()
        if spec.get("kind") != "replacements"
        for entry in spec["fields"]
    }


def test_schema_when_keys_gate_conditional_fields() -> None:
    schema = _extract_schema()
    fields = _schema_fields(schema)

    # Remote-server details only matter in remote mode.
    for key in ("engine.server_url", "engine.token", "engine.request_timeout"):
        assert fields[key]["when"] == {"engine.mode": "remote"}, key

    # Local whisper knobs and the whole polish section are local-mode only;
    # whisper.language stays visible in both modes (it is sent to the server).
    for key in (
        "whisper.model",
        "whisper.device",
        "whisper.compute",
        "whisper.beam_size",
        "whisper.vad_filter",
    ):
        assert fields[key]["when"] == {"engine.mode": "local"}, key
    assert "when" not in fields["whisper.language"]
    assert schema["polish"]["when"] == {"engine.mode": "local"}

    # Platform gates.
    assert fields["dictionary.use_selection"]["when"] == {"platform": "linux"}
    assert fields["hotkey.backend"]["option_when"] == {"evdev": {"platform": "linux"}}
    assert fields["output.paste_chord"]["option_when"] == {"cmd-v": {"platform": "darwin"}}

    # The server section renders inside a collapsed <details>.
    assert schema["server"]["details_label"] == "Server hosting (voicisst serve)"

    # Every `when` condition references "platform" or a real schema field, and
    # compares against a real enum option where the driver is an enum.
    valid_keys = set(fields) | {"platform"}
    conditions: list[dict] = []
    for spec in schema.values():
        if spec.get("when"):
            conditions.append(spec["when"])
        for entry in spec.get("fields", []):
            if entry.get("when"):
                conditions.append(entry["when"])
            conditions.extend((entry.get("option_when") or {}).values())
    assert conditions, "SCHEMA should declare conditional visibility"
    for when in conditions:
        for key, expected in when.items():
            assert key in valid_keys, f"unknown `when` driver {key!r}"
            assert isinstance(expected, str), f"`when` values are strings: {key!r}"
            if key != "platform" and fields[key]["type"] == "enum":
                assert expected in fields[key]["options"], f"{key}={expected!r}"


def test_schema_lmstudio_model_dropdown_and_advanced_flags() -> None:
    schema = _extract_schema()
    fields = _schema_fields(schema)

    # LM Studio is a first-class polish backend, in settings and the wizard.
    assert "lmstudio" in fields["polish.backend"]["options"]
    assert 'id="wiz-polish-lmstudio"' in INDEX

    # The model field is a datalist dropdown fed by /api/polish/models.
    assert fields["polish.model"].get("widget") == "models"
    assert "/api/polish/models" in APP_JS
    assert 'list="wiz-polish-models"' in INDEX
    assert 'id="wiz-polish-models"' in INDEX

    # The API key only shows for backends that can need one.
    assert fields["polish.api_key"]["when"] == {"polish.backend": "openai"}

    # Rarely-touched knobs are flagged advanced (rendered behind a closed
    # "More options" disclosure); the everyday essentials are not.
    advanced = {key for key, entry in fields.items() if entry.get("advanced")}
    for key in (
        "engine.request_timeout",
        "whisper.beam_size",
        "polish.num_ctx",
        "polish.vram_unload_below_mb",
        "audio.rms_gate",
        "output.key_delay_ms",
        "ui.web_port",
    ):
        assert key in advanced, key
    for key in (
        "engine.mode",
        "whisper.model",
        "whisper.language",
        "polish.enabled",
        "polish.backend",
        "polish.model",
        "hotkey.keys",
        "hotkey.mode",
        "audio.input_device",
        "output.mode",
        "dictionary.words",
        "history.enabled",
    ):
        assert key not in advanced, key
    assert "More options" in APP_JS


def test_show_all_toggle_and_remote_note_wired() -> None:
    # The escape hatch exists in the markup, with a proper label.
    assert 'id="show-all"' in INDEX
    assert "Show all settings" in INDEX
    # It persists via localStorage and announces through the aria-live region.
    assert "localStorage" in APP_JS
    assert "not used in your current setup" in APP_JS
    # Remote mode replaces transcription/polish settings with one short line.
    assert "Transcription and polish run on your server — configure them there." in APP_JS
    # Hiding uses the hidden attribute (kept out of the accessibility tree and
    # tab order), driven by one small evaluator over the `when` objects.
    assert "applyVisibility" in APP_JS
    assert "whenMatches" in APP_JS
