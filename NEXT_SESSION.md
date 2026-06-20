# NEXT_SESSION — Build_Scripts

> End-of-session handoff. Overwrite at the end of each working block, then commit + push.
> Next session on any machine: `git pull`, open this folder in Claude Code, ask Claude to read CLAUDE.md + NEXT_SESSION.md and continue.

## Last worked: 2026-06-20 on Linux Threadripper (home) — big build_projects session
## What I just did (build_projects.py 1.2.0 → 1.3.0; build_all.py 1.2.6 → 1.2.7)
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
- All committed + pushed to master.

## Current state
- Branch master; **build_projects.py v1.3.0, build_all.py v1.2.7 — committed + pushed.**
- Windows lane: ONE VM, `--windows-jobs 2` is the practical sweet spot (lane=3
  safe but ~no gain for the 1-light + 2-heavy mix); VM auto start/stop in place.
- All linux + windows binaries current. macOS waits on quota reset or a local Mac.
- Host measured: 3990X 128T / **62GB RAM = the ceiling**; each heavy build peaks
  ~6GB / wants ~8 cores → one right-sized VM beats multiple small VMs.
- VM currently shut off (lifecycle test left it off — it now starts on demand).

## Next task (the ONE thing)
- **Nothing pressing.** Optional follow-ons below.

## Open questions / parked / optional
- **Per-build Nuitka job-cap (optional tuning)** — cap each windows build's
  `--jobs` to vCPU÷lanes so concurrent lanes don't oversubscribe the cores
  (currently each build asks for jobs=16, so lane=2 runs 32 threads on 16 vCPU).
  Would tune lane=2/3 a bit. Pass `--jobs` through build_all→build.py. Not done.
- **Per-run dynamic VM sizing (optional)** — size the VM's vCPU/RAM to
  `--windows-jobs` before cold-boot via `virsh setvcpus/setmaxmem --config`
  (cold boot picks up new sizing; Win10 needs a 2-socket topology for big counts).
  Useful if workloads grow heavier. Not done.
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
