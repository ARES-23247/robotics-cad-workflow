#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "[1/4] Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[2/4] Creating Python 3.12 .venv..."
  uv venv --python 3.12 .venv
fi

echo "[3/4] Installing Python CAD dependencies..."
uv pip install --python .venv/bin/python -r requirements.txt

echo "[4/4] Running setup & verification..."
.venv/bin/python tools/setup_solidcraft_cots.py --no-build "$@"
