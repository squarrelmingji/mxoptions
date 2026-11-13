#!/bin/sh
# Ubuntu 24.04 packages the gate, the journeys and the release build need, beyond a minimal system:
#   sh ops/ship/prereqs.sh           install them (as root, or through sudo)
#   sh ops/ship/prereqs.sh --check   name the missing ones; exit 1 when any is missing
# CI runs this same script. Qt itself comes from the PySide6 wheels; these are the system libraries
# those wheels load (measured from a headless start and tools/screenshots.py), plus one font, plus
# pipewire-bin for the PipeWire client config (client.conf); without it the app crashes at start.
set -eu
PACKAGES="
git make gcc libc6-dev linux-libc-dev python3 python3-venv python3-dev ca-certificates
libbrotli1 libdbus-1-3 libdrm2 libegl1 libfontconfig1 libfreetype6 libgl1 libglib2.0-0t64
libgssapi-krb5-2 libpulse0 libx11-6 libxext6 libxkbcommon0 libxrandr2 libstdc++6 libgcc-s1
zlib1g libzstd1 libpipewire-0.3-0t64 pipewire-bin libva-drm2 libva-x11-2 fonts-dejavu-core
"
if [ "${1:-}" = --check ]; then
	missing=
	for p in $PACKAGES; do
		dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | grep -q '^ii' || missing="$missing $p"
	done
	[ -z "$missing" ] && { echo "prerequisites: all present"; exit 0; }
	echo "prerequisites missing:$missing" >&2
	exit 1
fi
[ "$(id -u)" = 0 ] && as= || as=sudo
$as apt-get update -q
# shellcheck disable=SC2086 # one word per package
$as env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $PACKAGES
