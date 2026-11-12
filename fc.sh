#!/usr/bin/env bash
# Run a project script with a Python that can `import FreeCAD` (Linux / macOS).
#   ./fc.sh run.py part.step
# Easiest FreeCAD setup:  conda create -n steprecon -c conda-forge freecad python=3.11 && conda activate steprecon
# Or point FREECAD_PYTHON at any interpreter, and FREECAD_BIN at the folder holding FreeCAD.so.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${FREECAD_PYTHON:-python3}"
if [ ! -e "$1" ] && [ -e "$HERE/$1" ]; then set -- "$HERE/$1" "${@:2}"; fi
exec "$PY" "$@"
