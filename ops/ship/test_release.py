"""Self-check of ops/ship/release.py against a fake GitHub and git. Nothing here reaches the network.

    python3 ops/ship/test_release.py

Every gh and git call goes to World, which keeps releases, tags, CI runs and CI files in memory and records
any deletion of something that is not an unpublished draft. Each publish, point and health case checks the
terminal state and what changed on the fake channel.
"""
import hashlib
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
import release  # noqa: E402
import make_deb  # noqa: E402

R = release.REPO
with open(os.path.join(HERE, "hardware-gates.json")) as f:
    MANIFEST = json.load(f)
C1, C2, C3, SIDE = "a" * 40, "b" * 40, "c" * 40, "d" * 40
STABLE = {"stable": True, "unclassified": [], "groups": {"keyboard": {"present": False, "missing": []}}}
OPEN = {"stable": False, "unclassified": [], "groups": {"mouse-follows-keyboard": {
    "present": True, "missing": ["mouse-follows-keyboard: no result for this build"]}}}


def ci_files(version, commit, tree, tamper=None):
    files = {f"mxoptions-{version}-py3-none-any.whl": b"wheel " + version.encode(),
             f"mxoptions_{release.debian_version(version)}_all.deb": b"deb " + version.encode()}
    info = {"commit": commit, "tree": "x" * 40 if tamper == "meta" else tree, "version": version}
    files["BUILDINFO.json"] = json.dumps(info).encode()
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in sorted(files.items())).encode()
    if tamper == "sum":
        files[f"mxoptions-{version}-py3-none-any.whl"] = b"other bytes"
    if tamper == "extra":
        files["notes.txt"] = b"x"
    return files


