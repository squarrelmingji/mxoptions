#!/bin/sh
# Critical journeys with no mouse, keyboard or screen:
#   sh ops/ship/journeys.sh VENV_PYTHON [APP_TREE]
# Starts MX Options headless with private settings and drives it through `--call`, the way a script
# or the user service does. APP_TREE defaults to this checkout; a behaviour check runs this driver
# against the parent tree, then the head tree. With MXOPTIONS_BIN set (an installed `mxoptions`),
# J2-J6 drive that copy instead and J7-J8, which need a source tree, are left out.
# Prints one PASS or FAIL line per journey; exits 1 when any fails, 4 when a device is reachable,
# 129, 130 or 143 when stopped by SIGHUP, SIGINT or SIGTERM. Every way out ends the app it started,
# and everything that app started, before the private directory goes away.
set -u
PY=$1
DRIVER=$(cd "$(dirname "$0")" && pwd)
TREE=$(cd "${2:-$DRIVER/../..}" && pwd)
WAIT=${MXJ_START_WAIT:-15}  # seconds a start gets to answer; ops/ship/test_journeys.py shortens it

# Hardware-free means no device can be reached at all, not merely none plugged in.
for node in /dev/hidraw* /dev/uinput /dev/uhid /dev/input/event*; do
	[ -e "$node" ] || continue
	if [ -r "$node" ] || [ -w "$node" ]; then
		echo "REFUSE: $node is reachable; run where no input device can be opened" >&2
		exit 4
	fi
done

# Short on purpose: the app's local socket lives here, and a socket path has at most 107 bytes.
T=$(mktemp -d /tmp/mxj.XXXXXX)
APP=  # process id of the app started last; it leads its own process group. Empty once collected.
reap() {  # kill the app and its process group, then collect it
	[ -n "$APP" ] || return 0
	kill -KILL "-$APP" 2>/dev/null || kill -KILL "$APP" 2>/dev/null
	wait "$APP" 2>/dev/null
	APP=
}
trap 'reap; rm -rf "$T"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
export HOME="$T/home" XDG_CONFIG_HOME="$T/home/.config" XDG_DATA_HOME="$T/home/.local/share" \
	XDG_CACHE_HOME="$T/home/.cache" XDG_RUNTIME_DIR="$T/run" TMPDIR="$T/tmp" \
	QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1
unset DBUS_SESSION_BUS_ADDRESS DISPLAY WAYLAND_DISPLAY MXOPTIONS_LAUNCHER
mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$TMPDIR"
chmod 700 "$XDG_RUNTIME_DIR"
cd "$T"  # never from the caller's directory: `python -m mxoptions` would import a checkout found there

if [ -n "${MXOPTIONS_BIN:-}" ]; then
	MX=$MXOPTIONS_BIN
	VERSION=$(timeout -k 1 10 "$MX" --version | sed 's/^MX Options //')
else
	MX="$PY $TREE/main.py"
	VERSION=$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import mxoptions; print(mxoptions.__version__)' "$TREE")
fi
echo "driver $DRIVER @ $(git -C "$DRIVER" rev-parse HEAD 2>/dev/null || echo not-git); app $TREE; version $VERSION"

fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1: $2"; fails=$((fails + 1)); }
ask() {  # the answer on stdout, only when the call succeeds; exit 124 when it hangs
	t=$1; shift  # its diagnostics (Qt, PipeWire, "not running") stay on stderr, out of the comparisons
	r=$(timeout -k 1 "$t" $MX --call "$@") || return
	printf '%s\n' "$r"
}
call() { ask 5 "$@"; }
start() {  # 0 once it answers; else it is killed and collected before 1 comes back
	setsid $MX --background >>"$T/app.log" 2>&1 &
	APP=$!
	end=$(($(date +%s) + WAIT))
	until [ "$(ask 2 AppVersion)" = "$VERSION" ]; do
		if ! kill -0 "$APP" 2>/dev/null || [ "$(date +%s)" -ge "$end" ]; then reap; return 1; fi
		sleep 0.2
	done
}
stop() {  # SIGTERM, as systemd stops the service; the unit allows 8 seconds
	kill -TERM "$APP"
	n=0
	while kill -0 "$APP" 2>/dev/null; do
		n=$((n + 1)); [ $n -ge 40 ] && { reap; return 1; }
		sleep 0.2
	done
	wait "$APP"; rc=$?
	kill -KILL "-$APP" 2>/dev/null  # anything it left behind in its group
	APP=
	return $rc
}
saved_dpi() {  # the window saves half a second after the last change: wait up to 5 s for it
	n=0
	while :; do
		dpi=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["pointer"]["dpi"])' \
			"$XDG_CONFIG_HOME/mxoptions/config.json" 2>/dev/null)
		[ "$dpi" = "$1" ] || [ $n -ge 25 ] && break
		n=$((n + 1)); sleep 0.2
	done
	echo "$dpi"
}

