"""Production releases of squarrelmingji/mxoptions: GitHub Releases and the releases/latest pointer.

    python3 ops/ship/release.py serving                    the latest release: tag, commit, tree
    python3 ops/ship/release.py inflight                   unpublished drafts; exit 10 when any exists
    python3 ops/ship/release.py publish COMMIT [--preview] publish the files CI built for COMMIT
    python3 ops/ship/release.py point TAG                  make a published release latest (rollback, promotion)
    python3 ops/ship/release.py health [TAG]               check a published release (default: latest)

Each call prints one JSON line. Run it from a clean checkout of origin/main tip, never from the commit it
publishes; publish and point refuse otherwise. It needs git and an authenticated gh, and nothing else.

publish refuses before it creates anything when the tag or a release for the version exists, a draft is in
flight, COMMIT is not on the first-parent history of origin/main, or (without --preview) hwgate.py says the
commit is not stable. It then waits for the push CI run of COMMIT, takes that run's release-<COMMIT> files
unchanged, checks them, uploads them as a draft and publishes it: stable as latest, or a preview as a
pre-release that never becomes latest. Terminal states: published, preview, refused, draft-left, timeout.
If a step fails after the draft exists, the draft this run made is deleted when it is still a draft
(refused), else left for inspection (draft-left). Nothing here deletes a published release, a tag or a file.

point TAG makes a published stable-eligible release latest and marks every newer stable release a
pre-release. The same bytes stay; nothing is rebuilt. It refuses drafts, releases missing files, commits off
origin/main and commits hwgate.py does not call stable.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import hwgate  # noqa: E402

REPO = "squarrelmingji/mxoptions"
CI_WAIT = 30 * 60  # seconds publish waits for the push CI run
POLL = 30
VERSION = re.compile(r"\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?")
TAG = re.compile(r"[A-Za-z0-9._-]+")
DEB_PATHS = ("./usr/bin/mxoptions", "./usr/lib/mxoptions/mxoptions/app.py", "./usr/lib/systemd/user/mxoptions.service",
             "./usr/share/applications/mxoptions.desktop", "./usr/lib/udev/rules.d/70-mxoptions.rules")
sleep, clock = time.sleep, time.monotonic  # test_release.py replaces these, and run()


class Fail(Exception):
    pass


def run(args, timeout=120, env=None):
    """(exit status, stdout, stderr); 124 when it timed out, 127 when the program is missing."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=ROOT, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"
    except FileNotFoundError:
        return 127, "", f"{args[0]}: not found"


def last(text):
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


def must(args, what, timeout=120):
    rc, out, err = run(args, timeout)
    if rc:
        raise Fail(f"{what}: {last(err) or f'exit {rc}'}")
    return out


def api(path, *extra, missing=False):
    """GitHub REST read; None for a 404 when missing=True."""
    rc, out, err = run(["gh", "api", *extra, path])
    if rc and missing and "HTTP 404" in err:
        return None
    if rc:
        raise Fail(f"gh api {path}: {last(err) or f'exit {rc}'}")
    return json.loads(out)


def patch(rid, what, **fields):
    args = ["gh", "api", "-X", "PATCH", f"repos/{REPO}/releases/{rid}"]
    for k, v in fields.items():
        args += ["-F", f"{k}={json.dumps(v)}"] if isinstance(v, bool) else ["-f", f"{k}={v}"]
    must(args, what)


def git(*args):
    return must(["git", *args], "git " + args[0]).strip()


# Pure parts (test_release.py checks these directly).

def version_of(init_text):
    m = re.search(r'^__version__ = "([^"]+)"$', init_text, re.M)
    if not m or not VERSION.fullmatch(m.group(1)):
        raise Fail("mxoptions/__init__.py has no plain __version__")
    return m.group(1)


def debian_version(version):  # same rule as tools/make_deb.py; test_release.py compares the two
    return re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1~\2\3", version) + "-1"


