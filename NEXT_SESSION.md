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
- Branch: master. All 14 project repos clean + synced. sync_projects.py v1
  (status + ff-pull) built, tested, working. build_projects.py regression-clean
  after the projutil extraction.

## Next task (the ONE thing)
- **sync write verbs** (`--push`, then `--commit MSG` / `--merge`) — goes BEYOND
  the chosen v1 scope (status + ff-pull), so confirm with me first. Build each
  against a deliberately dirty/ahead/diverged /tmp scratch repo since the live
  repos are all clean. `--push`: clean+ahead only, re-confirm branch, never
  `--force`, report-and-leave-untouched on auth failure.

## Open questions / blockers
- sync write verbs (`--push`/`--commit`/`--merge`) are deferred — build them
  against a deliberately dirty/ahead/diverged scratch repo (live repos are clean,
  so those paths can't be exercised). See PROJECT_MEMORY "Open items".
- The first real `build_projects.py` multi-project build is still unrun (Linux-
  only first): `python build_projects.py --only linux`.

## Resume commands
```
python sync_projects.py                       # status of the build-list set (read-only)
python sync_projects.py --all                 # status of every git repo
python sync_projects.py --all --pull --dry-run  # preview fast-forwards
python build_projects.py --list-projects      # the default build/sync set
python build_projects.py --only linux         # first real cross-OS build (Linux)
```
