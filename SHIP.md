# Ship profile

Policy: the owner's PR review, land and deploy policy. Procedure: skill `ship-workflow`. Every
field filled; `none` plus reason where it does not apply.

## Level

Level: 0. No proof has run yet; see Pending proofs and Proof log.

Level 0 lets agents verify and babysit pull requests, never merge or publish. Level 1 (agents
merge) needs every proof under Pending proofs to pass and be recorded first. Even at Level 1,
until the Proof log shows the Level 2 and 3 proofs, a release happens only as a proof run the
owner's standing authorization covers.

## Repository

- Repository: `squarrelmingji/mxoptions`, a fork of `nictonus/mxoptions`. Pull requests, issues
  and comments go to this fork only (`gh ... --repo squarrelmingji/mxoptions`).
- Target branch: `main`
- Merge method: squash. `merge` for: syncs from `nictonus/mxoptions`, whose ancestry must survive.
- Author lanes allowed for autopilot: Claude Code, OpenCode. Codex-authored PRs stop at
  merge-ready.

## Environments

| Name       | Deploys from | URL | Pattern | Trigger | Waits for CI |
| ---------- | ------------ | --- | ------- | ------- | ------------ |
| production | a commit on `main`'s first-parent history, older than `main` tip | `https://github.com/squarrelmingji/mxoptions/releases/latest` | A agent-run | `python3 ops/ship/release.py publish <commit>` from a clean checkout of `main` tip | yes: `release.py` refuses unless the push CI run on that commit succeeded, and publishes that run's `release-<commit>` files unchanged |

Production is what a new installation downloads: the GitHub Releases of this fork and the
`releases/latest` pointer. It proves the published files, never a device. Copies already
installed on people's computers change only when their owners reinstall. No staging.
Production-only: PR-head behaviour check is the pre-deploy proof. A git bundle or a CI artifact
handed to a tester is a preview, not a deploy.

Promotion staging to production: none: no staging.

## Release eligibility

Three different questions; each has its own evidence.

- **Merge** (Level 1 and up): required checks, the reviewer's gate and behaviour check, a passing
  verdict. All synthetic. Hardware results never block a merge, so feature work goes on while
  hardware checks wait.
- **Stable release** (`publish` without `--preview`, and every `point`): everything a merge
  needs, plus `python3 ops/ship/hwgate.py check <commit>` exit 0. `ops/ship/hardware-gates.json`
  names, per device capability, the hardware checks a stable release needs and the device-side
  files those results cover. A result stays valid until one of those files changes; other changes
  never void it. A capability not yet in the code needs nothing. A file under `mxoptions/` or
  `data/` that the list does not classify blocks stable until a reviewed PR classifies it.
  Results live outside the repository (`$S/hardware-evidence.json`), only as the owner reports
  them, with no device identifiers. No override exists.
- **Preview** (`publish <commit> --preview`): a GitHub pre-release, never `latest`, titled and
  noted as a preview with the hardware checks still open. For the owner's own hardware tests.
  It claims no stable readiness. `point <tag>` makes it stable later only when `hwgate.py check`
  passes for its commit; the same bytes, never rebuilt.

## Required checks

Exact check names that must be `SUCCESS` on the PR head, from GitHub Actions (app id 15368):

- `gate`
- `journeys`

## Run and verify the app

- Prerequisites: Ubuntu 24.04 (the supported setup; other systems need their own equivalents and
  do not count for a reproduction check); `sudo` or root for the one install command; network to
  `archive.ubuntu.com`, `github.com` and `pypi.org`. Packages: `ops/ship/prereqs.sh` lists every
  one (build tools, Ubuntu's `python3` 3.12 with `venv` and headers, and the system libraries the
  PySide6 wheels load, measured from a headless start and from `tools/screenshots.py`). Python
  packages: exact pins in `ops/ship/constraints.txt`. No accounts, secrets or environment
  variables. No device access: the journeys refuse to start (exit 4) when any `/dev/hidraw*`,
  `/dev/uinput`, `/dev/uhid` or `/dev/input/event*` can be opened.
- Setup, in order (CI runs steps 2 and 3 word for word; its checkout is step 1):
  1. `sudo apt-get update && sudo apt-get install -y git ca-certificates`, then clone and enter the
     checkout.
  2. `sh ops/ship/prereqs.sh`, then `sh ops/ship/prereqs.sh --check`: prints
     `prerequisites: all present`, else names what is missing and exits 1.
  3. `sh ops/ship/env.sh /usr/bin/python3 "$V"`, where `V` is a new directory.
- Install: step 3 above.
- Local gate: `sh ops/ship/gate.sh "$V/bin/python"`, then `git diff --check origin/main...HEAD`.
  The gate runs `make check` and the ship tooling's self-checks (`ops/ship/test_*.py`).
