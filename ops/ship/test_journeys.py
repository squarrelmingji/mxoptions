"""Failure checks for ops/ship/journeys.sh, with a fake app and no device.

    python3 ops/ship/test_journeys.py [DRIVER]

The fake app hangs at start (ignoring SIGTERM, with a child of its own), exits at once, or answers
nothing. Each run must end in bounded time, leave no process of the fake app behind, and remove its
private directory, also when the driver itself is stopped by SIGINT, SIGTERM or SIGHUP. A fake that
answers on stdout while writing diagnostics on stderr, as Qt and PipeWire do, must be ready; one with
a wrong answer on stdout, or the right answer but a failing status, must not.
"""
import glob
import os
import signal
import subprocess
import sys
import tempfile
import time

DRIVER = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "journeys.sh"))
MARK = str(900000 + os.getpid() % 99999)  # sleep argument that tags every fake-app process

FAKE = r"""#!/bin/sh
case "$1" in
--version) pwd >"$FAKE_DIR/cwd"; echo "MX Options 9.9.9" ;;
--background)
	[ "$FAKE_MODE" = exit ] && exit 1
	echo "fake diagnostic: starting" >&2
	[ -e "$FAKE_DIR/started" ] && exit 0  # a second start hands over
	touch "$FAKE_DIR/started"
	[ "$FAKE_MODE" = noisy ] || trap '' TERM
	sleep "$FAKE_MARK" &
	sleep "$FAKE_MARK"
	;;
--call)
	[ -e "$FAKE_DIR/started" ] || exit 3
	[ "$FAKE_MODE" = mute ] && exec sleep "$FAKE_MARK"
	case "$FAKE_MODE" in noisy | wrong | refuse) ;; *) exit 3 ;; esac
	echo "fake diagnostic: $2" >&2
	case "$2" in AppVersion) a=9.9.9 ;; ApiVersion) a=1 ;; Connected) a=false ;; *) exit 2 ;; esac
	[ "$FAKE_MODE" = wrong ] && { echo "$a" >&2; a=0.0.0; }  # the right answer, but not on stdout
	echo "$a"
	[ "$FAKE_MODE" = refuse ] && exit 2
	exit 0
	;;
*) exit 1 ;;
esac
"""


def reachable_device():
    for node in glob.glob("/dev/hidraw*") + ["/dev/uinput", "/dev/uhid"] + glob.glob("/dev/input/event*"):
        if os.path.exists(node) and (os.access(node, os.R_OK) or os.access(node, os.W_OK)):
            return node
    return None


def fake_processes():
    found = []
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if args[:2] == [b"sleep", MARK.encode()]:
            found.append(cmdline.split("/")[2])
    return found


def run(mode, signum=None):
    """Run the driver against the fake app: (process, seconds, new private dirs, output, fake folder)."""
    folder = tempfile.mkdtemp(prefix="mxf.", dir="/tmp")
    fake = os.path.join(folder, "mxoptions")
    with open(fake, "w") as f:
        f.write(FAKE)
    os.chmod(fake, 0o755)
    env = dict(os.environ, MXOPTIONS_BIN=fake, MXJ_START_WAIT="3", FAKE_MODE=mode, FAKE_DIR=folder, FAKE_MARK=MARK)
    log = os.path.join(folder, "driver.log")
    before = set(glob.glob("/tmp/mxj.*"))
    began = time.monotonic()
    # Own session, so a broken driver and what it left can be killed as one group. Python leaves
    # SIGINT at its default in the child, so the driver can trap it. Output to a file, not a pipe:
    # a stray process holding a pipe open would hang this check.
    with open(log, "w") as out:
        driver = subprocess.Popen(["sh", DRIVER, sys.executable], env=env, stdout=out, stderr=subprocess.STDOUT,
                                  start_new_session=True)
    if signum:
        while not os.path.exists(os.path.join(folder, "started")) and time.monotonic() - began < 5:
            time.sleep(0.05)
        driver.send_signal(signum)
    try:
        driver.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(driver.pid, signal.SIGKILL)
        driver.wait()
    took = time.monotonic() - began
    time.sleep(0.3)  # let a killed group disappear from /proc
    left = sorted(set(glob.glob("/tmp/mxj.*")) - before)
    with open(log) as f:
        text = f.read()
    return driver, took, left, text, folder


def check(name, mode, signum, status, bound, says=()):
    driver, took, left, out, folder = run(mode, signum)
    rc = driver.returncode
    try:
        with open(os.path.join(folder, "cwd")) as f:
            cwd = f.read().strip()
    except OSError:
        cwd = None
    orphans = fake_processes()
    try:  # never leave our own mess behind, even when the check fails
        os.killpg(driver.pid, signal.SIGKILL)
    except OSError:
        pass
    for pid in orphans:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
    subprocess.run(["rm", "-rf", folder, *left])
    problems = []
    if rc != status:
        problems.append(f"exit {rc}, want {status}")
    if took > bound:
        problems.append(f"took {took:.1f} s, bound {bound} s")
    if orphans:
        problems.append(f"fake app still running: {orphans}")
    if left:
        problems.append(f"private directory left: {left}")
    problems += [f"no {line!r} line" for line in says if line not in out.splitlines()]
    if cwd is not None and not cwd.startswith("/tmp/mxj."):  # the app must not run in the caller's directory
        problems.append(f"app ran in {cwd}")
    print(("PASS " if not problems else "FAIL ") + f"{name} ({took:.1f} s)" + "".join("; " + p for p in problems))
    if problems:
        print("  driver said: " + " | ".join(out.strip().splitlines()[-6:]))
    return not problems


def main():
    node = reachable_device()
    if node:
        print(f"SKIP: {node} is reachable; the driver refuses to run here")
        return 0
    results = [
        check("hung start is killed with its child", "hang", None, 1, 12),
        check("early exit is collected", "exit", None, 1, 2.5),
        check("silent --call is bounded", "mute", None, 1, 12),
        check("SIGINT during start", "hang", signal.SIGINT, 130, 6),
        check("SIGTERM during start", "hang", signal.SIGTERM, 143, 6),
        check("SIGHUP during start", "hang", signal.SIGHUP, 129, 6),
        check("stderr diagnostics leave answers exact", "noisy", None, 1, 6, ("PASS J2", "PASS J5")),
        check("wrong answer on stdout fails", "wrong", None, 1, 8, ("FAIL J2: no answer within 3 s",)),
        check("failing call status fails", "refuse", None, 1, 8, ("FAIL J2: no answer within 3 s",)),
    ]
    print(f"journeys driver failure checks: {sum(results)} of {len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