class World:
    """A fake gh and git: releases, tags, push CI runs and their files, all in memory."""

    def __init__(self):
        self.main = [C2, C1]  # first-parent history of origin/main, newest first
        self.head, self.dirty = C2, ""
        self.versions = {C1: "0.10.0.post1", C2: "0.10.0.post2", C3: "0.10.0.post3", SIDE: "0.10.0.post9"}
        self.trees = {c: c[0] * 39 + "f" for c in self.versions}
        self.releases, self.tags, self.latest, self.next_id, self.t = {}, {}, None, 100, 0
        self.runs = {c: [[{"databaseId": 7, "status": "completed", "conclusion": "success", "headSha": c,
                           "headBranch": "main", "event": "push"}]] for c in self.versions}
        self.files = {c: ci_files(v, c, self.trees[c]) for c, v in self.versions.items()}
        self.calls, self.violations, self.fail = [], [], set()
        self.journeys_rc, self.deb_version = 0, None
        self.fail_upload = self.ignore_latest = False

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t

    def view(self, r):
        return {k: r[k] for k in ("id", "tag_name", "draft", "prerelease", "name", "published_at", "body")} | {
            "assets": [{"name": n, "size": len(b), "state": "uploaded"} for n, b in sorted(r["files"].items())]}

    def stable_newest(self):
        rs = [r for r in self.releases.values() if not r["draft"] and not r["prerelease"]]
        return max(rs, key=lambda r: r["published_at"])["id"] if rs else None

    def __call__(self, args, timeout=120, env=None):
        self.calls.append(args)
        try:
            out = self.answer(args)
        except LookupError as e:
            return 1, "", f"gh: Not Found (HTTP 404) {e}"
        except RuntimeError as e:
            return 1, "", str(e)
        return 0, out if isinstance(out, str) else json.dumps(out), ""

    def answer(self, a):
        key = " ".join(a)
        for f in self.fail:
            if f in key:
                raise RuntimeError(f"failed: {f}")
        if a[0] == "git":
            return self.git(a[1:])
        if a[:3] == ["gh", "api", "--paginate"]:
            return "".join(json.dumps(self.view(r)) + "\n" for r in sorted(self.releases.values(), key=lambda r: -r["id"]))
        if a[:4] == ["gh", "api", "-X", "PATCH"]:
            return self.patch(int(a[4].rsplit("/", 1)[1]), a[5:])
        if a[:4] == ["gh", "api", "-X", "DELETE"]:
            r = self.releases.pop(int(a[4].rsplit("/", 1)[1]))
            if not r["draft"]:
                self.violations.append(f"deleted published {r['tag_name']}")
            return ""
        if a[:2] == ["gh", "api"]:
            return self.get(a[2].removeprefix(f"repos/{R}/"))
        if a[:3] == ["gh", "run", "list"]:
            c = a[a.index("--commit") + 1]
            states = self.runs[c]
            return states.pop(0) if len(states) > 1 else states[0]
        if a[:3] == ["gh", "run", "download"]:
            c = a[a.index("-n") + 1].removeprefix("release-")
            self.write(a[a.index("-D") + 1], self.files[c])
            return ""
        if a[:3] == ["gh", "release", "create"]:
            return self.create(a)
        if a[:3] == ["gh", "release", "download"]:
            r = next(r for r in self.releases.values() if r["tag_name"] == a[3] and not r["draft"])
            self.write(a[a.index("-D") + 1], r["files"])
            return ""
        if a[1:3] == ["-m", "venv"] or a[1:3] == ["-m", "pip"]:
            return ""
        if a[0] == "sh" and a[1].endswith("journeys.sh"):
            if self.journeys_rc:
                raise RuntimeError("journeys: 1 failed")
            return "journeys: all passed\n"
        if a[:2] == ["dpkg-deb", "-f"]:
            return (self.deb_version or os.path.basename(a[2]).split("_")[1]) + "\n"
        if a[:2] == ["dpkg-deb", "-c"]:
            return "".join(f"-rw-r--r-- root/root 1 2026-01-01 00:00 {p}\n" for p in release.DEB_PATHS)
        raise AssertionError(f"unexpected call {a}")

    def git(self, a):
        if a[0] == "fetch":
            return ""
        if a == ["status", "--porcelain"]:
            return self.dirty
        if a == ["rev-parse", "HEAD"]:
            return self.head
        if a == ["rev-parse", "origin/main"]:
            return self.main[0]
        if a[:2] == ["rev-parse", "--verify"]:
            c = a[2].removesuffix("^{commit}")
            if c not in self.versions:
                raise RuntimeError("fatal: Needed a single revision")
            return c + "\n"
        if a[0] == "rev-parse" and a[1].endswith("^{tree}"):
            return self.trees[a[1].removesuffix("^{tree}")] + "\n"
        if a == ["rev-list", "--first-parent", "origin/main"]:
            return "\n".join(self.main) + "\n"
        if a[0] == "show":
            c, path = a[1].split(":", 1)
            return {"mxoptions/__init__.py": f'__version__ = "{self.versions[c]}"\n',
                    "ops/ship/constraints.txt": "PySide6==6.12.0\n"}[path]
        raise AssertionError(f"unexpected git {a}")

    def get(self, path):
        if path == "releases/latest":
            if self.latest is None:
                raise LookupError(path)
            return self.view(self.releases[self.latest])
        if path.startswith("releases/tags/"):
            for r in self.releases.values():
                if r["tag_name"] == path[len("releases/tags/"):] and not r["draft"]:
                    return self.view(r)
            raise LookupError(path)
        if path.startswith("releases/"):
            r = self.releases.get(int(path.split("/")[1]))
            if r is None:
                raise LookupError(path)
            return self.view(r)
        if path.startswith("git/ref/tags/"):
            tag = path[len("git/ref/tags/"):]
            if tag not in self.tags:
                raise LookupError(path)
            return {"object": {"type": "commit", "sha": self.tags[tag]}}
        if path.startswith("git/commits/"):
            return {"tree": {"sha": self.trees[path[len("git/commits/"):]]}}
        raise AssertionError(f"unexpected api {path}")

    def write(self, folder, files):
        os.makedirs(folder)
        for n, b in files.items():
            with open(os.path.join(folder, n), "wb") as f:
                f.write(b)

    def create(self, a):
        tag, files = a[3], a[a.index("--notes-file") + 2:]
        with open(a[a.index("--notes-file") + 1]) as f:
            body = f.read()
        r = {"id": self.next_id, "tag_name": tag, "draft": True, "prerelease": False, "body": body,
             "name": a[a.index("--title") + 1], "published_at": None, "target": a[a.index("--target") + 1], "files": {}}
        self.next_id += 1
        self.releases[r["id"]] = r
        for path in files:
            if self.fail_upload:
                raise RuntimeError("upload failed: connection reset")
            with open(path, "rb") as f:
                r["files"][os.path.basename(path)] = f.read()
        return ""

    def patch(self, rid, a):
        r = self.releases[rid]
        fields = {a[i + 1].split("=", 1)[0]: a[i + 1].split("=", 1)[1] for i in range(0, len(a), 2)}
        if fields.pop("draft", None) == "false":
            self.t += 1
            r.update(draft=False, published_at=f"2026-11-12T00:{self.t:02d}:00Z")
            self.tags.setdefault(r["tag_name"], r["target"])
        if "prerelease" in fields:
            r["prerelease"] = fields.pop("prerelease") == "true"
        r.update(fields)
        if fields.get("make_latest") == "true" and not self.ignore_latest:
            self.latest = rid
        if self.latest is not None and self.releases[self.latest]["prerelease"]:
            self.latest = self.stable_newest()
        return ""