- Launch locally: `QT_QPA_PLATFORM=offscreen "$V/bin/python" main.py --background`; ready when
  `"$V/bin/python" main.py --call AppVersion` prints the version.
- Behaviour check: `sh ops/ship/journeys.sh "$V/bin/python" <tree>` from the head checkout, once
  with the parent tree and once with the head tree. Changed features without a journey: drive them
  with `main.py --call` (docs/API.md) or `tools/screenshots.py`, and say so in the result.
- Driver: `ops/ship/journeys.sh`; revision: the PR revision for app checks, a clean checkout of
  `main` tip for ship tooling. The unit tests (`make check`) count as driver too.
- Driver failure handling: every app start runs in its own process group. A start that does not
  answer within 15 s (polled with 2 s calls), or whose app exits first, is killed with its group
  and collected before the journey fails. Every other `--call` gives up after 5 s (exit 124),
  every other app command after 10 s,
  `make install` and `tools/screenshots.py` after 300 s. SIGINT, SIGTERM or SIGHUP end the run
  with 130, 143 or 129, after the app is killed and collected and the private directory removed.
  `ops/ship/test_journeys.py` proves this with a fake app on every gate run.
- Reset: none needed: every gate and journey run makes a new `mktemp -d /tmp/mx?.XXXXXX` home,
  settings and runtime directory and removes it at exit, on every way out.
- Evidence to capture: the `Ran N tests` and `OK` lines, the self-check lines, every `PASS`/`FAIL`
  journey line with the driver revision line, and the CI run URL.
- Cleanup: `rm -rf "$V"`; `git status --porcelain` must stay empty (build output is ignored).

### Critical journeys

Run by `sh ops/ship/journeys.sh` except J1 and J9. No mouse, keyboard or screen.

| ID   | Journey                         | Steps or command                                                  | Expected result |
| ---- | ------------------------------- | ----------------------------------------------------------------- | --------------- |
| `J1` | Unit tests, translations, tooling | `sh ops/ship/gate.sh "$V/bin/python"`                           | `OK`, every self-check passes, exit 0 |
| `J2` | Starts headless and answers     | `--background`, then `--call AppVersion`, `ApiVersion`, `Connected` | version within 15 s, `1`, `false`; exit 3 before start |
| `J3` | A setting is saved and kept     | `--call Set pointer.dpi 1600`, `Get`, settings file, restart, `Get` | `true`, `1600` each time |
| `J4` | Error codes and clean stop      | `--call Bogus`, SIGTERM, `--call AppVersion`                       | exit 2; stops within 8 s with 0; then exit 3 |
| `J5` | One copy per user               | second `--background` while running                               | exits 0; first copy still answers |
| `J6` | Menu entry                      | `--install`, `--uninstall`                                        | desktop file written, then removed |
| `J7` | Installed layout                | `make install PREFIX=<temp> UDEV=`, run it, `make uninstall`       | installed copy answers; no file left |
| `J8` | Original settings window draws  | `tools/screenshots.py <temp>`                                     | buttons, gestures, scroll, haptics, ring, flow, device pages, each over 10 kB |
| `J9` | Release files                   | CI `journeys`: `ops/ship/build.sh`, wheel into a new venv, J2-J6 on it | wheel, .deb, BUILDINFO.json, SHA256SUMS verified |

Device journeys (buttons, gestures, haptics, wheel, Flow, keyboard) need real hardware. Only the
owner runs them, on the owner's own computers; agents never do. They never gate a merge. They
gate a stable release through `ops/ship/hardware-gates.json` (see Release eligibility).

## Deploy

- Deploy command per environment: production `python3 ops/ship/release.py publish <commit>`
  (stable) or `... publish <commit> --preview`, from a clean detached checkout of `main` tip; it
  refuses any other checkout. `<commit>` must be on `main`'s first-parent history and strictly
  older than that tip: the tools never run from the commit being deployed or rolled back to.
  `publish` and `point` refuse `main` tip itself before any change, also from another directory,
  so a commit is publishable once a newer commit lands on `main`.
- Read serving version: `python3 ops/ship/release.py serving`; returns the `latest` tag, its
  commit and tree.
- Detect in-flight deploy and read one operation: `python3 ops/ship/release.py inflight` (exit 10
  while a draft exists); `gh release view <tag> --repo squarrelmingji/mxoptions --json isDraft,isPrerelease,assets`.
- Terminal states, all of them: `published`, `preview`, `refused`, `draft-left`, `timeout`. Only
  `published` counts as deployed to production; `preview` is a pre-release, not production.
  `refused` and `timeout`: the channel is as it was (a draft this run made and could remove is
  gone). `draft-left`: a release this run made, or may have made, is left for inspection: a
  draft, one published without the expected read-back, or a create whose outcome is unknown;
  nothing was deleted. Read `serving` and `inflight` next. Only the draft id the create reply
  names counts as this run's; a same-tag draft found later is never deleted.
