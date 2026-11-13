#!/bin/sh
# The local gate: compile, translations, every unit test, and the ship tooling's own self-checks,
# with no screen and private settings.
#   sh ops/ship/gate.sh VENV_PYTHON
set -eu
cd "$(dirname "$0")/../.."
T=$(mktemp -d /tmp/mxg.XXXXXX)
trap 'rm -rf "$T"' EXIT
export HOME="$T" XDG_CONFIG_HOME="$T/config" XDG_DATA_HOME="$T/data" XDG_CACHE_HOME="$T/cache" \
	XDG_RUNTIME_DIR="$T" TMPDIR="$T" QT_QPA_PLATFORM=offscreen
unset DBUS_SESSION_BUS_ADDRESS DISPLAY WAYLAND_DISPLAY
make PYTHON="$1" check
"$1" ops/ship/test_hwgate.py
"$1" ops/ship/test_journeys.py
"$1" ops/ship/test_release.py
