#!/usr/bin/env bash
# One-time setup on Linux / macOS. FreeCAD must already be importable (see fc.sh for the conda one-liner).
#   ./setup.sh              # install python deps into ./vendor and run the tests
#   ./setup.sh --skip-claude   # local models only
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"
PY="${FREECAD_PYTHON:-python3}"
"$PY" -c "import sys; sys.path.insert(0, '.'); import step_recon, FreeCAD; print('FreeCAD', '.'.join(FreeCAD.Version()[:3]), 'python', sys.version.split()[0])" \
  || { echo "FreeCAD is not importable. Install FreeCAD 1.x (conda-forge) or set FREECAD_BIN / FREECAD_PYTHON."; exit 1; }
if [ "$1" != "--skip-claude" ]; then
  "$PY" -m pip install --quiet --target vendor -r requirements.txt
fi
"$PY" run_tests.py
echo
echo "Setup OK. Claude: export ANTHROPIC_API_KEY=... && ./fc.sh run.py part.step"
echo "          Local : ./fc.sh run.py part.step --backend openai --model qwen3:8b"