# J2: it starts headless, answers, and says no mouse is connected.
call AppVersion >/dev/null; rc=$?
if [ $rc -ne 3 ]; then fail J2 "not running should exit 3, got $rc"
elif ! start; then fail J2 "no answer within $WAIT s"
elif [ "$(call ApiVersion)" != 1 ] || [ "$(call Connected)" != false ]; then fail J2 "ApiVersion $(call ApiVersion), Connected $(call Connected)"
else pass J2; fi

if [ -n "$APP" ]; then
	# J3: a setting changed through the interface is saved and survives a restart.
	if [ "$(call Set pointer.dpi 1600)" != true ] || [ "$(call Get pointer.dpi)" != 1600 ]; then fail J3 "Set/Get pointer.dpi"
	elif [ "$(saved_dpi 1600)" != 1600 ]; then fail J3 "config.json has $(saved_dpi 1600)"
	elif ! stop || ! start || [ "$(call Get pointer.dpi)" != 1600 ]; then fail J3 "not kept across a restart"
	else pass J3; fi

	# J5: a second start hands over to the copy already running and leaves it running.
	if [ -n "$APP" ]; then
		timeout -k 2 10 $MX --background >>"$T/app.log" 2>&1; rc=$?
		if [ $rc -ne 0 ]; then fail J5 "second start exited $rc"
		elif ! kill -0 "$APP" 2>/dev/null || [ "$(call AppVersion)" != "$VERSION" ]; then fail J5 "first copy gone"
		else pass J5; fi
	fi

	# J4: errors keep their documented exit codes, and it stops cleanly on SIGTERM.
	call Bogus >/dev/null; rc=$?
	if [ $rc -ne 2 ]; then fail J4 "unknown call should exit 2, got $rc"
	elif [ -z "$APP" ] || ! stop; then fail J4 "did not stop within 8 s with status 0"
	else
		call AppVersion >/dev/null; rc=$?
		if [ $rc -ne 3 ]; then fail J4 "after stop should exit 3, got $rc"; else pass J4; fi
	fi
	reap
else
	fail J3 "skipped: J2 failed"; fail J4 "skipped: J2 failed"; fail J5 "skipped: J2 failed"
fi

# J6: the menu entry is added and removed again.
entry="$XDG_DATA_HOME/applications/mxoptions.desktop"
if ! timeout -k 1 10 $MX --install >/dev/null 2>&1 || ! grep -q '^Exec=' "$entry"; then fail J6 "no menu entry at $entry"
elif ! timeout -k 1 10 $MX --uninstall >/dev/null 2>&1 || [ -e "$entry" ]; then fail J6 "menu entry left behind"
else pass J6; fi

if [ -z "${MXOPTIONS_BIN:-}" ]; then
	# J7: "make install" lays out a copy that runs, and "make uninstall" takes every file away again.
	P="$T/prefix"
	if ! timeout -k 5 300 make -s -C "$TREE" install PREFIX="$P" unitdir="$P/lib/systemd/user" UDEV= PYTHON="$PY" >"$T/install.log" 2>&1; then
		fail J7 "make install failed: $(tail -1 "$T/install.log")"
	elif [ "$(timeout -k 1 10 "$P/bin/mxoptions" --version)" != "MX Options $VERSION" ]; then fail J7 "installed --version"
	else
		saved=$MX; MX="$P/bin/mxoptions"
		if ! start; then fail J7 "installed copy did not answer"
		elif ! stop; then fail J7 "installed copy did not stop"
		elif ! timeout -k 5 120 make -s -C "$TREE" uninstall PREFIX="$P" unitdir="$P/lib/systemd/user" UDEV= >>"$T/install.log" 2>&1; then fail J7 "make uninstall failed"
		elif [ -n "$(find "$P" -type f)" ]; then fail J7 "left behind: $(find "$P" -type f | head -3 | tr '\n' ' ')"
		else pass J7; fi
		MX=$saved
	fi

	# J8: every page of the original settings window still renders (the README pictures' tool).
	if ! timeout -k 5 300 "$PY" "$TREE/tools/screenshots.py" "$T/shots" >"$T/shots.log" 2>&1; then fail J8 "screenshots.py: $(tail -1 "$T/shots.log")"
	else
		missing=
		for page in buttons gestures scroll haptics ring flow device; do
			[ "$(wc -c <"$T/shots/$page.png" 2>/dev/null || echo 0)" -gt 10000 ] || missing="$missing $page"
		done
		if [ -n "$missing" ]; then fail J8 "pages missing or empty:$missing"; else pass J8; fi
	fi
fi

[ $fails -eq 0 ] && { echo "journeys: all passed"; exit 0; }
echo "journeys: $fails failed; app log:"; tail -20 "$T/app.log"
exit 1
