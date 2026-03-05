#!/usr/bin/env bash
# USB Nexus — macOS Setup & Launch Script
set -e

GREEN='\033[1;32m' DIM='\033[0;90m' RESET='\033[0m' BOLD='\033[1m'

echo -e "${GREEN}"
echo "  ██╗   ██╗███████╗██████╗     ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗"
echo "  ██║   ██║██╔════╝██╔══██╗    ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝"
echo "  ██║   ██║███████╗██████╔╝    ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗"
echo "  ██║   ██║╚════██║██╔══██╗    ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║"
echo "  ╚██████╔╝███████║██████╔╝    ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║"
echo "   ╚═════╝ ╚══════╝╚═════╝     ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
echo -e "${RESET}"
echo -e "  ${BOLD}Universal USB-C Device Terminal${RESET}"
echo ""

# ── Check Python ────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "  ✗ Python 3 required. Install via: brew install python"
  exit 1
fi
echo -e "  ${GREEN}✓${RESET} Python $(python3 --version | cut -d' ' -f2)"

# ── Check / install Homebrew deps ───────────────────────────────────
echo ""
echo -e "  ${DIM}Checking system dependencies...${RESET}"

install_brew_pkg() {
  if ! command -v "$1" &>/dev/null; then
    echo -e "  ${DIM}Installing $1 via Homebrew...${RESET}"
    brew install "$2" 2>/dev/null || echo -e "  ⚠ Could not install $1 (non-fatal)"
  else
    echo -e "  ${GREEN}✓${RESET} $1 found"
  fi
}

if command -v brew &>/dev/null; then
  install_brew_pkg "libusb-config" "libusb"
  install_brew_pkg "openocd"       "open-ocd"
  install_brew_pkg "dfu-util"      "dfu-util"
else
  echo "  ⚠ Homebrew not found — skipping system deps"
  echo "    Install from https://brew.sh then run:"
  echo "    brew install libusb open-ocd dfu-util"
fi

# ── Python venv & packages ──────────────────────────────────────────
echo ""
echo -e "  ${DIM}Setting up Python environment...${RESET}"

VENV_DIR="$(dirname "$0")/.venv"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install -q --upgrade pip
pip install -q -r "$(dirname "$0")/requirements.txt"
echo -e "  ${GREEN}✓${RESET} Python packages installed"

# ── Launch ──────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}▶ Launching USB Nexus...${RESET}"
echo -e "  ${DIM}Open your browser: http://localhost:8765${RESET}"
echo ""

# Auto-open browser after brief delay
(sleep 1.5 && open http://localhost:8765 2>/dev/null) &

cd "$(dirname "$0")" && python3 app.py
