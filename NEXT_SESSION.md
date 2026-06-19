# NEXT_SESSION — Build_Scripts

> End-of-session handoff. Overwrite at the end of each working block, then commit + push.
> Next session on any machine: `git pull`, open this folder in Claude Code, ask Claude to read CLAUDE.md + NEXT_SESSION.md and continue.

## Last worked: 2026-06-19 on Linux Threadripper (home)
## What I just did
- **sync_projects.py v1.0.0 (NEW)** — multi-repo git status + safe fast-forward
  update. No verb = read-only fetch + status table; `--pull` is FF-only, refuses
  dirty trees, skips ahead/diverged/detached/shallow, confirms per-repo (`--yes`
  to skip), `--dry-run`/`--non-interactive` for preview/CI. Selection: default =
  `build_projects.toml` set, `--project a,b`, `--all` (every git repo).
- **gitutil.py v1.0.0 (NEW)** — shared safe git layer: one `_git` chokepoint,
  read-only API + `pull_ff_only`, GCM-hardened network ops, NO force/reset
  surface. **projutil.py (NEW)** — project selection + `build_projects.toml` CRUD,
  extracted from build_projects.py (which now imports it; behavior byte-identical).
- Design from a 7-agent analysis; code passed a 3-lens adversarial review.
  Validated `--pull` against /tmp scratch repos (behind / behind+dirty / diverged).
- **build.py v1.11.1** — `report_repo_freshness` wired to gitutil (byte-identical
  output; graceful skip if gitutil absent on a remote host). build_all.py's local
  pull left inline by decision (1-line gain not worth a behavior change).
- Docs updated (ABOUT/HELP/USER_GUIDE/PROJECT_MEMORY/CLAUDE).

## Current state
- Branch: master. sync_projects.py v1 (status + ff-pull) built, tested, working.
- **build_projects.py FULLY VALIDATED — full cross-OS run, 9/9 green** (2026-06-19):
  all 3 projects × {linux local, windows via SSH-to-VM, macos via GitHub Actions}.
  ~63m wall-clock vs ~148m if fully serial (~2.3x from lane parallelism). The
  serial Windows lane is the critical path (~63m of job-time: ajj3 3m41 +
  WealthBuilder 22m21 + Thrift 36m53). All 9 binaries verified in dist/<os-arch>/.
- **Swap headroom held trivially** — peak 189 MiB of 31 GiB used during the
  heaviest concurrent load (2 Linux Nuitka + Windows VM compiling + 3 macOS).
- **build_all.py v1.2.3** — collector no longer nests prior tarballs.
- Host: 64GB RAM, swap now 32 GiB, vm.swappiness=10.

## Next task (the ONE thing)
- **Nothing pressing — the build + sync toolchain is complete and validated.**
  Optional enhancement tied to the planned extra VMs: a SECOND Windows build VM
  would let `build_projects.py` raise the windows lane cap to 2 and halve the
  critical path (Windows is the bottleneck because it's one serial VM). The 32GiB
  swap headroom already supports running more VMs concurrently.

## Open questions / blockers
- **sync write verbs (`--push`/`--commit`/`--merge`) are PARKED by decision
  (2026-06-19).** The owner confirmed pull/ff-only is all that's wanted; commit/
  push/merge are too dangerous for the marginal benefit. Don't build them unless
  asked. Status + ff-pull is the intended final shape of sync_projects.py.

## Resume commands
```
python sync_projects.py                       # status of the build-list set (read-only)
python sync_projects.py --all                 # status of every git repo
python sync_projects.py --all --pull --dry-run  # preview fast-forwards
python build_projects.py --list-projects      # the default build/sync set
python build_projects.py --only linux         # Linux-only (fast, ~14m)
python build_projects.py                       # FULL cross-OS (all 3 OSes, ~63m; Windows is the long pole)
```
