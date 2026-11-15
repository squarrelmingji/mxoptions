"""May this commit be a stable release, given the hardware checks on record?

    python3 ops/ship/hwgate.py check COMMIT [EVIDENCE]   one JSON line; exit 0 stable, 1 not
    python3 ops/ship/hwgate.py digest COMMIT             the digest an evidence entry must carry

Run from a checkout that has COMMIT. Groups come from hardware-gates.json next to this file (a clean
checkout of main tip). A group applies when any of its `present` probes matches COMMIT: a probe is
`glob:regex`, true when a file the glob names (fnmatch; `*` also crosses `/`) holds a regex match
(multiline). The digest covers every file `paths` names, so a hardware result stays valid until one of
those device-side files changes; other changes, such as the version or docs, never void it. Each
`require` check of an applying group needs its newest entry for that group and digest to say `pass`;
each `record` check needs any newest entry. A group that does not apply needs nothing. Any file under
mxoptions/ or data/ that neither `paths` nor `ungated` names makes the commit unclassified: not stable
until hardware-gates.json says what it is. Merging code never waits for any of this. No override.

EVIDENCE (default: the ship state directory) is a JSON list written by whoever records the owner's
results: {"group", "gate", "digest", "commit", "result": "pass"|"fail"|"pending", "date", "note"}.
`note` is required: where the result came from. It is text, never run. No device names, serial numbers
or identifiers go in it. Any other result (for example "absent") counts as no result.
"""
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = os.path.expanduser("~/.local/state/ship/squarrelmingji-mxoptions/hardware-evidence.json")
RESULTS = {"pass", "fail", "pending"}


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def files(commit):
    """{path: "mode type sha"} for every file in the commit."""
    out = {}
    for line in git("ls-tree", "-r", "--full-tree", commit).splitlines():
        meta, path = line.split("\t", 1)
        out[path] = meta
    return out


def matches(path, globs):
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def digest(tree, globs):
    lines = "".join(f"{tree[p]}\t{p}\n" for p in sorted(tree) if matches(p, globs))
    return hashlib.sha256(lines.encode()).hexdigest()


def present(commit, tree, probes, text):
    for probe in probes:
        glob, _, pattern = probe.partition(":")
        for path in sorted(p for p in tree if fnmatch.fnmatchcase(p, glob)):
            if path not in text:
                text[path] = git("show", f"{commit}:{path}")
            if re.search(pattern, text[path], re.M):
                return True
    return False


def check(commit, manifest, evidence):
    commit = git("rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    tree = files(commit)
    every = manifest["paths"] + manifest["ungated"]
    unclassified = [p for p in sorted(tree) if p.startswith(("mxoptions/", "data/")) and not matches(p, every)]
    d = digest(tree, manifest["paths"])
    groups, text = {}, {}
    for group in manifest["groups"]:
        here = present(commit, tree, group["present"], text)
        missing = []
        if here:
            for kind in ("require", "record"):
                for gate in group.get(kind, []):
                    newest = None
                    for e in evidence:
                        if (e.get("group"), e.get("gate"), e.get("digest")) == (group["id"], gate, d) \
                                and e.get("result") in RESULTS and e.get("note"):
                            newest = e
                    if newest is None:
                        missing.append(f"{gate}: no result for this build")
                    elif kind == "require" and newest["result"] != "pass":
                        missing.append(f"{gate}: {newest['result']}")
        groups[group["id"]] = {"present": here, "missing": missing}
    stable = not unclassified and not any(g["missing"] for g in groups.values())
    return {"commit": commit, "digest": d, "stable": stable, "unclassified": unclassified, "groups": groups}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main(argv):
    manifest = load(os.path.join(HERE, "hardware-gates.json"), None)
    if len(argv) >= 2 and argv[0] == "check":
        result = check(argv[1], manifest, load(argv[2] if len(argv) > 2 else EVIDENCE, []))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["stable"] else 1
    if len(argv) == 2 and argv[0] == "digest":
        print(digest(files(argv[1]), manifest["paths"]))
        return 0
    print("\n".join(__doc__.splitlines()[2:4]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
