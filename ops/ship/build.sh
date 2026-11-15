#!/bin/sh
# The files of a release, from one clean commit:
#   sh ops/ship/build.sh VENV_PYTHON OUT
# OUT gets the wheel, the .deb, BUILDINFO.json (commit, tree, tool versions) and SHA256SUMS. CI runs
# this on every commit; a release publishes the bytes CI made for its commit and is never rebuilt.
set -eu
PY=$1
OUT=$(mkdir -p "$2" && cd "$2" && pwd)
cd "$(dirname "$0")/../.."
[ -z "$(git status --porcelain)" ] || { echo "build.sh: the tree has changes; build from a clean checkout" >&2; exit 1; }
[ -z "$(ls -A "$OUT")" ] || { echo "build.sh: $OUT is not empty" >&2; exit 1; }
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
export SOURCE_DATE_EPOCH PYTHONDONTWRITEBYTECODE=1

"$PY" -m build --wheel --no-isolation --outdir "$OUT"
# The package runs on the distribution's own Python, so its launcher must name that one.
make deb PYTHON=/usr/bin/python3
mv dist/mxoptions_*_all.deb "$OUT/"
rm -rf dist build ./*.egg-info

"$PY" - "$OUT" <<'PY'
import hashlib, json, os, platform, subprocess, sys
out = sys.argv[1]
version = subprocess.run([sys.executable, "-c", "import mxoptions; print(mxoptions.__version__)"],
                         capture_output=True, text=True, check=True).stdout.strip()
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()
freeze = subprocess.run([sys.executable, "-m", "pip", "freeze", "--all"], capture_output=True, text=True).stdout.split()
files = {name: hashlib.sha256(open(os.path.join(out, name), "rb").read()).hexdigest() for name in sorted(os.listdir(out))}
info = {"version": version, "commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
        "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]), "python": platform.python_version(),
        "system": platform.freedesktop_os_release().get("PRETTY_NAME", "") if hasattr(platform, "freedesktop_os_release") else "",
        "tools": freeze, "files": files}
json.dump(info, open(os.path.join(out, "BUILDINFO.json"), "w"), indent=1, sort_keys=True)
PY
(cd "$OUT" && sha256sum -- * > SHA256SUMS && sha256sum -c --quiet SHA256SUMS)

# The package says what the program says it is, and carries the files an installation needs.
deb=$(ls "$OUT"/mxoptions_*_all.deb)
want=$("$PY" -c 'import mxoptions, re; print(re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1~\2\3", mxoptions.__version__) + "-1")')
[ "$(dpkg-deb -f "$deb" Version)" = "$want" ] || { echo "build.sh: .deb version is not $want" >&2; exit 1; }
list=$(dpkg-deb -c "$deb")
for path in ./usr/bin/mxoptions ./usr/lib/mxoptions/mxoptions/app.py ./usr/lib/systemd/user/mxoptions.service \
	./usr/share/applications/mxoptions.desktop ./usr/lib/udev/rules.d/70-mxoptions.rules; do
	echo "$list" | grep -q " $path\$" || { echo "build.sh: .deb lacks $path" >&2; exit 1; }
done
launcher=$(dpkg-deb --fsys-tarfile "$deb" | tar -xOf - ./usr/bin/mxoptions)
echo "$launcher" | grep -q 'exec /usr/bin/python3 -m mxoptions' \
	|| { echo "build.sh: .deb launcher does not use /usr/bin/python3" >&2; exit 1; }
ls -l "$OUT"
