#!/usr/bin/env bash
set -e

GREEN='\033[1;32m'; RESET='\033[0m'; DIM='\033[0;90m'

echo -e "${GREEN}SwissIO local workbench${RESET}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required."
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

pip install -q --upgrade pip
pip install -q -r requirements.txt

echo -e "${DIM}Open http://localhost:8765${RESET}"
(sleep 1.5 && (open http://localhost:8765 >/dev/null 2>&1 || true)) &
python3 app.py