- Deploy timeout: 45 minutes.

## Health check

- Command: `python3 ops/ship/release.py health [<tag>]` (default: the serving tag).
- Pass: the release and every file download (a missing release or file is a 404 and fails);
  `SHA256SUMS` verifies; `BUILDINFO.json` commit and tree equal the tag's; the wheel installs into
  a new venv with the pinned constraints and passes J2-J6 headless; the .deb names the right
  version and carries the installation files. It proves the published Linux files, never a
  device, a Windows build or a .deb installation (`make deb-test` needs unprivileged user
  namespaces, which the agents' host denies).

## Rollback

- Command to known-good: `python3 ops/ship/release.py point <known-good tag>`, from a clean
  checkout of `main` tip; the tag's commit must be older than that tip, as for `publish`.
- Restores: the stored release files of that tag, unchanged; it becomes `latest` and every newer
  stable release becomes a pre-release. Nothing is rebuilt and nothing published is deleted.
- Read back after rollback: `python3 ops/ship/release.py serving`, then `health`.

## Extra-care paths

| Glob                                                       | Reason                                                       | Own deploy | Automated |
| ---------------------------------------------------------- | ------------------------------------------------------------ | ---------- | --------- |
| `.github/workflows/**`                                     | CI definition and required checks                            | none       | yes: no deploy, gate only |
| `ops/ship/**`                                              | ship tooling: run from a clean `main` checkout               | none       | yes: no deploy, gate only |
| `ops/ship/hardware-gates.json`                             | decides which hardware results a stable release needs        | none       | yes: the reviewer checks every capability the PR adds or changes is listed with its hardware checks, and every new device-side file is in `paths` |
| `data/70-mxoptions.rules.in`, `mxoptions/hid_linux.py`     | device access rules, and a copy that udev runs as root       | inside the release files: same pattern, health and rollback as production | yes: the verdict must carry a security review of every changed line (privilege, paths, input parsing, what runs as root), regression tests for the changed behaviour, and a diff limited to the PR's stated scope. Agents never install the rules or run the helper as root on any computer. Undo: revert PR through the gate; for a release, `release.py point <known-good>`; installed copies change when their owners reinstall |

## Ask the owner first (repo-specific)

- Any pull request, issue or comment on `nictonus/mxoptions` or other upstream: a message to people.
- `make aur`, `make dist` uploads, or any publish to AUR, PyPI or another registry.
- Deleting a published release, its tag or its files. (`release.py` never does; it may delete
  only an unpublished draft it made itself.)
- Installing on, or running anything on, the owner's own computers; any test with real devices.
- A device write without a verified payload and a verified undo.

## Pending proofs

Not run yet. Each moves to the Proof log with its date and actual result when it passes.

| Level | Proof | Command | Expected result |
| ----- | ----- | ------- | --------------- |
| 0 | CI runs on PRs | `gh pr checks <first setup PR> --repo squarrelmingji/mxoptions` | `gate` and `journeys` listed and passing, with the run URL |
| 1 | Profile at PR head (first setup PR) | `git show HEAD:SHIP.md` in the reviewer's checkout of the PR head | present; the verdict record binds base, head and tree |
| 1 | Branch rule | `gh api repos/squarrelmingji/mxoptions/rules/branches/main` | deletion, non_fast_forward, pull_request, required_status_checks `gate` and `journeys` (app 15368), strict true |
| 1 | Merge methods | `gh repo view squarrelmingji/mxoptions --json squashMergeAllowed,mergeCommitAllowed` | both true |
| 1 | Merge deploys nothing | `grep -n -A4 '^on:' .github/workflows/*.yml`; `grep -c 'gh release' .github/workflows/*.yml`; `gh api repos/squarrelmingji/mxoptions/environments --jq .total_count` | `ci.yml` only, on pull_request, push to main and workflow_dispatch, contents read; `gh release` count 0; 0 environments; `release.py` runs only by hand |
| - | Ruleset undo | `gh api -X DELETE repos/squarrelmingji/mxoptions/rulesets/<ruleset id>` | recorded with the real id, not run |
| - | Reproduction | clean Ubuntu 24.04, Setup steps 1-3, Local gate, Behaviour check, by someone other than the profile author | every step passes; image, driver revision and prerequisites recorded |

## Proof log

| Date | Level | Proof | Command | Result |
| ---- | ----- | ----- | ------- | ------ |

None recorded yet.

Level `-` marks non-level checks, such as the reproduction check.
