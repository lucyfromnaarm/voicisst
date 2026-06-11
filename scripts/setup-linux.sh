#!/usr/bin/env bash
# Linux system setup for Flow (idempotent, multi-distro).
#
# Installs the system packages Flow needs (ydotool, wl-clipboard, libnotify,
# PortAudio), loads the uinput module at boot, writes a udev rule giving the
# 'input' group access to /dev/uinput, adds you to that group, and installs
# + starts a *user* ydotoold systemd unit (so its socket lives in your
# XDG_RUNTIME_DIR rather than root-owned /tmp).
#
# Supports dnf (Fedora), apt (Debian/Ubuntu), pacman (Arch), zypper (openSUSE).
set -euo pipefail

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "this script is Linux-only"

USER_NAME="${SUDO_USER:-$USER}"
# Locate the repo when run from a checkout; empty when piped (curl | bash).
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(dirname "$SCRIPT_DIR")"
else
    SCRIPT_DIR=""
    REPO_ROOT=""
fi

# --- 1. system packages -------------------------------------------------------
# Package names differ per distro:
#   notify-send : libnotify (dnf/pacman) / libnotify-bin (apt) / libnotify-tools (zypper)
#   PortAudio   : portaudio (dnf/pacman) / libportaudio2 (apt/zypper)
PM=""
PKGS=()
if command -v dnf >/dev/null 2>&1; then
    PM="dnf"; PKGS=(ydotool wl-clipboard libnotify portaudio)
elif command -v apt-get >/dev/null 2>&1; then
    PM="apt"; PKGS=(ydotool wl-clipboard libnotify-bin libportaudio2)
elif command -v pacman >/dev/null 2>&1; then
    PM="pacman"; PKGS=(ydotool wl-clipboard libnotify portaudio)
elif command -v zypper >/dev/null 2>&1; then
    PM="zypper"; PKGS=(ydotool wl-clipboard libnotify-tools libportaudio2)
else
    warn "no supported package manager found (dnf/apt/pacman/zypper)."
    warn "Install manually: ydotool, wl-clipboard, libnotify, PortAudio — then re-run."
fi

pkg_install() {
    # Install one package; return non-zero (after a warning) if unavailable,
    # e.g. ydotool is missing from older Debian/Ubuntu releases.
    case "$PM" in
        dnf)    sudo dnf install -y "$1" ;;
        apt)    sudo apt-get install -y "$1" ;;
        pacman) sudo pacman -S --needed --noconfirm "$1" ;;
        zypper) sudo zypper --non-interactive install "$1" ;;
    esac
}

if [ -n "$PM" ]; then
    log "Installing system packages via $PM (needs sudo): ${PKGS[*]}"
    [ "$PM" = "apt" ] && sudo apt-get update
    for pkg in "${PKGS[@]}"; do
        pkg_install "$pkg" || warn "could not install '$pkg' — install it manually
   (ydotool may need a newer distro release or a build from source:
    https://github.com/ReimuNotMoe/ydotool)"
    done
fi

# --- 2. uinput at boot ---------------------------------------------------------
log "Ensuring uinput module is loaded at boot"
sudo install -m 0644 /dev/stdin /etc/modules-load.d/uinput.conf <<<'uinput'
sudo modprobe uinput || true

# --- 3. udev rule for /dev/uinput ---------------------------------------------
log "Writing udev rule for /dev/uinput"
sudo install -m 0644 /dev/stdin /etc/udev/rules.d/80-uinput.rules <<'EOF'
# Allow members of the input group to use /dev/uinput (needed by ydotoold).
KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=/dev/uinput || true

# --- 4. input group ------------------------------------------------------------
log "Ensuring $USER_NAME is in the 'input' group (evdev hotkeys + uinput)"
if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx input; then
    log "  already in input group"
else
    sudo usermod -aG input "$USER_NAME"
    warn "Added $USER_NAME to 'input' — log out and back in for it to apply."
fi

# --- 5. user ydotoold unit -------------------------------------------------------
log "Installing user ydotoold systemd unit"
mkdir -p "$HOME/.config/systemd/user"
UNIT_SRC="${REPO_ROOT:+$REPO_ROOT/packaging/systemd/ydotoold.service}"
if [ -n "$UNIT_SRC" ] && [ -f "$UNIT_SRC" ]; then
    install -m 0644 "$UNIT_SRC" "$HOME/.config/systemd/user/ydotoold.service"
else
    # Running standalone (curl | bash) — write the unit inline.
    cat > "$HOME/.config/systemd/user/ydotoold.service" <<'EOF'
[Unit]
Description=ydotoold (user instance)
Documentation=man:ydotoold(8)

[Service]
Type=simple
ExecStart=/usr/bin/ydotoold --socket-path=%t/.ydotool_socket --socket-own=%U:%U --socket-perm=0600
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
fi
systemctl --user daemon-reload

# Disable any system-wide ydotoold (its socket is root-owned in /tmp).
if systemctl is-enabled ydotoold.service >/dev/null 2>&1; then
    warn "Disabling system ydotoold so the user unit owns the socket"
    sudo systemctl disable --now ydotoold.service || true
fi

log "Enabling ydotoold (user)"
if systemctl --user enable --now ydotoold.service; then
    sleep 0.5
    systemctl --user status ydotoold.service --no-pager 2>/dev/null | head -5 || true
else
    warn "could not start ydotoold — is it installed? Check: journalctl --user -u ydotoold"
fi

# --- 6. done ---------------------------------------------------------------------
FLOW_UNIT_SRC="${REPO_ROOT:-<repo>}/packaging/systemd/flow.service"
cat <<EOF

Linux setup done. Next:
  1. If you were just added to the 'input' group, log out and back in.
  2. Verify everything:        flow selftest
  3. Run it in the foreground: flow run
  4. To start Flow at login as a user service:
       install -Dm0644 "$FLOW_UNIT_SRC" \\
           ~/.config/systemd/user/flow.service
       systemctl --user daemon-reload
       systemctl --user enable --now flow.service
       journalctl --user -u flow -f
     (The unit expects 'flow' at ~/.local/bin/flow — edit ExecStart if yours
      lives elsewhere; 'command -v flow' tells you where it is.)
EOF