def assets(version):
    return {f"mxoptions-{version}-py3-none-any.whl", f"mxoptions_{debian_version(version)}_all.deb",
            "BUILDINFO.json", "SHA256SUMS"}


def sums_errors(folder, version):
    """What is wrong with the release files in folder: names, SHA256SUMS, nothing extra or missing."""
    names = set(os.listdir(folder))
    if names != assets(version):
        return [f"files are {sorted(names)}, want {sorted(assets(version))}"]
    listed = {}
    with open(os.path.join(folder, "SHA256SUMS")) as f:
        for line in f:
            digest, _, name = line.rstrip("\n").partition("  ")
            listed[name] = digest
    errors = [] if set(listed) == names - {"SHA256SUMS"} else [f"SHA256SUMS lists {sorted(listed)}"]
    for name, want in sorted(listed.items()):
        path = os.path.join(folder, name)
        if name in names and name != "SHA256SUMS":
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != want:
                    errors.append(f"{name}: checksum differs")
    return errors


def buildinfo_errors(folder, commit, tree, version):
    try:
        with open(os.path.join(folder, "BUILDINFO.json")) as f:
            info = json.load(f)
    except (OSError, ValueError) as e:
        return [f"BUILDINFO.json: {e.__class__.__name__}"]
    want = {"commit": commit, "tree": tree, "version": version}
    return [f"BUILDINFO.json {k} is {info.get(k)!r}, want {v!r}" for k, v in want.items() if info.get(k) != v]


def open_checks(hardware, manifest):
    """[(name, check text)] for each open hardware check, in manifest order, once each."""
    out = []
    for group in hardware.get("groups", {}).values():
        for m in group["missing"]:
            name = m.split(":")[0]
            if name not in (n for n, _ in out):
                out.append((name, manifest["checks"].get(name, "")))
    return out


def preview_notes(hardware, manifest, commit):
    lines = ["Preview for hardware testing, not a stable release. Hardware checks open:"]
    lines += [f"{name}: {text}" for name, text in open_checks(hardware, manifest)]
    lines += [f"{path}: not classified in ops/ship/hardware-gates.json" for path in hardware.get("unclassified", [])]
    return "\n".join(lines) + "\n\n" + stable_notes(commit)


def stable_notes(commit):
    return (f"Built by CI from commit {commit}. SHA256SUMS lists the checksums of the files; BUILDINFO.json names "
            "the commit, the tree and the tool versions.\n")


def not_stable(hardware):
    names = [m.split(":")[0] for g in hardware["groups"].values() for m in g["missing"]]
    parts = [f"hardware checks open: {', '.join(names)}"] if names else []
    parts += [f"unclassified: {', '.join(hardware['unclassified'])}"] if hardware["unclassified"] else []
    return "; ".join(parts)


# Reads.

def gate(commit):
    with open(os.path.join(HERE, "hardware-gates.json")) as f:
        manifest = json.load(f)
    return hwgate.check(commit, manifest, hwgate.load(hwgate.EVIDENCE, [])), manifest


def tools_checkout():
    """Raise unless this is a clean checkout of origin/main tip (after fetching it)."""
    must(["git", "fetch", "-q", "origin", "main"], "git fetch origin main")
    if git("status", "--porcelain") or git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise Fail("run from a clean checkout of origin/main tip")


def on_main(commit):
    return commit in git("rev-list", "--first-parent", "origin/main").split()


def tag_commit(tag):
    ref = api(f"repos/{REPO}/git/ref/tags/{tag}")
    if ref["object"]["type"] != "commit":
        raise Fail(f"tag {tag} does not name a commit")
    sha = ref["object"]["sha"]
    return sha, api(f"repos/{REPO}/git/commits/{sha}")["tree"]["sha"]


