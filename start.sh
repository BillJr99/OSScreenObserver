#!/usr/bin/env bash
# start.sh — Linux launcher for OSScreenObserver.
#
# Detects missing system + Python dependencies, prompts before installing,
# then starts the server in the default mode (inspect — interactive VLM
# setup runs only in this mode).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── helpers ─────────────────────────────────────────────────────────────────

confirm() {
    # confirm "prompt text" — returns 0 on y/Y/<Enter>, 1 otherwise.
    local prompt="$1"
    local reply
    read -r -p "$prompt [Y/n] " reply
    case "$reply" in
        ""|y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

have() { command -v "$1" >/dev/null 2>&1; }

detect_pkg_manager() {
    if   have apt-get; then echo "apt"
    elif have dnf;     then echo "dnf"
    elif have pacman;  then echo "pacman"
    elif have zypper;  then echo "zypper"
    else echo ""
    fi
}

install_pkg() {
    # install_pkg <apt-name> <dnf-name> <pacman-name> <zypper-name>
    local pm; pm="$(detect_pkg_manager)"
    case "$pm" in
        apt)    sudo apt-get update && sudo apt-get install -y "$1" ;;
        dnf)    sudo dnf install -y "$2" ;;
        pacman) sudo pacman -S --noconfirm "$3" ;;
        zypper) sudo zypper install -y "$4" ;;
        *)
            echo "  ✗ No supported package manager found." >&2
            echo "    Install manually: apt='$1', dnf='$2', pacman='$3', zypper='$4'" >&2
            return 1
            ;;
    esac
}

echo "═══════════════════════════════════════════════════════════════"
echo "  OSScreenObserver — Linux launcher"
echo "═══════════════════════════════════════════════════════════════"

# ─── Python ──────────────────────────────────────────────────────────────────

if ! have python3; then
    echo "  ✗ Python 3 is required but was not found on PATH."
    if confirm "Install Python 3 now?"; then
        install_pkg python3 python3 python python3
    else
        echo "  Aborting — Python 3 is required."
        exit 1
    fi
fi
echo "  ✓ python3 → $(python3 --version)"

# Debian/Ubuntu split venv + pip out of the python3 metapackage; install
# them explicitly when missing. On dnf/pacman/zypper these ship with python3.
if [[ "$(detect_pkg_manager)" == "apt" ]]; then
    if ! python3 -c "import venv" >/dev/null 2>&1; then
        confirm "Install python3-venv (required to create the .venv)?" && \
            sudo apt-get install -y python3-venv
    fi
    if ! python3 -m pip --version >/dev/null 2>&1; then
        confirm "Install python3-pip?" && sudo apt-get install -y python3-pip
    fi
fi

# ─── Tesseract (OCR) ─────────────────────────────────────────────────────────

if ! have tesseract; then
    echo "  ✗ tesseract not found — OCR will be unavailable."
    if confirm "Install tesseract-ocr (system package)?"; then
        install_pkg tesseract-ocr tesseract tesseract tesseract-ocr || \
            echo "  (continuing without OCR; description.from_ocr will report missing binary)"
    fi
else
    echo "  ✓ tesseract → $(tesseract --version 2>&1 | head -n1)"
fi

# ─── wmctrl (window enumeration) ─────────────────────────────────────────────

if ! have wmctrl; then
    echo "  ✗ wmctrl not found — window enumeration will fall back to python-xlib."
    if confirm "Install wmctrl?"; then
        install_pkg wmctrl wmctrl wmctrl wmctrl || true
    fi
else
    echo "  ✓ wmctrl present"
fi

# ─── Ollama (optional — for local VLM) ───────────────────────────────────────

if ! have ollama; then
    echo "  ⓘ ollama not found — required only if you want a local VLM."
    if confirm "Install Ollama via the official install script?"; then
        curl -fsSL https://ollama.com/install.sh | sh
    else
        echo "  (skipping; either set vlm.enabled=false in config.json or"
        echo "   point vlm.base_url at a remote endpoint)"
    fi
else
    echo "  ✓ ollama → $(ollama --version 2>/dev/null | head -n1)"
fi

# ─── Python virtualenv + pip install ─────────────────────────────────────────

VENV_DIR="${SCRIPT_DIR}/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    if confirm "Create a project virtualenv at .venv/?"; then
        python3 -m venv "$VENV_DIR"
    else
        echo "  (using system Python; you may want a venv to avoid clobbering system packages)"
    fi
fi

if [[ -d "$VENV_DIR" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    echo "  ✓ activated virtualenv .venv/"
fi

if confirm "Install/upgrade Python dependencies from requirements.txt?"; then
    python3 -m pip install --upgrade pip
    python3 -m pip install -r requirements.txt
fi

# ─── Bootstrap config.json + fix tesseract path ──────────────────────────────

python3 setup_config.py || true

# ─── Launch ──────────────────────────────────────────────────────────────────

echo ""
echo "  Starting OSScreenObserver (auto mode: TTY → inspect, pipe → both)…"
echo "  Web UI → http://127.0.0.1:5001"
echo ""
exec python3 main.py "$@"