def fresh(hardware=STABLE):
    w = World()
    release.run, release.sleep, release.clock = w, w.sleep, w.clock
    release.gate = lambda commit: (hardware, MANIFEST)
    return w


def mutations(w):
    return [c for c in w.calls if c[:3] == ["gh", "release", "create"] or c[2:3] == ["-X"]]


# Pure parts.
assert release.version_of('APP = 1\n__version__ = "0.10.0.post1"\n') == "0.10.0.post1"
for bad in ('__version__ = "1.0; rm"\n', "nothing\n", '__version__ = "../x"\n'):
    try:
        release.version_of(bad)
        raise AssertionError(bad)
    except release.Fail:
        pass
for v in ("0.10.0", "0.10.0b3", "0.10.0rc1", "0.10.0.post1", "1.2a4"):
    assert release.debian_version(v) == make_deb.debian_version(v), v
notes = release.preview_notes(OPEN, MANIFEST, C1)
assert notes.splitlines()[0] == "Preview for hardware testing, not a stable release. Hardware checks open:"
assert notes.splitlines()[1] == "mouse-follows-keyboard: " + MANIFEST["checks"]["mouse-follows-keyboard"], notes
assert C1 in notes and "not classified" not in notes
assert "mxoptions/x.py: not classified" in release.preview_notes(dict(OPEN, unclassified=["mxoptions/x.py"]), MANIFEST, C1)
assert release.not_stable(OPEN) == "hardware checks open: mouse-follows-keyboard"

# Read commands on an empty channel.
w = fresh()
assert release.main(["serving"]) == ({"tag": None, "commit": None, "tree": None}, 0)
assert release.main(["inflight"]) == ({"drafts": []}, 0)
assert release.main(["publish"])[1] == 2 and release.main(["publish", C1, "--force"])[1] == 2
assert release.main(["health"])[0]["checks"] == {"release": "no latest release"}


def publish(w, commit, preview=False, want="published"):
    out, rc = release.publish(commit, preview)
    assert out["state"] == want, (want, out)
    assert rc == (0 if want in ("published", "preview") else 1), (out, rc)
    assert not w.violations, w.violations
    return out


# Stable publish: CI files unchanged, then latest.
w = fresh()
w.main, w.head = [C1], C1
w.runs[C1] = [[{"databaseId": 7, "status": "in_progress", "conclusion": "", "headSha": C1, "headBranch": "main",
                "event": "push"}]] * 3 + w.runs[C1]
out = publish(w, C1)
assert out["tag"] == "v0.10.0.post1" and out["id"] == 100 and w.t >= 3 * release.POLL
assert release.serving() == {"tag": "v0.10.0.post1", "commit": C1, "tree": w.trees[C1]}
r = w.releases[100]
assert r["files"] == w.files[C1] and not r["prerelease"] and r["name"] == "MX Options 0.10.0.post1"
assert any(c[:3] == ["gh", "run", "download"] and c[c.index("-n") + 1] == "release-" + C1 for c in w.calls)

