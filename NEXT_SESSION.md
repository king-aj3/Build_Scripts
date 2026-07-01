# NEXT_SESSION — Build_Scripts

> End-of-session handoff. Overwrite at the end of each working block, then commit + push.
> Next session on any machine: `git pull`, open this folder in Claude Code, ask Claude to read CLAUDE.md + NEXT_SESSION.md and continue.

## Last worked: 2026-07-01 on Linux Threadripper (home) — build_projects UX (menu + help)

## What I just did (build_projects.py v1.4.1 → v1.5.0)
- **`--menu`** — interactive picker (stdlib prompts): choose projects → OSes →
  dry-run/sequential, see the macOS **10x-billing** caveat AT the point of choice,
  echo the equivalent command, then build after `Run now? [y/N]`. Fills the same
  args the flags would, so a bare run is unchanged. Smoke-tested end-to-end.
- **Clearer `--help`** — the macOS default-skip now LEADS the `-h` description + an
  examples epilog gives the exact `--only linux,windows,macos` command (was buried in
  `--only`). Fixed HELP/USER_GUIDE recipes that mislabeled a bare run "all OSes".
- Docs updated: HELP, USER_GUIDE, README, ABOUT (v1.5.0 what's-new + date),
  PROJECT_MEMORY (design entry), CLAUDE (version). Committed + pushed.

## Earlier this cycle (build_projects.py 1.2.0 → 1.4.1; build_all.py 1.2.6 → 1.2.7)
- **v1.2.1** — Ctrl-C now actually aborts a run (`shutdown(wait=False,
  cancel_futures=True)`); clean exit 130, no traceback. (Was draining the cap-1
  windows queue and finishing builds after the interrupt.)
- **v1.2.2 + build_all v1.2.7** — macOS **skipped by default** (private-repo
  Actions billing: 10× macOS, quota spent). A billing-blocked macOS run reports
  **SKIP** not FAIL (build_all `_is_billing_block` → exit `_EXIT_SKIPPED=3` →
  build_projects `Result.status` ok|fail|skip). `--only ...,macos` to force it.
- **v1.2.3** — `--windows-jobs N` (default 1). MEASURED 2 AND 3 concurrent
  Windows compiles fine on the single 32GB VM (peak ~12GB, ~20GB free); lane=2
  ~30% faster. The old "needs a 2nd VM for lane=2" note was WRONG — corrected.
- **v1.3.0** — auto **start/stop the Windows VM** (`win10_pro_x64_python`) around
  windows builds: start if shut off (wait for SSH), graceful shutdown after all
  binaries built+copied — only if it started the VM; leave up on failure / clean
  up on Ctrl-C; never force. Config `build_projects.toml [windows_vm]`;
  `--no-manage-vm` skips. Verified both paths (already-running→up; off→start/stop).
- **v1.4.0** — per-build **job-cap + dynamic VM sizing**. On cold-start, `virt-xml`
  right-sizes the VM to `K*cores_per_build` vCPU (2-socket) / `K*mem_per_build_gb`
  GB and each build runs `--jobs cores_per_build` → no oversubscription. Defaults
  16/16. **Never resizes/stops an already-running VM.**
- **v1.4.1** — **dynamic sizing DEFAULT OFF.** Benchmark: more vCPU = ~27% slower
  per build (L3/CCD locality; 16 vCPU is the sweet spot), socket layout irrelevant,
  job-cap neutral. So `size_to_jobs=false`; a fixed 16-vCPU VM + lane parallelism is
  fastest (lane-2 ~25m). Sizing code kept but dormant.
- All committed + pushed to master.

## Current state
- Branch master; **build_projects.py v1.5.0, build_all.py v1.2.7 — committed + pushed.**
- Windows lane: ONE fixed **16-vCPU** VM (measured sweet spot); `--windows-jobs 2`
  → ~25m lane-2; auto start/stop in place; dynamic sizing OFF (it hurt — see v1.4.1).
- All linux + windows binaries current. macOS waits on quota reset or a local Mac.
- Host: 3990X 128T / **62GB RAM**; build is **L3-locality-bound → ~16 vCPU sweet
  spot** (more vCPU ~27% slower); one fixed-size VM beats sizing or multiple VMs.
- VM **shut off at 16 vCPU(2x8)/32GB** (benchmark left it there; size_to_jobs off so
  it keeps this size — starts on demand for builds).

## Next task (the ONE thing)
- **Nothing pressing.** Optional follow-ons below.

## Open questions / parked / optional
- **Job-cap + dynamic VM sizing — shipped (v1.4.0), then DEFAULTED OFF (v1.4.1)**
  after benchmarking showed vCPU growth is counterproductive here (~27% slower; 16
  vCPU sweet spot). Code dormant; flip size_to_jobs=true only where more vCPU helps.
  End-to-end resize/start/build/shutdown all verified.
- **Local macOS build target** — a used Apple-Silicon Mac mini (16GB, ~$450-550)
  as an SSH host or self-hosted runner = native arm64, **zero Actions minutes**.
  The real macOS fix vs. the billing block. (Docker/VM can't do arm64 on x86_64.)
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