def releases():
    rc, out, err = run(["gh", "api", "--paginate", f"repos/{REPO}/releases", "--jq", ".[]"])
    if rc:
        raise Fail(f"gh api releases: {last(err) or f'exit {rc}'}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def serving():
    latest = api(f"repos/{REPO}/releases/latest", missing=True)
    if latest is None:
        return {"tag": None, "commit": None, "tree": None}
    commit, tree = tag_commit(latest["tag_name"])
    return {"tag": latest["tag_name"], "commit": commit, "tree": tree}


def drafts():
    return [{"id": r["id"], "tag": r["tag_name"]} for r in releases() if r["draft"]]


def wait_ci(commit):
    """The id of the successful push CI run of commit; Fail when it failed or is unavailable, None on timeout."""
    end = clock() + CI_WAIT
    while True:
        rc, out, err = run(["gh", "run", "list", "--repo", REPO, "--workflow", "ci.yml", "--event", "push",
                            "--commit", commit, "--json", "databaseId,status,conclusion,headSha,headBranch,event"])
        if rc:
            raise Fail(f"CI unavailable: {last(err) or f'exit {rc}'}")
        runs = [r for r in json.loads(out) if r["headSha"] == commit and r["event"] == "push"
                and r["headBranch"] == "main"]
        if runs and runs[0]["status"] == "completed":  # newest first
            if runs[0]["conclusion"] != "success":
                raise Fail(f"push CI run {runs[0]['databaseId']} ended {runs[0]['conclusion']}")
            return runs[0]["databaseId"]
        if clock() >= end:
            return None
        sleep(POLL)


# Commands.

def publish(commit, preview):
    out = {"state": "refused", "tag": "", "id": None, "reason": "", "hardware": {}}
    try:
        tools_checkout()
        commit = git("rev-parse", "--verify", f"{commit}^{{commit}}")
        if not on_main(commit):
            raise Fail(f"{commit} is not on the first-parent history of origin/main")
        version = version_of(git("show", f"{commit}:mxoptions/__init__.py"))
        tag = out["tag"] = "v" + version
        if api(f"repos/{REPO}/git/ref/tags/{tag}", missing=True) or any(r["tag_name"] == tag for r in releases()):
            raise Fail(f"tag or release {tag} exists")
        if drafts():
            raise Fail("a draft release is in flight; see inflight")
        hardware, manifest = gate(commit)
        out["hardware"] = hardware
        if not hardware["stable"] and not preview:
            raise Fail("not stable: " + not_stable(hardware))
        run_id = wait_ci(commit)
        if run_id is None:
            out.update(state="timeout", reason=f"push CI run of {commit} not finished within {CI_WAIT // 60} min")
            return out, 1
        before = serving()
    except Fail as e:
        out["reason"] = str(e)
        return out, 1
    folder = tempfile.mkdtemp(prefix="mxr.")
    try:
        files = os.path.join(folder, "files")
        try:
            must(["gh", "run", "download", str(run_id), "--repo", REPO, "-n", f"release-{commit}", "-D", files],
                 "CI files", timeout=900)
            errors = sums_errors(files, version) or buildinfo_errors(files, commit, git("rev-parse", f"{commit}^{{tree}}"),
                                                                     version)
            if errors:
                raise Fail("; ".join(errors))
        except Fail as e:
            out["reason"] = str(e)
            return out, 1
        notes = os.path.join(folder, "notes.md")
        with open(notes, "w") as f:
            f.write(preview_notes(hardware, manifest, commit) if preview else stable_notes(commit))
        title = f"MX Options {version}" + (" preview" if preview else "")
        sizes = {n: os.path.getsize(os.path.join(files, n)) for n in os.listdir(files)}
        rc, _, err = run(["gh", "release", "create", tag, "--repo", REPO, "--draft", "--target", commit, "--title", title,
                          "--notes-file", notes, *sorted(os.path.join(files, n) for n in sizes)], timeout=900)
        try:
            mine = [d for d in drafts() if d["tag"] == tag]  # inflight was empty before: a draft with this tag is ours
            if rc or len(mine) != 1:
                raise Fail(f"draft upload: {last(err) or f'{len(mine)} drafts for {tag}'}")
            rid = out["id"] = mine[0]["id"]
            got = {a["name"]: a["size"] for a in api(f"repos/{REPO}/releases/{rid}")["assets"] if a["state"] == "uploaded"}
            if got != sizes:
                raise Fail(f"draft files read back as {sorted(got.items())}")
            if preview:
                patch(rid, "publish preview", draft=False, prerelease=True, make_latest="false")
                r = api(f"repos/{REPO}/releases/{rid}")
                if r["draft"] or not r["prerelease"] or serving() != before:
                    raise Fail("preview read-back: not a pre-release, or latest changed")
                out.update(state="preview", reason=f"pre-release {tag}; latest stays {before['tag']}")
            else:
                patch(rid, "publish", draft=False, prerelease=False, make_latest="true")
                now = serving()
                if (now["tag"], now["commit"]) != (tag, commit):
                    raise Fail(f"read-back: latest is {now['tag']} at {now['commit']}")
                out.update(state="published", reason=f"{tag} is latest")
            return out, 0
        except Exception as e:  # any failure once a draft may exist ends in a terminal state, never a traceback
            removed, what = discard_draft(tag, out["id"])
            out.update(state="refused" if removed else "draft-left", reason=f"{e}; {what}")
            return out, 1
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def discard_draft(tag, rid):
    """(removed, what happened): delete the draft this run made, only while it is an unpublished draft of tag."""
    try:
        if rid is None:
            mine = [d for d in drafts() if d["tag"] == tag]
            if len(mine) != 1:
                return len(mine) == 0, f"{len(mine)} drafts for {tag}; nothing deleted"
            rid = mine[0]["id"]
        r = api(f"repos/{REPO}/releases/{rid}", missing=True)
        if r is None:
            return True, "no draft left"
        if not r["draft"] or r["tag_name"] != tag:
            return False, f"release {rid} is not an unpublished draft of {tag}; left as it is"
        must(["gh", "api", "-X", "DELETE", f"repos/{REPO}/releases/{rid}"], "delete draft")
        return True, f"draft {rid} removed"
    except Exception as e:
        return False, f"draft kept: {e}"


def point(tag):
    hardware = {}
    try:
        if not TAG.fullmatch(tag) or not VERSION.fullmatch(tag[1:]) or tag[0] != "v":
            raise Fail(f"{tag!r} is not a release tag")
        tools_checkout()
        target = api(f"repos/{REPO}/releases/tags/{tag}", missing=True)  # published releases only
        if target is None or target["draft"]:
            raise Fail(f"no published release {tag}")
        got = {a["name"] for a in target["assets"] if a["state"] == "uploaded" and a["size"] > 0}
        if got != assets(tag[1:]):
            raise Fail(f"{tag} files are {sorted(got)}")
        commit, _ = tag_commit(tag)
        if not on_main(commit):
            raise Fail(f"{commit} is not on the first-parent history of origin/main")
        hardware, _ = gate(commit)
        if not hardware["stable"]:
            raise Fail("not stable: " + not_stable(hardware))
        fields = {"prerelease": False, "make_latest": "true"}
        if target["name"].endswith(" preview"):  # promotion: the stable title and notes
            fields.update(name=f"MX Options {tag[1:]}", body=stable_notes(commit))
        patch(target["id"], f"point {tag}", **fields)
        newer = [r for r in releases() if not r["draft"] and not r["prerelease"] and r["id"] != target["id"]
                 and (r["published_at"] or "") > (target["published_at"] or "")]
        for r in newer:
            patch(r["id"], f"pre-release {r['tag_name']}", prerelease=True, make_latest="false")
        latest = serving()["tag"]
        if latest != tag:
            raise Fail(f"read-back: latest is {latest}")
        return {"latest": latest, "prerelease": [r["tag_name"] for r in newer]}, 0
    except Fail as e:
        return {"refused": str(e), "hardware": hardware}, 1


def health(tag=None):
    checks = {}
    out = {"tag": tag, "pass": False, "checks": checks}
    folder = tempfile.mkdtemp(prefix="mxh.")
    step = "release"
    try:
        if tag is None:
            tag = out["tag"] = serving()["tag"]
            if tag is None:
                raise Fail("no latest release")
        if not TAG.fullmatch(tag):
            raise Fail("not a tag name")
        if api(f"repos/{REPO}/releases/tags/{tag}", missing=True) is None:
            raise Fail(f"404: no published release {tag}")
        version = tag[1:]
        commit, tree = tag_commit(tag)
        checks[step], step = True, "files"
        files = os.path.join(folder, "files")
        must(["gh", "release", "download", tag, "--repo", REPO, "-D", files], "download", timeout=900)
        errors = sums_errors(files, version)
        if errors:
            raise Fail("; ".join(errors))
        checks[step], step = True, "buildinfo"
        errors = buildinfo_errors(files, commit, tree, version)
        if errors:
            raise Fail("; ".join(errors))
        checks[step], step = True, "wheel"
        must(["git", "fetch", "-q", "origin", "main"], "git fetch origin main")
        constraints = os.path.join(folder, "constraints.txt")
        with open(constraints, "w") as f:
            f.write(git("show", f"{commit}:ops/ship/constraints.txt") + "\n")
        venv = os.path.join(folder, "venv")
        must([sys.executable, "-m", "venv", venv], "venv")
        must([os.path.join(venv, "bin", "python"), "-m", "pip", "install", "-q", "--disable-pip-version-check",
              "-c", constraints, os.path.join(files, f"mxoptions-{version}-py3-none-any.whl")], "wheel install",
             timeout=900)
        checks[step], step = True, "journeys"
        env = dict(os.environ, MXOPTIONS_BIN=os.path.join(venv, "bin", "mxoptions"))
        rc, text, err = run(["sh", os.path.join(HERE, "journeys.sh"), os.path.join(venv, "bin", "python")], 300, env)
        if rc:
            raise Fail(f"exit {rc}: {last(text) or last(err)}")
        checks[step], step = True, "deb"
        deb = os.path.join(files, f"mxoptions_{debian_version(version)}_all.deb")
        have = must(["dpkg-deb", "-f", deb, "Version"], "dpkg-deb").strip()
        if have != debian_version(version):
            raise Fail(f"Version {have}")
        listing = {line.split()[-1] for line in must(["dpkg-deb", "-c", deb], "dpkg-deb").splitlines() if line.split()}
        lacks = [p for p in DEB_PATHS if p not in listing]
        if lacks:
            raise Fail(f"lacks {', '.join(lacks)}")
        checks[step] = True
        out["pass"] = True
    except Fail as e:
        checks[step] = str(e)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return out, 0 if out["pass"] else 1


def main(argv):
    try:
        if argv == ["serving"]:
            return serving(), 0
        if argv == ["inflight"]:
            d = drafts()
            return {"drafts": d}, 10 if d else 0
        if argv[:1] == ["publish"] and len(argv) in (2, 3) and argv[2:] in ([], ["--preview"]):
            return publish(argv[1], argv[2:] == ["--preview"])
        if argv[:1] == ["point"] and len(argv) == 2:
            return point(argv[1])
        if argv[:1] == ["health"] and len(argv) <= 2:
            return health(argv[1] if len(argv) == 2 else None)
    except Fail as e:
        return {"error": str(e)}, 2
    return {"error": "usage: " + " | ".join(line.split("  ")[0].strip() for line in __doc__.splitlines()[2:7])}, 2


if __name__ == "__main__":
    result, status = main(sys.argv[1:])
    print(json.dumps(result, sort_keys=True))
    sys.exit(status)
