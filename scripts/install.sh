#!/usr/bin/env bash
# Voicisst installer for Linux and macOS.
#
# Installs voicisst[local,server,tray,ui] as an isolated CLI tool using
# uv (preferred) or pipx (installed via pip --user as a fallback). Tries
# PyPI first; if the package is not published there yet, installs straight
# from the git repository.
#
# Usage:
#   ./scripts/install.sh
#   curl -fsSL https://github.com/lucyfromnaarm/voicisst/raw/main/scripts/install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/lucyfromnaarm/voicisst"
EXTRAS="local,server,tray,ui"
PYPI_SPEC="voicisst[${EXTRAS}]"
GIT_SPEC="voicisst[${EXTRAS}] @ git+${REPO_URL}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

PYTHON="$(command -v python3 || command -v python || true)"

# --- pick an installer: uv > pipx > (pip --user pipx) -----------------------
TOOL=""
if command -v uv >/dev/null 2>&1; then
    TOOL="uv"
elif command -v pipx >/dev/null 2>&1; then
    TOOL="pipx"
else
    [ -n "$PYTHON" ] || die "python3 not found — install Python 3.10+ first
  (e.g. 'sudo dnf install python3' / 'sudo apt install python3' / 'brew install python')"
    log "Neither uv nor pipx found — installing pipx into your user site"
    "$PYTHON" -m pip install --user pipx \
        || die "could not install pipx — install uv or pipx manually:
  https://docs.astral.sh/uv/  or  https://pipx.pypa.io/"
    "$PYTHON" -m pipx ensurepath || true
    TOOL="pipx-module"
fi
log "Using installer: ${TOOL}"

install_spec() {
    case "$TOOL" in
        uv)          uv tool install --force "$1" ;;
        pipx)        pipx install --force "$1" ;;
        pipx-module) "$PYTHON" -m pipx install --force "$1" ;;
    esac
}

# --- install: PyPI first, git fallback ---------------------------------------
log "Installing ${PYPI_SPEC} from PyPI"
if install_spec "$PYPI_SPEC"; then
    log "Installed from PyPI"
else
    warn "PyPI install failed (package not published yet?) — installing from git"
    command -v git >/dev/null 2>&1 || die "git not found — install git and re-run"
    install_spec "$GIT_SPEC" || die "install from ${REPO_URL} failed"
    log "Installed from ${REPO_URL}"
fi

# --- next steps ---------------------------------------------------------------
cat <<'EOF'

Voicisst is installed. Next steps:
  1. New terminal (or `hash -r`) so `voicisst` is on PATH (usually ~/.local/bin).
  2. voicisst ui              # guided setup in your browser
                              # (or by hand: voicisst config init)
  3. voicisst selftest        # check mic, hotkeys, whisper, polish, injection
  4. voicisst run             # hold the hotkey, speak, release

For LLM polish, install Ollama (https://ollama.com) and pull a model, or
point [polish] at any OpenAI-compatible server in the config.
EOF

# --- Linux extras: ydotool/uinput/udev/systemd --------------------------------
if [ "$(uname -s)" = "Linux" ]; then
    script_dir=""
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    setup_script="${script_dir}/setup-linux.sh"
    if [ -n "$script_dir" ] && [ -f "$setup_script" ]; then
        if [ -t 0 ]; then
            printf 'Run Linux setup now (ydotool, /dev/uinput udev rule, input group)? [Y/n] '
            read -r answer
            case "$answer" in
                n*|N*) log "Skipped. Run later: $setup_script" ;;
                *)     bash "$setup_script" ;;
            esac
        else
            log "Non-interactive shell — run scripts/setup-linux.sh afterwards to set up
    ydotool, the /dev/uinput udev rule, and the 'input' group."
        fi
    else
        log "Linux needs one more step (ydotool + /dev/uinput permissions). Run:
    curl -fsSL ${REPO_URL}/raw/main/scripts/setup-linux.sh | bash"
    fi
fi
