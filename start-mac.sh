#!/usr/bin/env bash
# start-mac.sh — macOS launcher for OSScreenObserver.
#
# Detects missing system + Python dependencies, prompts before installing,
# then starts the server in the default mode (inspect — interactive VLM
# setup runs only in this mode).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── helpers ─────────────────────────────────────────────────────────────────

confirm() {
    local prompt="$1"
    local reply
    read -r -p "$prompt [Y/n] " reply
    case "$reply" in
        ""|y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

have() { command -v "$1" >/dev/null 2>&1; }

echo "═══════════════════════════════════════════════════════════════"
echo "  OSScreenObserver — macOS launcher"
echo "═══════════════════════════════════════════════════════════════"

# ─── Homebrew (prerequisite for everything else) ─────────────────────────────

if ! have brew; then
    echo "  ✗ Homebrew not found — required to install tesseract / ollama / python."
    echo "    See https://brew.sh"
    if confirm "Install Homebrew now?"; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # Add brew to PATH for this shell (Apple Silicon vs Intel).
        if [[ -x /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    else
        echo "  Continuing without brew — you must install dependencies manually."
    fi
else
    echo "  ✓ brew → $(brew --version | head -n1)"
fi

# ─── Python 3 ────────────────────────────────────────────────────────────────

if ! have python3; then
    echo "  ✗ python3 not found."
    if have brew && confirm "Install Python 3 via Homebrew?"; then
        brew install python
    else
        echo "  Aborting — Python 3 is required."
        exit 1
    fi
fi
echo "  ✓ python3 → $(python3 --version)"

# ─── Tesseract (OCR) ─────────────────────────────────────────────────────────

if ! have tesseract; then
    echo "  ✗ tesseract not found — OCR will be unavailable."
    if have brew && confirm "Install tesseract via Homebrew?"; then
        brew install tesseract
    fi
else
    echo "  ✓ tesseract → $(tesseract --version 2>&1 | head -n1)"
fi

# ─── Ollama (optional — for local VLM) ───────────────────────────────────────

if ! have ollama; then
    echo "  ⓘ ollama not found — required only if you want a local VLM."
    if have brew && confirm "Install Ollama via Homebrew?"; then
        brew install ollama
        echo "  (start the Ollama service in a separate shell: 'ollama serve')"
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
    if confirm "Also install pyobjc for full macOS accessibility-tree support?"; then
        python3 -m pip install pyobjc
    fi
fi

# ─── macOS accessibility-permissions note ────────────────────────────────────

cat <<'EOF'

  ⓘ macOS requires Accessibility + Screen Recording permissions for the
    AX adapter and screenshot capture. The first run will trigger a
    permission prompt; grant Terminal/iTerm/your shell host in
    System Settings → Privacy & Security → Accessibility and Screen Recording.

EOF

# ─── Launch ──────────────────────────────────────────────────────────────────

echo "  Starting OSScreenObserver (default mode: inspect)…"
echo "  Web UI → http://127.0.0.1:5001"
echo ""
exec python3 main.py "$@"