# Refusals before anything is created.
cases = {
    "dirty tools checkout": lambda w: setattr(w, "dirty", " M ops/ship/release.py\n"),
    "tools checkout behind main": lambda w: setattr(w, "head", C1),
    "commit off main": lambda w: None,  # SIDE below
    "tag exists": lambda w: w.tags.update({"v0.10.0.post2": C2}),
    "draft in flight": lambda w: w.releases.update({5: {"id": 5, "tag_name": "v9", "draft": True, "prerelease": False,
                                                        "name": "x", "body": "", "published_at": None, "files": {}}}),
    "CI failed": lambda w: w.runs.update({C2: [[dict(w.runs[C2][0][0], conclusion="failure")]]}),
    "CI unavailable": lambda w: w.fail.add("gh run list"),
    "CI run of another branch": lambda w: w.runs.update({C2: [[dict(w.runs[C2][0][0], headBranch="x")]]}),
    "checksum": lambda w: w.files.update({C2: ci_files("0.10.0.post2", C2, w.trees[C2], "sum")}),
    "metadata": lambda w: w.files.update({C2: ci_files("0.10.0.post2", C2, w.trees[C2], "meta")}),
    "extra file": lambda w: w.files.update({C2: ci_files("0.10.0.post2", C2, w.trees[C2], "extra")}),
    "download failed": lambda w: w.fail.add("gh run download"),
}
for name, setup in cases.items():
    w = fresh()
    setup(w)
    before = json.dumps(w.releases, sort_keys=True)
    out = publish(w, SIDE if name == "commit off main" else C2, want="timeout" if name == "CI run of another branch"
                  else "refused")
    assert not mutations(w) and json.dumps(w.releases, sort_keys=True) == before, (name, w.calls)
    assert out["reason"], name

# Not stable: refused without --preview (no CI wait, nothing created); --preview makes a pre-release.
w = fresh(OPEN)
out = publish(w, C2, want="refused")
assert out["reason"] == "not stable: hardware checks open: mouse-follows-keyboard" and out["hardware"] == OPEN
assert not mutations(w) and not any(c[:3] == ["gh", "run", "list"] for c in w.calls)
w = fresh()
publish(w, C1)
release.gate = lambda commit: (OPEN, MANIFEST)
out = publish(w, C2, preview=True, want="preview")
r = w.releases[out["id"]]
assert r["prerelease"] and not r["draft"] and r["name"] == "MX Options 0.10.0.post2 preview"
assert r["body"] == release.preview_notes(OPEN, MANIFEST, C2)
assert release.serving()["tag"] == "v0.10.0.post1"  # never latest
edits = [c for c in w.calls if c[:4] == ["gh", "api", "-X", "PATCH"] and c[4].endswith(f"/{out['id']}")]
assert len(edits) == 1 and {"prerelease=true", "make_latest=false", "draft=false"} <= set(edits[0]), edits

# CI never finishes: timeout, nothing created.
w = fresh()
w.runs[C2] = [[dict(w.runs[C2][0][0], status="queued", conclusion="")]]
out = publish(w, C2, want="timeout")
assert not mutations(w) and w.t >= release.CI_WAIT

# Failures after the draft exists: the draft this run made goes, nothing published is touched.
w = fresh()
publish(w, C1)
w.fail_upload = True
out = publish(w, C2, want="refused")
assert "removed" in out["reason"] and set(w.releases) == {100} and release.serving()["tag"] == "v0.10.0.post1"
w = fresh()
w.fail.add("make_latest=true")
out = publish(w, C2, want="refused")
assert "removed" in out["reason"] and not w.releases and release.serving()["tag"] is None
w = fresh()
w.fail.update({"make_latest=true", "-X DELETE"})
out = publish(w, C2, want="draft-left")
assert w.releases[out["id"]]["draft"] and "draft kept" in out["reason"]
w = fresh()
w.ignore_latest = True  # published, but latest did not move: left for inspection, never deleted
out = publish(w, C2, want="draft-left")
assert not w.releases[out["id"]]["draft"] and "left as it is" in out["reason"]
w = fresh()
w.fail.add("--paginate")  # releases cannot be listed
out, rc = release.publish(C2, False)
assert out["state"] == "refused" and rc == 1 and not mutations(w), out  # fails before create: releases unreadable

