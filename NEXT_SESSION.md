# NEXT_SESSION — Build_Scripts

> End-of-session handoff. Overwrite at the end of each working block, then commit + push.
> Next session on any machine: `git pull`, open this folder in Claude Code, ask Claude to read CLAUDE.md + NEXT_SESSION.md and continue.

## Last worked: 2026-06-19 on Linux Threadripper (home)
## What I just did
- **build.py v1.11.0** — pre-build gate (`preflight_gate`): blocks a build on a
  missing entry point or declared `data_files`/`data_dirs` not on disk (exits
  before the compile; `--force` bypasses). Version drift / unbundled hints stay
  warnings. Plus a report-only repo-freshness check (read-only `git fetch`,
  never pulls).
- **build_projects.py v1.1.0 (NEW)** — multi-project scheduler over `build_all.py`.
  Schedules `(project × OS)` jobs by OS lane: windows=1 (shared VM), linux=2
  (`--linux-jobs`), macos=all (`--mac-jobs`). `--parallel`/`--sequential`,
  `--only`, `--all` discovery, `--dry-run`, `--` passthrough.
- **Default project list** — `build_projects.toml` (`projects = [...]`). Run
  `build_projects.py` with no args to build the curated set; add a future project
  = one line in that file.
- Docs refreshed (ABOUT/HELP/USER_GUIDE/PROJECT_MEMORY/CLAUDE); roadmap
  "parallel build-matrix" item marked DONE.

## Current state
- Branch: master, pushed. All three projects **build green on Linux** (verified
  by hand). The scheduler is verified at unit level (gate, freshness, all modes
  via `--dry-run`) but has **NOT yet run a real multi-project build**.

## Next task (the ONE thing)
- Run the first real `build_projects.py` build — Linux-only first as the safe
  validation, then the full cross-OS run:
  `python build_projects.py --only linux`   (then drop `--only linux`)

## Open questions / blockers
- None blocking. (Housekeeping: a few sibling repos carry `.idea/` IDE-state
  churn — not Build_Scripts' concern.)

## Resume commands
```
python build_projects.py --dry-run            # preview the default-list schedule
python build_projects.py --only linux         # first real run (safe)
python build_projects.py                       # full cross-OS, all default projects
python build.py "/path/to/project" --audit    # config sanity for one project
```
