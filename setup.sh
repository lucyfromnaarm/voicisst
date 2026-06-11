#!/usr/bin/env bash
# Idempotent setup for flow.py.
# Installs system packages, loads uinput, writes a udev rule that lets the
# 'input' group own /dev/uinput, adds you to that group, installs Python
# deps, and installs a *user* ydotoold service (so its socket lives in your
# XDG_RUNTIME_DIR rather than root-owned /tmp).
set -euo pipefail

cd "$(dirname "$(realpath "$0")")"
REPO="$PWD"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

USER_NAME="${SUDO_USER:-$USER}"

log "Installing system packages via dnf (needs sudo)"
sudo dnf install -y \
    ydotool \
    python3-devel \
    portaudio-devel \
    libnotify \
    wl-clipboard \
    pulseaudio-utils

log "Ensuring uinput module is loaded at boot"
sudo install -m 0644 /dev/stdin /etc/modules-load.d/uinput.conf <<<'uinput'
sudo modprobe uinput || true

log "Writing udev rule for /dev/uinput"
sudo install -m 0644 /dev/stdin /etc/udev/rules.d/80-uinput.rules <<'EOF'
# Allow members of the input group to use /dev/uinput (needed by ydotoold).
KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=/dev/uinput || true

log "Ensuring $USER_NAME is in the 'input' group"
if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx input; then
    log "  already in input group"
else
    sudo usermod -aG input "$USER_NAME"
    warn "Added $USER_NAME to 'input' — log out and back in for it to apply."
fi

log "Installing Python deps into user site"
# Fedora 44 ships an unmarked system python so --user works; if a future
# release marks externally-managed, fall back to --break-system-packages or
# a venv at $REPO/.venv.
python3 -m pip install --user --upgrade -r requirements.txt \
  || python3 -m pip install --user --upgrade --break-system-packages -r requirements.txt

log "Installing user systemd units (ydotoold + flow)"
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$REPO/ydotoold.user.service" "$HOME/.config/systemd/user/ydotoold.service"
install -m 0644 "$REPO/flow.service"          "$HOME/.config/systemd/user/flow.service"
systemctl --user daemon-reload

# Disable any system-wide ydotoold (its socket is root-owned in /tmp).
if systemctl is-enabled ydotoold.service >/dev/null 2>&1; then
    warn "Disabling system ydotoold so our user-unit owns the socket"
    sudo systemctl disable --now ydotoold.service || true
fi

log "Enabling ydotoold (user)"
systemctl --user enable --now ydotoold.service
sleep 0.5
systemctl --user status ydotoold.service --no-pager | head -5 || true

log "Checking ollama model"
if command -v ollama >/dev/null; then
    if ollama list | awk '{print $1}' | grep -qx "qwen3.5:4b"; then
        log "  qwen3.5:4b present"
    else
        warn "qwen3.5:4b missing — run: ollama pull qwen3.5:4b"
    fi
else
    warn "ollama not installed — POLISH_ENABLED=0 will skip the polish step"
fi

cat <<EOF

Setup done. Next:
  1. If you were just added to the 'input' group, log out and back in.
  2. Verify everything:
        python3 flow.py --selftest
  3. Run it foreground first:
        python3 flow.py
     Hold the Menu key, speak, release.
  4. When happy, run as a user service:
        systemctl --user enable --now flow.service
        journalctl --user -u flow -f
EOF