# point: rollback, re-deploy, promotion, refusals.
w = fresh()
w.main, w.head = [C1], C1
publish(w, C1)
w.main, w.head = [C2, C1], C2
publish(w, C2)
assert release.point("v0.10.0.post1") == ({"latest": "v0.10.0.post1", "prerelease": ["v0.10.0.post2"]}, 0)
assert release.serving()["commit"] == C1 and w.releases[101]["prerelease"]
assert release.point("v0.10.0.post2") == ({"latest": "v0.10.0.post2", "prerelease": []}, 0)
assert not w.releases[101]["prerelease"] and not w.violations and len(w.releases) == 2
w.main, w.head = [C3, C2, C1], C3
release.gate = lambda commit: (OPEN, MANIFEST)
pre = publish(w, C3, preview=True, want="preview")
out, rc = release.point("v0.10.0.post3")
assert rc == 1 and out["refused"].startswith("not stable") and out["hardware"] == OPEN
release.gate = lambda commit: (STABLE, MANIFEST)
assert release.point("v0.10.0.post3") == ({"latest": "v0.10.0.post3", "prerelease": []}, 0)
r = w.releases[pre["id"]]
assert r["name"] == "MX Options 0.10.0.post3" and r["body"] == release.stable_notes(C3) and not r["prerelease"]
n = len(mutations(w))
w.releases[200] = {"id": 200, "tag_name": "v0.10.0.post4", "draft": True, "prerelease": False, "name": "d", "body": "",
                   "published_at": None, "files": {}}
w.releases[101]["files"].pop("BUILDINFO.json")
for tag, why in (("v0.10.0.post4", "no published release"), ("latest", "not a release tag"),
                 ("v0.10.0.post2", "files are"), ("v0.10.0.post9", "no published release")):
    out, rc = release.point(tag)
    assert rc == 1 and why in out["refused"], (tag, out)
w.releases[101]["files"] = dict(w.files[C2])
w.dirty = "?? x\n"
assert "clean checkout" in release.point("v0.10.0.post2")[0]["refused"]
w.dirty, w.main = "", [C3, C1]  # its commit is no longer on main's first-parent history
w.head = C3
assert "first-parent" in release.point("v0.10.0.post2")[0]["refused"]
assert len(mutations(w)) == n and release.serving()["tag"] == "v0.10.0.post3" and not w.violations

# health: published files, BUILDINFO, wheel journeys, .deb; a missing release is a 404.
w = fresh()
publish(w, C2)
out, rc = release.health()
assert rc == 0 and out == {"tag": "v0.10.0.post2", "pass": True, "checks": dict.fromkeys(
    ("release", "files", "buildinfo", "wheel", "journeys", "deb"), True)}, out
out, rc = release.health("v0.0.0-missing")
assert rc == 1 and out["checks"]["release"].startswith("404"), out
w.journeys_rc = 1
out, rc = release.health("v0.10.0.post2")
assert rc == 1 and out["checks"]["journeys"] == "exit 1: journeys: 1 failed", out
w.journeys_rc, w.deb_version = 0, "0.10.0-1"
assert release.health("v0.10.0.post2")[0]["checks"]["deb"] == "Version 0.10.0-1"
w.deb_version = None
w.releases[100]["files"]["SHA256SUMS"] = b"0  BUILDINFO.json\n"
out, rc = release.health("v0.10.0.post2")
assert rc == 1 and "files" in out["checks"] and out["checks"]["files"] is not True and "wheel" not in out["checks"], out
assert release.health("v0/../x")[0]["checks"]["release"] == "not a tag name"
assert not [c for c in w.calls if c[2:3] == ["-X"] and c[3] != "PATCH" or c[:3] == ["gh", "release", "delete"]]

print("release self-check: OK")
