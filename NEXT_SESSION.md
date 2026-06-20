# NEXT_SESSION — Build_Scripts

> End-of-session handoff. Overwrite at the end of each working block, then commit + push.
> Next session on any machine: `git pull`, open this folder in Claude Code, ask Claude to read CLAUDE.md + NEXT_SESSION.md and continue.

## Last worked: 2026-06-19 on Linux Threadripper (home) — a big session
## What I just did
- **build.py v1.11.1** — pre-build gate (blocks on a missing entry point or a
  declared `data_files`/`data_dirs` not on disk; `--force` bypasses) + report-
  only repo-freshness, wired to gitutil (graceful skip on a remote host).
- **build_all.py v1.2.6** — collector skips `*.tar.gz`/`*.zip` (no package
  nesting). Deliverables: **windows + macOS → `.zip`** (fixes the 0-byte Gumroad
  upload; macOS exec bit preserved), **linux → `.tar.gz` only** (native format).
- **build_projects.py v1.2.0** — multi-project scheduler (per-OS lanes:
  windows=1 shared VM, linux=2, macos=all) + default list (`build_projects.toml`)
  + CLI list/add/remove-project.
- **sync_projects.py v1.0.0 (NEW)** — multi-repo git status + safe ff-pull.
  Write verbs (`--push`/`--commit`/`--merge`) **PARKED** by decision.
- **gitutil.py v1.0.0 + projutil.py (NEW)** — shared safe-git + project-selection
  modules; build.py/build_projects.py import them.
- **Validated:** full cross-OS build **9/9 green** (3 projects × linux/windows/
  macos). **Tuned:** Windows VM → 16 vCPU / 32 GB (Thrift 37m→21m, measured);
  host swap → 32 GiB, `vm.swappiness=10`.

## Current state
- Branch master; **everything committed + pushed; all 13 repos clean + synced.**
- The build + sync toolchain is **COMPLETE, validated end-to-end, and tuned.**
  No known issues.

## Next task (the ONE thing)
- **Nothing pressing — the toolchain is done.** Pick from the optional items
  below only if/when you want them.

## Open questions / parked / optional
- **sync write verbs PARKED** (`--push`/`--commit`/`--merge`) — pull/ff-only is
  the intended final shape of sync_projects.py. Don't build them unless asked.
- **macOS Gatekeeper** — the `.zip` fixes the 0-byte *upload* issue, but the
  binaries are **unsigned / un-notarized**, so Gatekeeper blocks them on buyers'
  Macs (workaround: right-click→Open, or `xattr -dr com.apple.quarantine`).
  Proper fix = code-signing + notarization (Apple Developer acct, ~$99/yr).
  Separate task if distributing macOS commercially.
- **Windows lane=2 — DONE (no 2nd VM needed).** 2026-06-20: measured two
  concurrent Windows compiles on the single 32GB VM (peak ~12GB combined, ~20GB
  free) — `build_projects.py --windows-jobs 2` runs two lanes for a ~30%
  Windows-lane speedup. Default stays 1. (A 2nd VM or more RAM could push to 3+,
  but isn't necessary for 2.)
- `llm-from-scratch` was intentionally retired (OBE by ajj3-brain) — don't flag
  its local absence as lost work.

## Resume commands
```
python build_projects.py                  # full cross-OS build (default project set)
python build_projects.py --only linux     # Linux-only (fast, ~14m)
python build_projects.py --list-projects  # the default build/sync set
python sync_projects.py --all             # git status of every repo (read-only)
python sync_projects.py --all --pull      # ff-only update of clean+behind repos
```
