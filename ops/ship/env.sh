#!/bin/sh
# The Python environment for the gate, the journeys and the release build:
#   sh ops/ship/env.sh PYTHON VENV      PYTHON is 3.12 or later with venv; VENV is a new directory
set -eu
"$1" -m venv "$2"
"$2/bin/python" -m pip install -q --disable-pip-version-check -c "$(dirname "$0")/constraints.txt" \
	PySide6 evdev numpy build setuptools
"$2/bin/python" -m pip freeze --all > "$2/freeze.txt"
