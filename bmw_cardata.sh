#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="$SCRIPT_DIR/venv"
PYTHON_SCRIPT="$SCRIPT_DIR/bmw_cardata.py"

if [ ! -d "$VENV_PATH" ]; then
    echo "Fehler: Virtuelles Environment nicht gefunden: $VENV_PATH" >&2
    echo "Erstelle es mit: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt" >&2
    exit 1
fi

if [ ! -f "$PYTHON_SCRIPT" ]; then
    echo "Fehler: Python-Script nicht gefunden: $PYTHON_SCRIPT" >&2
    exit 1
fi

source "$VENV_PATH/bin/activate"

if [ $# -eq 0 ]; then
    exec "$PYTHON_SCRIPT" --help
fi

exec "$PYTHON_SCRIPT" "$@"

